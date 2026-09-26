import argparse
import asyncio
import json
from typing import Any

import httpx
from fastembed import TextEmbedding
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP
from opensearchpy import OpenSearch

from data_commons_search.config import settings
from data_commons_search.embeddings import embed_texts, rerank
from data_commons_search.models import SearchHit, SearchResults, UserInfo
from data_commons_search.utils import Timer, logger, timed
from data_commons_search.zenodo import ZENODO_REPO, search_zenodo_datasets

# Create MCP server https://github.com/modelcontextprotocol/python-sdk
mcp = FastMCP(
    name="EOSC Data Commons MCP",
    debug=settings.debug_enabled,
    dependencies=["mcp", "httpx", "opensearch-py", "fastembed", "pydantic"],
    instructions="Provide tools that helps users access data from various open-access data publishers, developed for the EOSC Data Commons project.",
    json_response=True,
    stateless_http=True,
    streamable_http_path="/",
)

embedding_model = TextEmbedding(settings.embedding_model)
opensearch_client = OpenSearch(hosts=[settings.opensearch_url], pool_maxsize=20)

# Guard rail on the `limit` a caller can ask a search tool for
MAX_RESULTS_COUNT = 50


def _log_mcp_user() -> UserInfo | None:
    """Return the authenticated MCP caller for a tool, if the request carried a valid bearer token.

    Returns the resolved ``UserInfo`` when authenticated (None otherwise) so tools can later tailor
    behaviour (e.g. user preferences). For now identity is purely informational.
    """
    token = get_access_token()
    return getattr(token, "userinfo", None) if token else None


# https://github.com/EOSC-Data-Commons/metadata-warehouse/blob/main/src/config/opensearch_mapping.json


def _hit_key(hit: SearchHit) -> str:
    """Identity of a hit when merging the result lists of several search terms."""
    url = hit.dataset_url
    return url.lower().replace("http://", "https://").rstrip("/") if url else hit.id


def _merge_dedup(result_sets: list[SearchResults]) -> SearchResults:
    """Concatenate one result list per search input and per backend into the pool to rerank, dropping repeats.

    Order within the pool does not rank anything: the reranker scores every hit and decides the
    output order. Duplicates keep their first occurrence, so our own entry wins over the same record
    from Zenodo since our lists come first.
    """
    seen: set[str] = set()
    hits: list[SearchHit] = []
    for res in result_sets:
        for hit in res.hits:
            key = _hit_key(hit)
            if key in seen:
                continue
            seen.add(key)
            hits.append(hit)
    return SearchResults(total_found=sum(r.total_found for r in result_sets), hits=hits)


def _rerank_query(user_question: str, queries: list[str]) -> str:
    """What to rerank against: the user's question, falling back to the search terms if it is blank.

    `user_question` is a required tool argument, so the fallback only covers a caller that sends an
    empty string rather than a real question.
    """
    return user_question.strip() or " | ".join(queries)


def _select_reranked(ranked_hits: list[SearchHit], limit: int) -> list[SearchHit]:
    """Keep the best `limit` hits, reserving a share of the slots for our own index.

    Zenodo contributes as many candidates as our index per search input and is far larger, so a
    plain `ranked_hits[:limit]` can return Zenodo only and hide our own records, which the
    federation is meant to complement rather than replace. Rerank order is kept on both sides: the
    reserved slots go to our best-scored hits, the rest follows the global rerank order.
    """
    reserved = int(limit * settings.own_results_min_share)
    if reserved <= 0:
        return ranked_hits[:limit]
    ours = [hit for hit in ranked_hits if hit.source.repo != ZENODO_REPO][:reserved]
    reserved_ids = {id(hit) for hit in ours}
    kept = ours + [hit for hit in ranked_hits if id(hit) not in reserved_ids]
    return sorted(kept[:limit], key=lambda hit: hit.score or 0.0, reverse=True)


