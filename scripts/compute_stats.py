"""Compute the datasetdb stats served by the /stats endpoint and write them to stats.json.

The app user has no access to datasetdb in prod, so run this offline against datasetdb with a
read-capable user. Point the existing POSTGRES_* vars in keys.env (or export them) at datasetdb,
e.g.:

    POSTGRES_HOST=...
    POSTGRES_USER=...
    POSTGRES_PASSWORD=...
    POSTGRES_DB=datasetdb

Then run:

    uv run scripts/compute_stats.py

The output is written to src/data_commons_search/stats.json (shipped with the package/image so
/stats can serve it in prod). Pass an output path to override:

    uv run scripts/compute_stats.py /path/to/stats.json
"""

import sys
from pathlib import Path

from data_commons_search.config import settings
from data_commons_search.stats import STATS_FILE, compute_stats, save_stats


def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else STATS_FILE
    print(f"Querying {settings.postgres_host}:{settings.postgres_port}/{settings.postgres_db} ...")
    stats = compute_stats()
    save_stats(stats, out_path)
    print(
        f"Wrote {out_path} · {len(stats.repositories)} repositories · "
        f"{stats.total_datasets} datasets · {stats.total_records} records"
    )
    for repo in stats.repositories:
        top = ", ".join(f"{s.subject} ({s.count})" for s in repo.top_subjects[:5])
        print(f"  {repo.code:<12} datasets={repo.datasets:<8} top subjects: {top}")


if __name__ == "__main__":
    main()
