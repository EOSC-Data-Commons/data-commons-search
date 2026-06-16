"""Quick relevance playground that calls the real `search_data` MCP tool.

Usage:
    uv run --env-file keys.env scripts/search_test.py "Data about CO2 levels in europe between 1960 and 2020" --start 1960-01-01 --end 2025-12-31
    uv run --env-file keys.env scripts/search_test.py "CO2 levels Europe"

Point it at the right cluster by setting OPENSEARCH_URL (or opensearch_url in keys.env),
e.g. OPENSEARCH_URL=http://localhost:9200 uv run python scripts/search_test.py "..."
"""

from __future__ import annotations

import argparse
import asyncio
import textwrap

from data_commons_search.config import settings
from data_commons_search.mcp_server import search_data


async def run(query: str, start: str | None, end: str | None, creator: str | None) -> None:
    print(f"OpenSearch: {settings.opensearch_url}")
    res = await search_data(query, start_date=start, end_date=end, creator_name=creator)

    print(f"\n{'=' * 78}")
    print(f"  query : {query}")
    print(f"  index : {settings.opensearch_index}   total matched: {res.total_found}")
    print(f"{'=' * 78}")
    for i, hit in enumerate(res.hits, 1):
        title = hit.title or "(no title)"
        desc = hit.description
        repo = hit.source.repo or "?"
        year = hit.source.publication_year or ""
        flag = "OK  " if desc else "NODESC"
        print(f"\n  #{i:<2} score={hit.opensearch_score:.4f}  {flag}  · {repo} · {year}")
        print(f"      {title[:90]}")
        if desc:
            print(textwrap.fill(desc[:220], width=86, initial_indent="      > ", subsequent_indent="        "))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("query", nargs="?", default="Data about CO2 levels in europe between 1960 and 2020")
    ap.add_argument("--start", default=None, help="start date yyyy-MM-dd (soft boost, not filter)")
    ap.add_argument("--end", default=None, help="end date yyyy-MM-dd (soft boost, not filter)")
    ap.add_argument("--creator", default=None, help="filter by creator name")
    args = ap.parse_args()

    asyncio.run(run(args.query, args.start, args.end, args.creator))


if __name__ == "__main__":
    main()
