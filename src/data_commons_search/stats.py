"""Compute and serve aggregated stats about the harvested records in datasetdb.

The app user has no access to datasetdb in prod, so the stats are pre-computed offline by
`scripts/compute_stats.py` (point the POSTGRES_* vars in keys.env at datasetdb with a
read-capable user) and written to `stats.json` next to this module. The `/stats` endpoint then
serves that file, which ships with the package/image (the `data/` dir is git/docker-ignored, so it
cannot live there).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

from data_commons_search import __version__
from data_commons_search.config import settings
from data_commons_search.models import DbStats, RepositoryStats, SubjectCount
from data_commons_search.utils import logger

# Pre-computed stats file, shipped with the package (see module docstring).
STATS_FILE = Path(__file__).parent / "stats.json"


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
    engine = create_engine(settings.postgres_url, pool_pre_ping=True)
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
            code=row["code"],
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


def save_stats(stats: DbStats, path: Path = STATS_FILE) -> Path:
    """Write the computed stats to `path` as indented JSON."""
    path.write_text(stats.model_dump_json(indent=2))
    return path


def load_stats(path: Path = STATS_FILE) -> DbStats | None:
    """Load pre-computed stats from `path`, or `None` if the file is missing/invalid."""
    if not path.exists():
        logger.warning(f"Stats file not found at {path}; run scripts/compute_stats.py to generate it")
        return None
    try:
        stats = DbStats.model_validate_json(path.read_text())
    except Exception as exc:
        logger.error(f"Failed to load stats from {path}: {exc}")
        return None
    stats.api_version = __version__
    return stats