async def _rerank_hits(
    query: str, res: SearchResults, limit: int, timings: dict[str, float] | None = None
) -> SearchResults:
    """Score the merged hits with the cross-encoder and keep the best `limit` of them.

    `query` is the user's own question, not the search terms derived from it: the terms are written
    to cast a wide net for retrieval, while reranking is asked what actually answers the question.
    One rerank call for the whole merged pool, so every hit gets a score on the same scale whichever
    search input (and whichever backend) produced it. If the rerank API is unavailable we return the
    head of the merged pool as-is, i.e. mostly the first search input's own top hits.
    """
    if not res.hits:
        return res
    documents = [f"{hit.title or ''}\n{hit.description or ''}".strip() for hit in res.hits]
    with Timer() as t_rerank:
        # Score the whole pool (no top_n): the selection below needs every hit's score, not just
        # the head the API would return, or the reserved slots have nothing left to pick from
        ranked = await rerank(query, documents)
    if timings is not None:
        timings["rerank"] = t_rerank.ms
    if ranked is None:
        logger.warning(f"Reranking unavailable, returning the first {limit} merged hits")
        return SearchResults(total_found=res.total_found, hits=res.hits[:limit])
    for idx, score in ranked:
        res.hits[idx].score = score
    hits = _select_reranked([res.hits[idx] for idx, _ in ranked], limit)
    logger.debug(
        f"rerank of {len(documents)} hits took {t_rerank.ms:.0f} ms, kept {len(hits)}: "
        + " | ".join(f"{h.title!r} ({h.source.repo}) score={h.score:.3f}" for h in hits)
    )
    return SearchResults(total_found=res.total_found, hits=hits)


async def _embed_query(search_input: str) -> list[float]:
    """Query vector: from the embeddings API when `embed_api_model` is set, else local fastembed.

    The OpenSearch index holds vectors from the fastembed `embedding_model`, so switching to the API
    needs the index re-embedded with the same model (and `embed_query_prefix` set for its family).
    """
    if settings.embed_api_model:
        vectors = await embed_texts([f"{settings.embed_query_prefix}{search_input}"])
        if vectors:
            return vectors[0]
        logger.warning("Embeddings API unavailable, falling back to the local fastembed model")
    # Blocking CPU work -> offload to a thread
    embedding = await asyncio.to_thread(lambda: next(iter(embedding_model.embed([search_input]))))
    return embedding.tolist()


@mcp.tool()
async def search_data(
    search_input: list[str],
    user_question: str,
    limit: int = settings.search_results_count,
    start_date: str | None = None,
    end_date: str | None = None,
    creator_name: str | None = None,
) -> SearchResults:
    """Search for datasets relevant to the user question.

    Args:
        search_input: List of natural language search inputs, all searched in one call
        user_question: The user's question, copied as asked (only fix typos and make it standalone).
            Used to rank the results
        limit: Number of datasets to return
        start_date: Optional start date in yyyy-MM-dd
        end_date: Optional end date in yyyy-MM-dd
        creator_name: Optional creator name to filter by

    Returns:
        The `limit` most relevant datasets (total_found, hits[]) across all search inputs
    """
    user = _log_mcp_user()
    if user:
        logger.info(f"Tool call `search_data` by user '{user.preferred_username or user.sub}'")
    queries = [q for q in search_input if q and q.strip()]
    if not queries:
        return SearchResults(total_found=0, hits=[])
    limit = max(1, min(limit, MAX_RESULTS_COUNT))
    # Per-stage durations filled in by the helpers below, then logged as one breakdown. Search inputs
    # run concurrently, so each backend reports its slowest input: what that stage cost in wall clock.
    timings: dict[str, float] = {}
    with Timer() as t_total:
        # Each input retrieves `limit` hits from our index plus `limit` from Zenodo, concurrently. All
        # those lists are then merged into one deduplicated pool and reranked down to `limit` results.
        results = await asyncio.gather(
            *(_search_data_one(q, limit, start_date, end_date, creator_name, timings) for q in queries)
        )
        pool = _merge_dedup([res for per_input in results for res in per_input])
        final = await _rerank_hits(_rerank_query(user_question, queries), pool, limit, timings)
    logger.info(
        f"`search_data` took {t_total.ms:.0f} ms for {len(queries)} search input(s), {len(final.hits)} hits: "
        f"embed {timings.get('embed', 0):.0f} ms | opensearch {timings.get('opensearch', 0):.0f} ms | "
        f"zenodo {timings.get('zenodo', 0):.0f} ms | rerank {timings.get('rerank', 0):.0f} ms"
    )
    return final


