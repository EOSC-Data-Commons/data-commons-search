"""Hybrid search over the appdb `dataset` tables, built by the DAGs in the `dags/` folder.

One SQL statement retrieves a candidate list per (method, field):

- semantic: cosine similarity of the query to the named embeddings of each field (pgvectorscale
  DiskANN, one partial index per field in `dataset_embedding`)
- lexical: BM25 on each field of `dataset` (pg_textsearch, one index per field)

Each list is min-max normalized over its own candidates, then the lists are combined with
weights: `hybrid_weights` between the methods, `search_field_weights` between the fields. A dataset
missing from a list scores 0 there, so a dataset matching on several fields ranks above one
matching on a single field (e.g. a bare title with no description).
"""

import asyncio
from typing import Any

from sqlalchemy import (
    CTE,
    ColumnElement,
    Float,
    Integer,
    Select,
    String,
    bindparam,
    case,
    cast,
    func,
    literal,
    select,
    union_all,
)
from sqlalchemy.dialects.postgresql import JSONB, JSONPATH

from data_commons_search.config import settings
from data_commons_search.dataset_models import (
    SEARCH_FIELDS,
    Dataset,
    DatasetEmbedding,
    bm25_index_name,
    creator_name_view,
)
from data_commons_search.db import SessionLocal
from data_commons_search.models import SearchHit, SearchResults

# Datasets matching the hard filters (creator) that get their vectors compared exactly. Above
# that, a selective filter on top of an ANN scan would return too few results
FILTERED_MAX_DATASETS = 10000
# Creator filter: the names closest to the requested one (typos, accents, word order), kept above a
# trigram similarity. A typo-ed full name scored 0.50 on its person, 0.39 on the next one
CREATOR_CANDIDATES = 20
CREATOR_MIN_SIMILARITY = 0.45
# DataCite date types describing the period the data covers, used for the soft date boost.
# Publication dates are not: a 1960-2020 series published in 2023 matches a 1960-2020 question
COVERAGE_DATE_TYPES = ("Collected", "Coverage", "Valid")
COVERAGE_IN_RANGE = "$[*] ? (@.type == $types[*]) ? (@.date >= $start && @.date <= $end)"

# Bound once per statement, the values are given at execution
QUERY = bindparam("query", type_=String)
VECTOR = bindparam("vector", type_=DatasetEmbedding.embedding.type)
POOL = bindparam("pool", type_=Integer)
CREATOR = bindparam("creator", type_=String)


def _semantic_list(field: str, filtered: CTE | None) -> Select[Any]:
    """Best chunk similarity per dataset for one embedded field."""
    distance = DatasetEmbedding.embedding.cosine_distance(VECTOR)
    if filtered is not None:
        # Exact distances over the filtered datasets, ordering on the score (not on the distance
        # operator) keeps the planner off the ANN index
        score = func.max(1 - distance).label("score")
        return (
            select(DatasetEmbedding.dataset_id, score)
            .join(filtered, filtered.c.id == DatasetEmbedding.dataset_id)
            .where(DatasetEmbedding.field == field)
            .group_by(DatasetEmbedding.dataset_id)
            .order_by(score.desc())
            .limit(POOL)
        )
    # ORDER BY distance LIMIT is what the DiskANN partial index of the field serves
    chunks = (
        select(DatasetEmbedding.dataset_id, (1 - distance).label("score"))
        .where(DatasetEmbedding.field == field)
        .order_by(distance)
        .limit(POOL)
        .subquery()
    )
    return select(chunks.c.dataset_id, func.max(chunks.c.score).label("score")).group_by(chunks.c.dataset_id)


def _lexical_list(field: str, filtered: CTE | None) -> Select[Any]:
    """BM25 score per dataset for one field. pg_textsearch returns negated scores, lowest first."""
    bm25 = getattr(Dataset, field).op("<@>", return_type=Float)(func.to_bm25query(QUERY, bm25_index_name(field)))
    matches = select(Dataset.id.label("dataset_id"), (-bm25).label("score")).order_by(bm25).limit(POOL)
    if filtered is not None:
        matches = matches.where(Dataset.id.in_(select(filtered.c.id)))
    subquery = matches.subquery()
    return select(subquery.c.dataset_id, subquery.c.score).where(subquery.c.score > 0)


def _weights() -> dict[str, float]:
    """Weight of each list, summing to 1 so the fused score stays in 0..1 (before the date boost)."""
    semantic, lexical = settings.hybrid_weights
    fields_total = sum(settings.search_field_weights.get(f, 0.0) for f in SEARCH_FIELDS) or 1.0
    weights = {}
    for field in SEARCH_FIELDS:
        share = settings.search_field_weights.get(field, 0.0) / fields_total
        weights[f"semantic_{field}"] = semantic * share
        weights[f"lexical_{field}"] = lexical * share
    return weights


