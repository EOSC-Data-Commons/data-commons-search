"""Reusable OpenSearch hybrid-search playground.

Test relevance directly against the cluster WITHOUT restarting the MCP service:
generate the query embedding with fastembed, build the same hybrid query the app
sends, run it, and pretty-print the results.

Usage:
    uv run --env-file keys.env scripts/search_test.py "Data about CO2 levels in europe between 1960 and 2020" --start 1960-01-01 --end 2020-12-31
    uv run --env-file keys.env scripts/search_test.py "CO2 levels Europe" --start 1960-01-01 --end 2020-12-31

Point it at the right cluster by setting OPENSEARCH_URL (or opensearch_url in keys.env),
e.g. OPENSEARCH_URL=http://localhost:9200 uv run python scripts/search_test.py "..."
"""

from __future__ import annotations

import argparse
import textwrap
from typing import Any

from fastembed import TextEmbedding
from opensearchpy import OpenSearch

from data_commons_search.config import settings

# --- knobs you will most often tweak ------------------------------------------------
KEYWORD_FIELDS = ["titles.title", "descriptions.description", "subjects.subject"]
RESULT_COUNT = 20  # how many results to display
# knn candidate pool. Must be MUCH larger than RESULT_COUNT so near-duplicates aren't dropped and
# min_max normalization is computed over a stable, rich set (a tiny pool makes scores swing wildly).
CANDIDATE_POOL = 100
# ------------------------------------------------------------------------------------

client = OpenSearch(hosts=[settings.opensearch_url])
embedder = TextEmbedding(settings.embedding_model)


def build_body(
    query: str,
    knn_w: float,
    kw_w: float,
    use_pipeline: bool,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any]:
    vector = next(iter(embedder.embed([query]))).tolist()

    keyword_query: dict[str, Any] = {"multi_match": {"query": query, "fields": KEYWORD_FIELDS}}

    # NOTE: the description penalty CANNOT be expressed in-query. An in-channel function_score is (a) a
    # no-op under min_max normalization (scale-invariant per channel) and (b) blind to the knn channel,
    # where the junk actually lives. It is applied post-search on the final combined score instead
    # (see demote_no_description) - the only layer where it survives normalization and spans both channels.

    # Soft date boost (mirrors mcp_server): boost in-range records, never exclude.
    if start_date or end_date:
        date_range: dict[str, str] = {"format": "yyyy-MM-dd"}
        if start_date:
            date_range["gte"] = start_date
        if end_date:
            date_range["lte"] = end_date
        keyword_query = {
            "bool": {
                "must": [keyword_query],
                "should": [
                    {
                        "nested": {
                            "path": "dates",
                            "query": {"range": {"dates.date": date_range}},
                            "boost": settings.date_boost,
                        }
                    }
                ],
            }
        }

    body: dict[str, Any] = {
        "size": CANDIDATE_POOL,
        "_source": ["titles", "descriptions", "subjects", "_repo", "publicationYear"],
        "query": {
            "hybrid": {
                "queries": [
                    {"knn": {"emb": {"vector": vector, "k": CANDIDATE_POOL}}},
                    keyword_query,
                ]
            }
        },
    }
    if use_pipeline:
        # min_max normalization -> scores in [0,1] AND weights actually affect ranking (unlike RRF,
        # which is rank-based and ignores score magnitude). knn surfaces real datasets, keyword surfaces
        # literal-"CO2" junk, so weighting knn higher suppresses the junk.
        body["search_pipeline"] = {
            "phase_results_processors": [
                {
                    "normalization-processor": {
                        "normalization": {"technique": "min_max"},
                        "combination": {
                            "technique": "arithmetic_mean",
                            "parameters": {"weights": [knn_w, kw_w]},
                        },
                    }
                }
            ]
        }
    return body