async def _search_data_one(
    search_input: str,
    limit: int,
    start_date: str | None = None,
    end_date: str | None = None,
    creator_name: str | None = None,
    timings: dict[str, float] | None = None,
) -> list[SearchResults]:
    """Run the hybrid dataset search for a single search term, one result list per backend queried.

    `timings` (when given) collects the slowest duration seen per stage across all search inputs.
    """

    def record(stage: str, ms: float) -> None:
        if timings is not None:
            timings[stage] = max(timings.get(stage, 0.0), ms)

    # ZENODO FEDERATION - started first so it runs while we embed and query OpenSearch.
    zenodo_task = (
        asyncio.create_task(timed(search_zenodo_datasets(search_input, limit, start_date, end_date, creator_name)))
        if settings.zenodo_search_enabled
        else None
    )

    with Timer() as t_embed:
        embedding = await _embed_query(search_input)
    record("embed", t_embed.ms)

    # Define filters
    filters: list[dict[str, Any]] = [
        # NOTE: Quality gate: only keep records that actually carry a description. We don't want that
        # {"nested": {"path": "descriptions", "query": {"exists": {"field": "descriptions.description"}}}},
        # TODO: latest indexing does not seems to include resourceTypeGeneral field
        # {
        #     "nested": {
        #         "path": "types",
        #         "query": {"term": {"types.resourceTypeGeneral": "Dataset"}},
        #     }
        # }
    ]

    # Soft date handling: a requested range boosts in-range records instead of excluding out-of-range or
    # undated ones. dates.date is usually the publication date, not the data's temporal coverage, so a hard
    # filter wrongly drops relevant records (e.g. a 1960-2020 dataset published in 2023)
    date_boost_clause: dict[str, Any] | None = None
    if start_date or end_date:
        date_range = {"format": "yyyy-MM-dd"}
        if start_date:
            date_range["gte"] = start_date
        if end_date:
            date_range["lte"] = end_date
        date_boost_clause = {
            "nested": {
                "path": "dates",
                "query": {"range": {"dates.date": date_range}},
                "boost": settings.date_boost,
            }
        }

    # Glucose level changes in the liver of individuals with type 1 diabetes from 1980 to 2020 by Westerink
    if creator_name:
        filters.append(
            {"query_string": {"query": f"*{creator_name}*", "default_field": "_creator", "default_operator": "AND"}}
            # {
            #     "nested": {
            #         "path": "creators",
            #         "query": {
            #             "wildcard": {
            #                 "creators.creatorName": {
            #                     "value": f"*{creator_name}*",
            #                     "case_insensitive": True,
            #                 }
            #             }
            #         },
            #     }
            # }
        )

    emb: dict[str, Any] = {
        "vector": embedding,
        "k": settings.candidate_pool,
    }
    if filters:
        emb["filter"] = {"bool": {"must": filters}}

    keyword_query: dict[str, Any] = {
        "multi_match": {
            "query": search_input,
            "fields": [
                # "titles.title^2",
                "titles.title",
                "descriptions.description",
                "subjects.subject",
                # "subjects.subject^1.5",
                # TODO: add more fields?
            ],
        }
    }
    # NOTE: the description penalty is applied after search on the final hit scores
    # (see below). An in-query function_score is a no-op under the RRF pipeline, which is rank-based
    # and discards score magnitude.
    # Wrap the keyword query when we have hard filters (creator) and/or a soft date boost.
    if filters or date_boost_clause:
        bool_query: dict[str, Any] = {"must": [keyword_query]}
        if filters:
            bool_query["filter"] = filters
        if date_boost_clause:
            bool_query["should"] = [date_boost_clause]
        keyword_query = {"bool": bool_query}

    body = {
        "size": settings.candidate_pool,
        "_source": [
            "titles",
            "subjects",
            "descriptions",
            "url",
            "doi",
            "dates",
            "publicationYear",
            "creators",
            "_harvest_url",
            "_repo",
        ],
        "query": {
            "hybrid": {
                "queries": [
                    {"knn": {"emb": emb}},
                    keyword_query,
                ],
            }
        },
        # Inline search pipeline: min_max normalization makes the weights
        # actually affect ranking (RRF is rank-based and ignores score magnitude)
        "search_pipeline": {
            "phase_results_processors": [
                {
                    "normalization-processor": {
                        "normalization": {"technique": "min_max"},
                        "combination": {
                            "technique": "arithmetic_mean",
                            "parameters": {"weights": settings.hybrid_weights},
                        },
                    }
                }
                ## RRF alternative
                # {
                #     "score-ranker-processor": {
                #         "combination": {
                #         "technique": "rrf",
                #         "rank_constant": 40,
                #         "parameters": { "weights": [0.4, 0.6] }
                #         }
                #     }
                # }
            ]
        },
    }
    # logger.info(f"OpenSearch query body: {json.dumps(body, indent=2)}")
    logger.debug(f"OpenSearch query filters: {json.dumps(filters, indent=2)}")
    with Timer() as t_opensearch:
        try:
            # Run the blocking OpenSearch call in a thread so it doesn't freeze the event loop
            resp = await asyncio.to_thread(
                lambda: opensearch_client.search(
                    index=settings.opensearch_index,
                    body=body,
                    # Deterministic scoring: global term stats (consistent IDF) + pinned shard copies,
                    # so near-tied results don't flip between identical queries (primary/replica IDF divergence)
                    search_type="dfs_query_then_fetch",
                    preference="data-commons-search",
                )
            )
        except Exception as e:
            logger.error(f"OpenSearch query failed: {e}")
            # Degrade to no local hits rather than returning early, so a Zenodo result set still
            # reaches the caller and its task is never left dangling.
            resp = {}
    # Extract hits from OpenSearch response
    raw_hits = resp.get("hits", {}).get("hits", [])

    # Soft description penalty applied on the final score (works under any pipeline, incl. rank-based RRF
    # which ignores in-query score tweaks): demote records without a description, then re-sort.
    if settings.description_penalty < 1.0:
        for hit in raw_hits:
            descriptions = hit.get("_source", {}).get("descriptions") or []
            has_desc = any(isinstance(d, dict) and d.get("description") for d in descriptions)
            if not has_desc and hit.get("_score") is not None:
                hit["_score"] *= settings.description_penalty
        raw_hits = sorted(raw_hits, key=lambda h: h.get("_score") or 0.0, reverse=True)
    # Trim the large candidate pool down to what this input contributes to the rerank pool.
    raw_hits = raw_hits[:limit]
    res = SearchResults(
        total_found=int(resp.get("hits", {}).get("total", {}).get("value", 0)),
        hits=[SearchHit(**hit) for hit in raw_hits],
    )
    record("opensearch", t_opensearch.ms)
    logger.debug(f"search_data: OpenSearch (no rerank) took {t_opensearch.ms:.1f} ms for {len(res.hits)} hits")
    logger.debug(
        "search_data candidates: "
        + " | ".join(f"{h.title!r} ({h.source.repo}) score={h.opensearch_score:.3f}" for h in res.hits)
    )

    # ZENODO FEDERATION: collect the hits of the query started at the top of this function. Returned
    # as its own list, so the merge ranks them against ours instead of appending them at the end.
    if zenodo_task is None:
        return [res]
    with Timer() as t_waited:
        zenodo_res, zenodo_ms = await zenodo_task
    record("zenodo", zenodo_ms)
    # TIMING - comment out this one call to drop the Zenodo/OpenSearch latency comparison.
    # The two queries run concurrently, so `waited` (idle time left once our own search is done)
    # is what Zenodo actually costs a request; `zenodo` alone is just how slow their API was.
    logger.debug(
        f"search_data timing: opensearch {t_opensearch.ms:.0f} ms ({len(res.hits)} hits) | "
        f"zenodo {zenodo_ms:.0f} ms ({len(zenodo_res.hits)} hits, waited {t_waited.ms:.0f} ms)"
    )
    return [res, zenodo_res]


