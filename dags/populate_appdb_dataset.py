"""datasetdb.records -> appdb.dataset, first DAG of the search pipeline.

First creates the missing tables of appdb (created empty by the metadata-warehouse), then transforms the records updated since the last successful run (manually triggered for now, daily
once deployed). Its `appdb_dataset_table` asset event triggers generate_embeddings. The task is skipped,
which stops the pipeline there, when no dataset is waiting for embeddings. The check is on the queue,
not on the changes of this run, so datasets left over by a failed embeddings run still trigger it.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from airflow.sdk import Param, dag, get_current_context, task
from airflow.sdk.exceptions import AirflowSkipException
from search_pipeline.assets import DATASET_TABLE
from search_pipeline.config import get_settings
from search_pipeline.db import appdb_engine
from search_pipeline.embeddings import queued_count
from search_pipeline.transform import has_datasets, populate_appdb_dataset
from sqlalchemy import text

from data_commons_search import dataset_models, db_models

# Records committed in datasetdb after the previous run started can carry an older updated_at
OVERLAP = timedelta(hours=6)


@dag(
    dag_id="populate_appdb_dataset",
    description="Transform the datasetdb records into the appdb dataset table",
    # Manual for now, unpausing a scheduled DAG starts a run of all repositories right away.
    # "@daily" once deployed
    schedule=None,
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    # Unpaused even where airflow pauses new DAGs, safe without a schedule
    is_paused_upon_creation=False,
    params={
        "full": Param(
            False,
            type="boolean",
            description="Re-read every datasetdb record, not only the ones updated since the last run. "
            "Nothing is dropped: datasets are upserted in place, then the datasets whose record was "
            "removed from datasetdb are deleted with their embeddings (only in the selected "
            "repositories, not done when limit is set)",
        ),
        "limit": Param(None, type=["null", "integer"], description="Stop after this many records, for testing"),
        "repositories": Param(
            [],
            type="array",
            items={"type": "string"},
            description="Only these datasetdb repository codes (e.g. DASCH, FINBIF), all when empty",
        ),
    },
    tags=["search"],
)
def populate_appdb_dataset_dag() -> None:
    @task
    def create_appdb_tables() -> None:
        """Create the missing appdb tables from the SQLAlchemy models, as the ingest role which then owns them.

        The deferred search indexes are left to rebuild_indexes. Roles and extensions: scripts/init_roles.sql
        """
        with appdb_engine(get_settings()).begin() as conn:
            # Functions used by the generated columns and the view
            conn.execute(text(dataset_models.JSONB_ARRAY_JOIN_DDL))
            conn.execute(text(dataset_models.CREATOR_KEY_DDL))
            db_models.Base.metadata.create_all(conn)
            dataset_models.Base.metadata.create_all(conn)
            for ddl in dataset_models.CREATOR_NAME_DDL:
                conn.execute(text(ddl))

    @task(outlets=[DATASET_TABLE])
    def transform() -> dict[str, Any]:
        context = get_current_context()
        params = context["params"]
        settings = get_settings()
        repositories = params["repositories"] or None
        # A lazy proxy, `is None` is never true on it, its truthiness is the wrapped value's
        previous = context.get("prev_start_date_success")
        # Full read on the first run, and when appdb was reset (or the repositories never loaded) since
        incremental = previous and not params["full"] and has_datasets(settings, repositories)
        since = previous - OVERLAP if incremental else None
        stats = populate_appdb_dataset(settings, since=since, limit=params["limit"], repositories=repositories)
        # A skipped task emits no asset event, so the next DAGs do not run
        if not queued_count(settings):
            raise AirflowSkipException(f"No dataset to embed: {stats.as_dict()}")
        context["outlet_events"][DATASET_TABLE].extra = stats.as_dict()
        return stats.as_dict()

    create_appdb_tables() >> transform()


populate_appdb_dataset_dag()