def demote_no_description(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Multiply the final _score of records lacking a description by settings.description_penalty,
    then re-sort. Works regardless of the combination pipeline (RRF or normalization) because it
    operates on the final returned score, not the per-channel score the pipeline discards."""
    if settings.description_penalty >= 1.0:
        return hits
    for h in hits:
        has_desc = bool(first(h.get("_source", {}), "descriptions", "description"))
        if not has_desc:
            h["_score"] = (h.get("_score") or 0.0) * settings.description_penalty
    return sorted(hits, key=lambda h: h.get("_score") or 0.0, reverse=True)


def first(src: dict[str, Any], key: str, sub: str) -> str:
    val = src.get(key)
    if isinstance(val, list) and val and isinstance(val[0], dict):
        return str(val[0].get(sub, ""))
    return ""


def pretty(query: str, body: dict[str, Any]) -> list[str]:
    # dfs_query_then_fetch -> global term stats (consistent IDF across shard copies);
    # preference -> pin to the same copies. Together they make scoring deterministic across
    # identical runs (otherwise primary/replica IDF differences flip near-tied results).
    resp = client.search(
        index=settings.opensearch_index,
        body=body,
        search_type="dfs_query_then_fetch",
        preference="data-commons-search",
    )
    hits = demote_no_description(resp.get("hits", {}).get("hits", []))[:RESULT_COUNT]
    total = resp.get("hits", {}).get("total", {}).get("value", 0)

    print(f"\n{'=' * 78}")
    print(f"  query : {query}")
    print(f"  index : {settings.opensearch_index}   total matched: {total}")
    print(f"{'=' * 78}")
    titles = []
    for i, h in enumerate(hits, 1):
        src = h.get("_source", {})
        title = first(src, "titles", "title") or "(no title)"
        desc = first(src, "descriptions", "description")
        repo = src.get("_repo", "?")
        year = src.get("publicationYear", "")
        flag = "OK  " if desc else "NODESC"
        titles.append(title)
        print(f"\n  #{i:<2} score={h.get('_score'):.4f}  {flag}  · {repo} · {year}")
        print(f"      {title[:90]}")
        if desc:
            print(textwrap.fill(desc[:220], width=86, initial_indent="      > ", subsequent_indent="        "))
    return titles


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="Data about CO2 levels in europe between 1960 and 2020")
    ap.add_argument("--knn", type=float, default=settings.hybrid_weights[0])
    ap.add_argument("--kw", type=float, default=settings.hybrid_weights[1])
    ap.add_argument("--start", default=None, help="start date yyyy-MM-dd (soft boost, not filter)")
    ap.add_argument("--end", default=None, help="end date yyyy-MM-dd (soft boost, not filter)")
    ap.add_argument("--no-pipeline", action="store_true")
    ap.add_argument("--diagnose", action="store_true", help="check if the inline pipeline is honored at all")
    args = ap.parse_args()

    print(f"OpenSearch: {settings.opensearch_url}")
    s = client.indices.get_settings(index=settings.opensearch_index)
    idx = next(iter(s.values())).get("settings", {}).get("index", {})
    default_pipeline = idx.get("search", {}).get("default_pipeline")
    print(
        f"index.search.default_pipeline = {default_pipeline!r}"
        + ("   <-- may override our inline pipeline!" if default_pipeline else "")
    )

    if args.diagnose:
        a = pretty(args.query + "  [weights 1,0 knn-only]", build_body(args.query, 1.0, 0.0, True))
        b = pretty(args.query + "  [weights 0,1 kw-only]", build_body(args.query, 0.0, 1.0, True))
        print(f"\n{'=' * 78}")
        if a == b:
            print("  VERDICT: identical top hits despite opposite weights ->")
            print("           the inline search_pipeline is being IGNORED by the cluster.")
            print("           Fix the cluster side (remove index default_pipeline), not the query.")
        else:
            print("  VERDICT: weights change results -> inline pipeline IS honored.")
            print("           So tune weights/penalty here, then mirror into mcp_server.py.")
        print(f"{'=' * 78}")
        return

    pretty(args.query, build_body(args.query, args.knn, args.kw, not args.no_pipeline, args.start, args.end))


if __name__ == "__main__":
    main()
