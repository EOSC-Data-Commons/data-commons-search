"""Hybrid search over the `datasets` table in PostgreSQL.

Replaces the OpenSearch index. Two retrieval channels run in a single SQL statement and are
combined in Python:

- lexical: BM25 over `datasets.search_text` (`pg_textsearch`, the `<@>` operator)
- semantic: cosine similarity over `record_embeddings` (`pgvectorscale`, StreamingDiskANN)

The schema (tables, indexes, the `labels` mapping used to search one named embedding field at a
time) lives in the metadata-warehouse repo, in `scripts/postgres_data/create_sql/appdb/`. The
embedding model below must stay the one used there to index, otherwise query and document vectors
are not comparable.
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

import httpx
from sqlalchemy import text

from data_commons_search.config import settings
from data_commons_search.db import engine
from data_commons_search.models import SearchHit, SearchResults
from data_commons_search.utils import logger

# `record_embeddings.labels`, generated from the `field` column so a query can restrict the
# DiskANN scan to one named embedding. Keep in sync with the generated column in tables.sql.
FIELD_LABELS: dict[str, int] = {"title": 1, "description": 2, "keywords": 3}


class QueryEmbedder:
    """Embeds a search query with the same model that produced the indexed vectors.

    The nomic models are trained with asymmetric task prefixes: documents were indexed with
    `search_document: `, so queries must carry `search_query: `. A mismatch does not raise, it
    just quietly degrades every result, which is why the prefix is a setting and not a literal.
    """

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.embedding_api_url,
            # FEDLLM_API_KEY is the key of the EGI endpoint embedding_api_url points at
            headers={"Authorization": f"Bearer {settings.fedllm_api_key}"},
            timeout=httpx.Timeout(30.0, connect=10.0),
        )

    async def embed(self, query: str) -> list[float]:
        """Return the embedding vector for a user query."""
        resp = await self._client.post(
            "/embeddings",
            json={
                "model": settings.embedding_model,
                "input": [settings.embedding_query_prefix + query],
            },
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


query_embedder = QueryEmbedder()


def _vector_literal(vector: list[float]) -> str:
    """Render a vector as the text form pgvector casts with `::vector`."""
    return "[" + ",".join(f"{v:.7f}" for v in vector) + "]"


def _semantic_branch() -> str:
    """One DiskANN scan per named embedding field, unioned.

    Scanning each field separately (rather than once over the whole index) guarantees every field
    contributes candidates: a single scan is easily filled entirely by description chunks, which
    are by far the most numerous. `field_weight` shades the similarity so that, between two equally
    close chunks, a title match outranks a description one.
    """
    branches = []
    for field, label in FIELD_LABELS.items():
        weight = settings.embedding_field_weights.get(field, 1.0)
        # S608 below: `weight` and `label` come from code-controlled config, never from the
        # request. Everything a user provides (query, vector, creator) goes in as a bind parameter.
        branch = f"""
        (SELECT e.record_url, (1 - (e.embedding <=> :vector)) * {weight} AS similarity
         FROM record_embeddings e
         WHERE e.labels && ARRAY[{label}]::smallint[]
         ORDER BY e.embedding <=> :vector
         LIMIT :pool)"""  # noqa: S608
        branches.append(branch)
    return "\n        UNION ALL".join(branches)


def _build_sql(hard_filter: str) -> str:
    """Full hybrid query. `hard_filter` is an extra predicate on `datasets`, or an empty string."""
    lexical_where = f"WHERE {hard_filter}" if hard_filter else ""
    semantic_where = f"WHERE {hard_filter}" if hard_filter else ""
    # S608 below: `hard_filter` is one of two literals chosen in `_run_query`, and the creator name
    # it matches against is a bind parameter, so nothing from the request is interpolated here.
    sql = f"""
WITH lexical AS (
    SELECT d.url, -(d.search_text <@> to_bm25query(:query, :bm25_index)) AS score
    FROM datasets d
    {lexical_where}
    ORDER BY d.search_text <@> to_bm25query(:query, :bm25_index)
    LIMIT :pool
),
semantic_chunks AS ({_semantic_branch()}
),
semantic AS (
    SELECT sc.record_url AS url, max(sc.similarity) AS score
    FROM semantic_chunks sc
    JOIN datasets d ON d.url = sc.record_url
    {semantic_where}
    GROUP BY sc.record_url
),
candidates AS (
    SELECT url FROM lexical
    UNION
    SELECT url FROM semantic
)
SELECT
    d.url, d.doi, d.title, d.description, d.keywords, d.creators,
    d.publication_year, d.publication_date, d.repository_code,
    l.score AS lexical_score,
    s.score AS semantic_score
