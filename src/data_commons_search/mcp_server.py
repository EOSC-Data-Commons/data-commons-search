import argparse
import json
import math
import time
from typing import Any
from urllib.parse import quote

import httpx
from fastembed import TextEmbedding
from fastembed.rerank.cross_encoder import TextCrossEncoder
from mcp.server.fastmcp import FastMCP
from opensearchpy import OpenSearch

from data_commons_search.config import settings
from data_commons_search.models import (
    FileMetrixFilesResponse,
    OpenSearchResults,
    SearchHit,
)
from data_commons_search.utils import logger

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
reranker_model = TextCrossEncoder(model_name=settings.reranker_model)
opensearch_client = OpenSearch(hosts=[settings.opensearch_url])

RERANK_TOP_K = 30


# https://github.com/EOSC-Data-Commons/metadata-warehouse/blob/main/src/config/opensearch_mapping.json


@mcp.tool()
async def search_data(
    search_input: str, start_date: str | None = None, end_date: str | None = None, creator_name: str | None = None
) -> OpenSearchResults:
    """Search for datasets relevant to the user question.

    Args:
        search_input: Natural language search input
        start_date: Optional start date in yyyy-MM-dd
        end_date: Optional end date in yyyy-MM-dd
        creator_name: Optional creator name to filter by

    Returns:
        Results from OpenSearch (total_found, hits[])
    """
    # Generate embedding for the query
    embedding = next(iter(embedding_model.embed([search_input])))
    # embedding = next(iter(embedding_model.embed([f"passage: {question}"])))

    # Define filters
    filters = [
        # TODO: latest indexing does not seems to include resourceTypeGeneral field
        # {
        #     "nested": {
        #         "path": "types",
        #         "query": {"term": {"types.resourceTypeGeneral": "Dataset"}},
        #     }
        # }
    ]
    logger.debug(
        f"Search: `{search_input}` | start_date: {start_date} | end_date: {end_date} | creator_name: {creator_name}"
    )

    if start_date or end_date:
        date_range = {"format": "yyyy-MM-dd"}
        if start_date:
            date_range["gte"] = start_date
        if end_date:
            date_range["lte"] = end_date
        filters.append(
            {
                "nested": {
                    "path": "dates",
                    "query": {"range": {"dates.date": date_range}},
                }
            }
        )

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
        "vector": embedding.tolist(),
        "k": settings.search_results_count,
    }
    if filters:
        emb["filter"] = {"bool": {"must": filters}}
    body = {
        "size": settings.search_results_count,
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
            "knn": {
                "emb": emb,
            }
        },
    }
    # logger.info(f"OpenSearch query body: {json.dumps(body, indent=2)}")
    logger.debug(f"OpenSearch query filters: {json.dumps(filters, indent=2)}")
    t_search_start = time.perf_counter()
    try:
        resp = opensearch_client.search(index=settings.opensearch_index, body=body)
    except Exception as e:
        logger.error(f"OpenSearch query failed: {e}")
        return OpenSearchResults(total_found=0, hits=[])
    t_search_elapsed = time.perf_counter() - t_search_start
    # Extract hits from OpenSearch response
    res = OpenSearchResults(
        total_found=int(resp.get("hits", {}).get("total", {}).get("value", 0)),
        hits=[SearchHit(**hit) for hit in resp.get("hits", {}).get("hits", [])],
    )
    logger.debug(f"search_data: OpenSearch (no rerank) took {t_search_elapsed * 1000:.1f} ms for {len(res.hits)} hits")

    # Cross-encoder reranking
    if res.hits:
        # t_rerank_start = time.perf_counter()
        documents = []
        for hit in res.hits:
            title = hit.title if hit.title is not None else ""
            description = hit.description if hit.description is not None else ""
            documents.append(f"{title}\n{description}".strip())
        # scores = list(reranker_model.rerank(search_input, documents))
        # for hit, score in zip(res.hits, scores, strict=True):
        #     hit.score = float(score)
        # reranked = sorted(res.hits, key=lambda h: h.score if h.score is not None else float("-inf"), reverse=True)
        scores = [float(s) for s in reranker_model.rerank(search_input, documents)]
        lo, hi = min(scores), max(scores)
        # Sigmoid with temperature so extremes don't saturate to exactly 0 or 1.
        # Temperature is derived from the observed range to keep the spread informative
        # regardless of the model's logit magnitude.
        mid = (hi + lo) / 2
        temperature = max((hi - lo) / 8, 1e-6)
        for hit, score in zip(res.hits, scores, strict=True):
            hit.score = 1.0 / (1.0 + math.exp(-(score - mid) / temperature))
        reranked = sorted(res.hits, key=lambda h: h.score if h.score is not None else float("-inf"), reverse=True)
        res.hits = reranked
        # t_rerank_elapsed = time.perf_counter() - t_rerank_start
        # logger.info(
        #     f"search_data: cross-encoder rerank of top {len(top_hits)} took {t_rerank_elapsed * 1000:.1f} ms "
        #     f"(total search+rerank: {(t_search_elapsed + t_rerank_elapsed) * 1000:.1f} ms)"
        # )
    return res


@mcp.tool()
async def get_dataset_files(dataset_doi: str) -> FileMetrixFilesResponse:
    """Get metadata for the files in a dataset (name, description, type, dates).

    Args:
        dataset_doi: DOI of the dataset

    Returns:
        Search results with a single dataset matching the DOI
    """
    # https://filemetrix.labs.dansdemo.nl/api/v1/10.17026%2FSS%2FR5XWCC
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{settings.filemetrix_api}/{quote(dataset_doi, safe='')}",
            headers={"accept": "application/json"},
        )
        if resp.status_code == 200:
            return FileMetrixFilesResponse.model_validate(resp.json())
    return FileMetrixFilesResponse(files=[])


@mcp.tool()
async def search_tools(search_input: str) -> OpenSearchResults:
    """Search for tools relevant to the user question

    Args:
        search_input: Natural language search input

    Returns:
        Search results with a list of tools and services relevant to the question
    """
    search_results = {
        "total_found": 1,
        "hits": [
            {
                "_id": "https://jupyter.org/",
                "_score": 0.8,
                "_source": {
                    "titles": [{"title": "JupyterLab", "lang": "en"}],
                    "descriptions": [{"description": "Notebooks", "lang": "en"}],
                    "url": "https://jupyter.org/",
                    "doi": None,
                    "dates": [{"date": "2016-08-29", "dateType": "Issued"}],
                    "publicationYear": "2016",
                    "creators": [{"creatorName": "Lastname, Firstname"}],
                },
            }
        ],
    }
    return OpenSearchResults.model_validate(search_results)


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