def _creator_datasets() -> CTE:
    """Datasets of the creator names closest to the requested one."""
    query_key = func.creator_key(CREATOR)
    # KNN on the trigram index first, the similarity cut after: a cut inside the index scan would
    # read the whole index when no name is close enough
    nearest = (
        select(creator_name_view.c.dataset_ids, func.similarity(creator_name_view.c.key, query_key).label("similarity"))
        .order_by(creator_name_view.c.key.op("<->")(query_key))
        .limit(CREATOR_CANDIDATES)
        .subquery()
    )
    return (
        select(func.unnest(nearest.c.dataset_ids).label("id"))
        .where(nearest.c.similarity >= CREATOR_MIN_SIMILARITY)
        .distinct()
        .limit(FILTERED_MAX_DATASETS)
        .cte("filtered")
        .prefix_with("MATERIALIZED")
    )


def _build_query(with_vector: bool, creator_filter: bool, date_range: dict[str, Any] | None) -> Select[Any]:
    """Full hybrid query, returning the best datasets with their fused score and the candidates count."""
    filtered = _creator_datasets() if creator_filter else None
    lists: list[tuple[str, Select[Any]]] = []
    for field in SEARCH_FIELDS:
        if with_vector:
            lists.append((f"semantic_{field}", _semantic_list(field, filtered)))
        lists.append((f"lexical_{field}", _lexical_list(field, filtered)))
    subqueries = [(name, query.subquery(name)) for name, query in lists]
    candidates = union_all(
        *(select(literal(name).label("list"), sq.c.dataset_id, sq.c.score) for name, sq in subqueries)
    ).cte("candidates")

    # Min-max normalization within each list, a list with a single candidate gives it 1
    low = func.min(candidates.c.score).over(partition_by=candidates.c.list)
    high = func.max(candidates.c.score).over(partition_by=candidates.c.list)
    normalized = select(
        candidates.c.list,
        candidates.c.dataset_id,
        func.coalesce((candidates.c.score - low) / func.nullif(high - low, 0), 1.0).label("score"),
    ).cte("normalized")
    weight = case({name: literal(w) for name, w in _weights().items()}, value=normalized.c.list, else_=literal(0.0))
    fused = (
        select(normalized.c.dataset_id, func.sum(normalized.c.score * weight).label("score"))
        .group_by(normalized.c.dataset_id)
        .cte("fused")
    )

    score: ColumnElement[Any] = fused.c.score
    if date_range:
        in_range = func.jsonb_path_exists(
            Dataset.dates, cast(literal(COVERAGE_IN_RANGE), JSONPATH), cast(literal(date_range, JSONB), JSONB)
        )
        score = score * case((in_range, settings.date_boost), else_=1.0)
    score = score.label("score")
    return (
        select(Dataset, score, func.count().over().label("total"))
        .join(fused, fused.c.dataset_id == Dataset.id)
        .order_by(score.desc())
        .limit(bindparam("limit"))
    )


def _date_range(start_date: str | None, end_date: str | None) -> dict[str, Any] | None:
    """jsonpath variables of the date boost, dates compare as yyyy-mm-dd strings."""
    if not start_date and not end_date:
        return None
    return {"start": start_date or "0000-01-01", "end": end_date or "9999-12-31", "types": list(COVERAGE_DATE_TYPES)}


def _to_hit(dataset: Dataset, score: float) -> SearchHit:
    """Map a dataset onto the `SearchHit` shape the agent and the frontend already use."""
    dates = [{"date": d["date"], "dateType": d["type"]} for d in dataset.dates]
    # SearchHit reads the publication date from the Issued date, which the transform may have derived
    if dataset.publication_date and not any(d["dateType"] == "Issued" for d in dates):
        dates.append({"date": dataset.publication_date.isoformat(), "dateType": "Issued"})
    return SearchHit.model_validate(
        {
            "_id": dataset.url,
            "_score": score,
            "_source": {
                "url": dataset.url,
                "doi": dataset.doi,
                "_repo": dataset.repository,
                "_harvest_url": dataset.harvest_url,
                "titles": [{"title": dataset.title}],
                "descriptions": [{"description": dataset.description}] if dataset.description else [],
                "subjects": [
                    {"subject": s["name"], "subjectScheme": s.get("scheme"), "valueUri": s.get("uri")}
                    for s in dataset.subjects
                ],
                "creators": [{"creatorName": c["name"], **c} for c in dataset.creators],
                "dates": dates,
                "publicationYear": str(dataset.publication_year) if dataset.publication_year else None,
            },
        }
    )


def _run_query(statement: Select[Any], params: dict[str, Any]) -> list[Any]:
    with SessionLocal() as session:
        return list(session.execute(statement, params))


async def search_datasets(
    query: str,
    vector: list[float] | None,
    limit: int,
    start_date: str | None = None,
    end_date: str | None = None,
    creator_name: str | None = None,
) -> SearchResults:
    """Hybrid search, lexical only when the query could not be embedded.

    `total_found` is the number of candidates considered: both methods stop at `candidate_pool`
    per field, there is no cheap exact count of all the matching datasets.
    """
    statement = _build_query(vector is not None, bool(creator_name), _date_range(start_date, end_date))
    params: dict[str, Any] = {"query": query, "pool": settings.candidate_pool, "limit": limit}
    if vector is not None:
        params["vector"] = vector
    if creator_name:
        params["creator"] = creator_name
    rows = await asyncio.to_thread(_run_query, statement, params)
    return SearchResults(total_found=rows[0].total if rows else 0, hits=[_to_hit(r.Dataset, r.score) for r in rows])