# @mcp.tool()
# async def get_dataset_files(dataset_doi: str) -> FileMetrixFilesResponse:
#     """Get metadata for the files in a dataset (name, description, type, dates).

#     Args:
#         dataset_doi: DOI of the dataset

#     Returns:
#         Search results with a single dataset matching the DOI
#     """
#     # _log_mcp_user()
#     # https://filemetrix.labs.dansdemo.nl/api/v1/10.17026%2FSS%2FR5XWCC
#     async with httpx.AsyncClient(timeout=10.0) as client:
#         resp = await client.get(
#             f"{settings.filemetrix_api}/{quote(dataset_doi, safe='')}",
#             headers={"accept": "application/json"},
#         )
#         if resp.status_code == 200:
#             return FileMetrixFilesResponse.model_validate(resp.json())
#     return FileMetrixFilesResponse(files=[])


def _formats_to_list(value: Any) -> list[str]:
    """Normalize a file-format column (array / jsonb / text) into a flat list of strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return [str(value)]


@mcp.tool()
async def search_tools(
    search_input: list[str], user_question: str, limit: int = settings.search_results_count
) -> SearchResults:
    """Search for tools relevant to the user question

    Args:
        search_input: List of search terms, all searched in one call
        user_question: The user's question, copied as asked (only fix typos and drop filler words).
            Used to rank the results, so do not summarize it or replace it with the search terms
        limit: Number of tools to return (default 10)

    Returns:
        The `limit` most relevant tools (total_found, hits[]) across all search terms
    """
    # _log_mcp_user()
    queries = [q for q in search_input if q and q.strip()]
    if not queries:
        return SearchResults(total_found=0, hits=[])
    limit = max(1, min(limit, MAX_RESULTS_COUNT))
    results = await asyncio.gather(*(_search_tools_one(q, limit) for q in queries))
    return await _rerank_hits(_rerank_query(user_question, queries), _merge_dedup(list(results)), limit)


async def _search_tools_one(search_input: str, limit: int) -> SearchResults:
    """Query the tool registry for a single search term."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{settings.tool_registry_api}/",
                params={"description": search_input},
                headers={"accept": "application/json"},
            )
            resp.raise_for_status()
            rows = resp.json()
    except Exception as e:
        logger.error(f"search_tools registry query failed: {e}")
        return SearchResults(total_found=0, hits=[])

    rows = rows[:limit]
    total = len(rows)
    hits: list[SearchHit] = []
    for rank, row in enumerate(rows):
        name = row.get("name")
        description = row.get("description")
        file_extensions = sorted(
            set(_formats_to_list(row.get("input_file_formats")) + _formats_to_list(row.get("output_file_formats")))
        )
        url = row.get("uri") or row.get("location")
        hits.append(
            SearchHit.model_validate(
                {
                    "_id": url or str(row.get("id")),
                    # The registry returns results already ordered by relevance but without a score, so
                    # derive a descending score from the rank to preserve ordering downstream.
                    "_score": float(total - rank),
                    "_source": {
                        "url": url,
                        "titles": [{"title": name}] if name else [],
                        "descriptions": [{"description": description}] if description else [],
                        "resourceType": "tool",
                    },
                    "fileExtensions": file_extensions,
                }
            )
        )
    logger.debug(f"search_tools: {total} tools matched for {search_input!r}")
    return SearchResults(total_found=total, hits=hits)


