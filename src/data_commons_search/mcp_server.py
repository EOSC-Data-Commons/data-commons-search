import argparse
import asyncio
from typing import Any
from urllib.parse import quote

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.fastmcp import FastMCP

from data_commons_search.config import settings
from data_commons_search.models import (
    FileMetrixFilesResponse,
    SearchHit,
    SearchResults,
    UserInfo,
)
from data_commons_search.search import search_datasets
from data_commons_search.utils import Timer, logger, timed
from data_commons_search.zenodo import merge_results, search_zenodo_datasets  # ZENODO FEDERATION

# Create MCP server https://github.com/modelcontextprotocol/python-sdk
mcp = FastMCP(
    name="EOSC Data Commons MCP",
    debug=settings.debug_enabled,
    dependencies=["mcp", "httpx", "sqlalchemy", "pydantic"],
    instructions="Provide tools that helps users access data from various open-access data publishers, developed for the EOSC Data Commons project.",
    json_response=True,
    stateless_http=True,
    streamable_http_path="/",
)


def _log_mcp_user() -> UserInfo | None:
    """Return the authenticated MCP caller for a tool, if the request carried a valid bearer token.

    Returns the resolved ``UserInfo`` when authenticated (None otherwise) so tools can later tailor
    behaviour (e.g. user preferences). For now identity is purely informational.
    """
    token = get_access_token()
    return getattr(token, "userinfo", None) if token else None


@mcp.tool()
async def search_data(
    search_input: str, start_date: str | None = None, end_date: str | None = None, creator_name: str | None = None
) -> SearchResults:
    """Search for datasets relevant to the user question.

    Args:
        search_input: Natural language search input
        start_date: Optional start date in yyyy-MM-dd
        end_date: Optional end date in yyyy-MM-dd
        creator_name: Optional creator name to filter by

    Returns:
        Hybrid search results from PostgreSQL (total_found, hits[])
    """
    user = _log_mcp_user()
    if user:
        logger.info(f"Tool call `search_data` by user '{user.preferred_username or user.sub}'")

    # ZENODO FEDERATION - started first so it runs while we embed the query and search postgres.
    zenodo_task = (
        asyncio.create_task(timed(search_zenodo_datasets(search_input, start_date, end_date, creator_name)))
        if settings.zenodo_search_enabled
        else None
    )

    with Timer() as t_search:
        try:
            res = await search_datasets(search_input, start_date, end_date, creator_name)
        except Exception as e:
            logger.error(f"PostgreSQL hybrid search failed: {e}")
            # Degrade to no local hits rather than returning early, so a Zenodo result set still
            # reaches the caller and its task is never left dangling.
            res = SearchResults(total_found=0, hits=[])
    logger.debug(f"search_data: postgres hybrid search took {t_search.ms:.1f} ms for {len(res.hits)} hits")
    logger.debug(
        "search_data candidates: "
        + " | ".join(f"{h.title!r} ({h.source.repo}) score={h.opensearch_score:.3f}" for h in res.hits)
    )

    # ZENODO FEDERATION - append the hits from the query started at the top of this function.
    if zenodo_task is not None:
        with Timer() as t_waited:
            zenodo_res, zenodo_ms = await zenodo_task
        # TIMING - comment out this one call to drop the Zenodo/postgres latency comparison.
        # The two queries run concurrently, so `waited` (idle time left once our own search is done)
        # is what Zenodo actually costs a request; `zenodo` alone is just how slow their API was.
        logger.debug(
            f"search_data timing: postgres {t_search.ms:.0f} ms ({len(res.hits)} hits) | "
            f"zenodo {zenodo_ms:.0f} ms ({len(zenodo_res.hits)} hits, waited {t_waited.ms:.0f} ms)"
        )
        res = merge_results(res, zenodo_res)
    return res


@mcp.tool()
async def get_dataset_files(dataset_doi: str) -> FileMetrixFilesResponse:
    """Get metadata for the files in a dataset (name, description, type, dates).

    Args:
        dataset_doi: DOI of the dataset

    Returns:
        Search results with a single dataset matching the DOI
    """
    # _log_mcp_user()
    # https://filemetrix.labs.dansdemo.nl/api/v1/10.17026%2FSS%2FR5XWCC
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{settings.filemetrix_api}/{quote(dataset_doi, safe='')}",
            headers={"accept": "application/json"},
        )
        if resp.status_code == 200:
            return FileMetrixFilesResponse.model_validate(resp.json())
    return FileMetrixFilesResponse(files=[])


def _formats_to_list(value: Any) -> list[str]:
    """Normalize a file-format column (array / jsonb / text) into a flat list of strings."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if v]
    return [str(value)]


@mcp.tool()
async def search_tools(search_input: str) -> SearchResults:
    """Search for tools relevant to the user question

    Args:
        search_input: search terms

    Returns:
        Search results with a list of tools relevant to the question
    """
    # _log_mcp_user()
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

    rows = rows[: settings.search_results_count]
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
