"""Compute the datasetdb stats served by the /stats endpoint and append them to appdb.

Reads the harvested records from DATASETDB_NAME (default "datasetdb") and appends one row to the
`stats_snapshots` table of POSTGRES_DB (default "appdb"), on the same host with the same
credentials. So /stats picks up the new numbers without a redeploy, and the table keeps the
history of growth over time.

Run offline as the ingest role (POSTGRES_APPDB_INGEST_USER and _PASSWORD), which reads datasetdb and
writes the stats_snapshots table (the search role of the API only reads it):

    uv run --env-file .staging.env scripts/compute_stats.py

Pass --dry-run to print the computed stats without writing them.
"""

import sys

from data_commons_search.config import settings
from data_commons_search.db import store_stats_snapshot
from data_commons_search.stats import compute_stats


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    print(f"Querying {settings.postgres_host}:{settings.postgres_port}/{settings.datasetdb_name} ...")
    stats = compute_stats()
    if dry_run:
        print("Dry run, nothing written")
    else:
        store_stats_snapshot(stats)
        print(f"Appended snapshot to {settings.postgres_db}.stats_snapshots at {stats.generated_at.isoformat()}")
    print(f"{len(stats.repositories)} repositories · {stats.total_datasets} datasets · {stats.total_records} records")
    for repo in stats.repositories:
        top = ", ".join(f"{s.subject} ({s.count})" for s in repo.top_subjects[:5])
        print(f"  {repo.code:<12} datasets={repo.datasets:<8} top subjects: {top}")


if __name__ == "__main__":
    main()