# @mcp.tool()
# async def search_citations(items_id: list[str]) -> OpenSearchResults:
#     """Search for citations relevant to datasets and/or tools by DOI or URL

#     Args:
#         items_id: List of DOIs or URLs of datasets/tools

#     Returns:
#         Search results with a list of citations relevant to the request
#     """
#     search_results = {
#         "total_found": 1,
#         "hits": [
#             {
#                 "_id": "https://doi.org/10.1109/MSR.2019.00077",
#                 "_score": 0.8,
#                 "_source": {
#                     "titles": [
#                         {
#                             "title": "A Large-Scale Study About Quality and Reproducibility of Jupyter Notebooks",
#                             "lang": "en",
#                         }
#                     ],
#                     "descriptions": [
#                         {
#                             "description": "Jupyter Notebooks have been widely adopted by many different communities, both in science and industry. They support the creation of literate programming documents that combine code, text, and execution results with visualizations and all sorts of rich media. The self-documenting aspects and the ability to reproduce results have been touted as significant benefits of notebooks. At the same time, there has been growing criticism that the way notebooks are being used leads to unexpected behavior, encourage poor coding practices, and that their results can be hard to reproduce. To understand good and bad practices used in the development of real notebooks, we studied 1.4 million notebooks from GitHub. We present a detailed analysis of their characteristics that impact reproducibility. We also propose a set of best practices that can improve the rate of reproducibility and discuss open challenges that require further research and development.",
#                             "lang": "en",
#                         }
#                     ],
#                     "url": "https://doi.org/10.1109/MSR.2019.00077",
#                     "doi": "10.1109/MSR.2019.00077",
#                     "dates": [{"date": "2019-08-29", "dateType": "Issued"}],
#                     "publicationYear": "2019 ",
#                     "creators": [{"creatorName": "Lastname, Firstname"}],
#                 },
#             }
#         ],
#     }
#     return OpenSearchResults.model_validate(search_results)


def cli() -> None:
    """Run the MCP server with appropriate transport."""
    parser = argparse.ArgumentParser(
        description="A Model Context Protocol (MCP) server for BioData resources at the SIB."
    )
    parser.add_argument("--http", action="store_true", help="Use Streamable HTTP transport")
    parser.add_argument("--port", type=int, default=8888, help="Port to run the server on")
    args = parser.parse_args()
    if args.http:
        mcp.run()
        mcp.settings.port = args.port
        mcp.settings.log_level = "INFO"
        mcp.run(transport="streamable-http")
    else:
        mcp.run()