FROM candidates c
JOIN datasets d ON d.url = c.url
LEFT JOIN lexical l ON l.url = c.url
LEFT JOIN semantic s ON s.url = c.url
"""  # noqa: S608
    return sql


def _min_max(value: float | None, low: float, high: float) -> float:
    """Normalize one channel's score into 0..1 over the candidate pool.

    A dataset found by only one channel scores 0 on the other, which is what makes the weights
    behave: a strong hit in both channels outranks a strong hit in one.
    """
    if value is None:
        return 0.0
    if high <= low:
        return 1.0
    return (value - low) / (high - low)


def _to_search_hit(row: Any, score: float) -> SearchHit:
    """Map one result row onto the `SearchHit` shape the rest of the app already speaks."""
    return SearchHit.model_validate(
        {
            "_id": row.url,
            "_score": score,
            "_source": {
                "url": row.url,
                "doi": row.doi,
                "_repo": row.repository_code,
                "titles": [{"title": row.title}] if row.title else [],
                "descriptions": [{"description": row.description}] if row.description else [],
                "subjects": [{"subject": kw} for kw in (row.keywords or [])],
                "creators": [{"creatorName": name} for name in (row.creators or [])],
                "publicationYear": str(row.publication_year) if row.publication_year else None,
                "dates": (
                    [{"date": row.publication_date.isoformat(), "dateType": "Issued"}] if row.publication_date else []
                ),
                "resourceType": "dataset",
            },
        }
    )


def _in_date_range(published: date | None, start_date: str | None, end_date: str | None) -> bool:
    """Whether a publication date falls inside a requested range."""
    if published is None:
        return False
    if start_date and published.isoformat() < start_date:
        return False
    return not (end_date and published.isoformat() > end_date)


def _run_query(query: str, vector: list[float], creator_name: str | None) -> list[Any]:
    """Execute the hybrid query, blocking. Called through `asyncio.to_thread`."""
    params: dict[str, Any] = {
        "query": query,
        "vector": _vector_literal(vector),
        "pool": settings.candidate_pool,
        "bm25_index": settings.bm25_index,
    }
    # Substring match on any creator. Applied as a filter on top of both channels rather than
    # before them, so a very selective name can leave fewer than `candidate_pool` results.
    hard_filter = ""
    if creator_name:
        hard_filter = "EXISTS (SELECT 1 FROM unnest(d.creators) AS creator WHERE creator ILIKE :creator)"
        params["creator"] = f"%{creator_name}%"
    with engine.connect() as conn:
        return list(conn.execute(text(_build_sql(hard_filter)), params))


async def search_datasets(
    query: str,
    start_date: str | None = None,
    end_date: str | None = None,
    creator_name: str | None = None,
) -> SearchResults:
    """Hybrid BM25 + vector search over the indexed datasets.

    Args:
        query: Natural language search input
        start_date: Optional start date in yyyy-MM-dd. Currently unused, see DATES DISABLED below
        end_date: Optional end date in yyyy-MM-dd. Currently unused, same
        creator_name: Optional creator name, matched as a substring against any creator

    Returns:
        The top `search_results_count` datasets, scored 0..1.
    """
    vector = await query_embedder.embed(query)
    rows = await asyncio.to_thread(_run_query, query, vector, creator_name)
    if not rows:
        return SearchResults(total_found=0, hits=[])

    lexical_scores = [r.lexical_score for r in rows if r.lexical_score is not None]
    semantic_scores = [r.semantic_score for r in rows if r.semantic_score is not None]
    lex_lo, lex_hi = (min(lexical_scores), max(lexical_scores)) if lexical_scores else (0.0, 0.0)
    sem_lo, sem_hi = (min(semantic_scores), max(semantic_scores)) if semantic_scores else (0.0, 0.0)
    semantic_weight, lexical_weight = settings.hybrid_weights

    scored: list[tuple[float, Any]] = []
    for row in rows:
        score = semantic_weight * _min_max(row.semantic_score, sem_lo, sem_hi) + lexical_weight * _min_max(
            row.lexical_score, lex_lo, lex_hi
        )
        # DATES DISABLED - the dates a user asks about are the period the data is *about*, but the
        # only date we index is `publication_date`, when the record was published. A 1960-2020 time
        # series published in 2023 matches the question and not the metadata, so scoring on
        # publication_date works against the user even as a soft boost. `start_date`/`end_date` stay
        # in the signature (the LLM still extracts them) but are unused until we index a temporal
        # coverage field. Uncomment to restore the boost.
        # if (start_date or end_date) and _in_date_range(row.publication_date, start_date, end_date):
        #     score *= settings.date_boost
        # Demote datasets without a description: they are almost always unusable to the reader.
        if not row.description:
            score *= settings.description_penalty
        scored.append((score, row))

    scored.sort(key=lambda pair: pair[0], reverse=True)
    hits = [_to_search_hit(row, score) for score, row in scored[: settings.search_results_count]]
    logger.debug(
        f"search_datasets: {len(rows)} candidates "
        f"({len(lexical_scores)} lexical, {len(semantic_scores)} semantic) for {query!r}"
    )
    # No cheap exact count: BM25 and DiskANN both stop at `candidate_pool`, so this is how many
    # datasets were considered, not how many exist that could match.
    return SearchResults(total_found=len(rows), hits=hits)
