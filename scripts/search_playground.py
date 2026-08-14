"""Quick relevance playground that calls the real `search_data` MCP tool.

Usage:
    uv run --env-file keys.env scripts/search_playground.py "Data about CO2 levels in europe between 1960 and 2020" --start 1960-01-01 --end 2025-12-31
    uv run --env-file keys.env scripts/search_playground.py "CO2 levels Europe"

Point it at the right database by setting POSTGRES_HOST (or postgres_host in keys.env),
e.g. POSTGRES_HOST=localhost uv run python scripts/search_playground.py "..."
"""

from __future__ import annotations

import argparse
import asyncio
import textwrap

from data_commons_search.config import settings
from data_commons_search.mcp_server import search_data


async def run(query: str, start: str | None, end: str | None, creator: str | None) -> None:
    print(f"PostgreSQL: {settings.postgres_host}/{settings.postgres_db}")
    res = await search_data(query, start_date=start, end_date=end, creator_name=creator)

    print(f"\n{'=' * 78}")
    print(f"  query : {query}")
    print(f"  model : {settings.embedding_model}   candidates: {res.total_found}")
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
