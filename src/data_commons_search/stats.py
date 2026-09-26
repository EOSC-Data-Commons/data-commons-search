"""Compute aggregated stats about the harvested records in datasetdb.

The app user has no access to datasetdb in prod, so the stats are pre-computed offline by
`scripts/compute_stats.py`, run with POSTGRES_* credentials that can read datasetdb; each run
appends a row to the `stats_snapshots` table in appdb. The `/stats`
endpoint serves the latest snapshot from there (see `db.get_latest_stats`), so stats can be
refreshed without redeploying, and the table keeps the history of growth over time.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from data_commons_search import __version__
from data_commons_search.config import settings
from data_commons_search.models import DbStats, RepositoryStats, SubjectCount

# The upstream `repositories.code` value is used verbatim as the short display label for each
# repository (e.g. on the landing-page "Datasets per repository" chart). A few of those codes are
# stale/abbreviated in the source table; override them here so /stats reports each repository's
# canonical short name. SwissUbase's code, for instance, is stored as "SWISS" — which is what users
# saw mislabelled on the chart.
REPOSITORY_CODE_OVERRIDES = {
    "SWISS": "SWISSUbase",
}


def _display_code(code: str) -> str:
    """Map an upstream repository code to its canonical short display code (identity if none)."""
    return REPOSITORY_CODE_OVERRIDES.get(code, code)


# Records count + dataset count per repository. LEFT JOIN so repos with zero harvested
# records still appear (record_count = 0 = not yet harvested).
REPOSITORY_STATS_SQL = """
SELECT
    rep.code,
    rep.name,
    COUNT(r.id)                                          AS record_count,
    COUNT(*) FILTER (WHERE r.resource_type = 'Dataset')  AS datasets,
    COUNT(DISTINCT r.endpoint_id)                        AS endpoints_with_records,
    COUNT(r.id) FILTER (WHERE r.opensearch_synced)       AS synced_to_opensearch,
    MAX(r.datestamp)                                     AS latest_record_datestamp
FROM repositories rep
LEFT JOIN records r ON r.repository_id = rep.id
GROUP BY rep.id, rep.code, rep.name
ORDER BY datasets DESC, record_count DESC;
"""

# Top subjects (keywords) per repository, counted over Dataset records. Subjects live inside the
# datacite_json->'subjects' array; we unnest it defensively (records with no/invalid subjects array
# contribute nothing) and rank per repository, keeping the top N.
TOP_SUBJECTS_SQL = """
WITH subject_counts AS (
    SELECT
        rep.code                              AS code,
        nullif(trim(s.elem->>'subject'), '')  AS subject,
        COUNT(*)                              AS cnt
    FROM records r
    JOIN repositories rep ON r.repository_id = rep.id
    CROSS JOIN LATERAL jsonb_array_elements(
        CASE WHEN jsonb_typeof(r.datacite_json->'subjects') = 'array'
             THEN r.datacite_json->'subjects'
             ELSE '[]'::jsonb END
    ) AS s(elem)
    WHERE r.resource_type = 'Dataset'
    GROUP BY rep.code, nullif(trim(s.elem->>'subject'), '')
),
ranked AS (
    SELECT
        code, subject, cnt,
        ROW_NUMBER() OVER (PARTITION BY code ORDER BY cnt DESC, subject) AS rn
    FROM subject_counts
    WHERE subject IS NOT NULL
)
SELECT code, subject, cnt
FROM ranked
WHERE rn <= :top_n
ORDER BY code, cnt DESC, subject;
"""


def compute_stats() -> DbStats:
    """Query datasetdb and build the aggregated `DbStats` (per-repository counts + top subjects)."""
    # Records live in datasetdb, while POSTGRES_DB stays on appdb (where the snapshot is written)
    engine = create_engine(settings.datasetdb_url, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            repo_rows = conn.execute(text(REPOSITORY_STATS_SQL)).mappings().all()
            subject_rows = conn.execute(text(TOP_SUBJECTS_SQL), {"top_n": settings.stats_top_subjects}).mappings().all()
    finally:
        engine.dispose()

    # Group subjects by repository code
    subjects_by_code: dict[str, list[SubjectCount]] = {}
    for row in subject_rows:
        subjects_by_code.setdefault(row["code"], []).append(SubjectCount(subject=row["subject"], count=row["cnt"]))

    repositories = [
        RepositoryStats(
            code=_display_code(row["code"]),
            name=row["name"],
            record_count=row["record_count"],
            datasets=row["datasets"],
            endpoints_with_records=row["endpoints_with_records"],
            synced_to_opensearch=row["synced_to_opensearch"],
            latest_record_datestamp=row["latest_record_datestamp"],
            top_subjects=subjects_by_code.get(row["code"], []),
        )
        for row in repo_rows
    ]
    return DbStats(
        api_version=__version__,
        generated_at=datetime.now(timezone.utc),
        total_records=sum(r.record_count for r in repositories),
        total_datasets=sum(r.datasets for r in repositories),
        repositories=repositories,
    )
