"""datasetdb.records -> appdb.dataset, first DAG of the search pipeline.

First creates the missing tables of appdb (created empty by the metadata-warehouse), then transforms the
records updated since the last complete read of their repository (manually triggered for now, daily once
deployed). Those per-repository watermarks are kept in an Airflow Variable, a run with `limit` or a failed
run leaves them unchanged, a run on some `repositories` only moves theirs. Its `appdb_dataset_table` asset event triggers generate_embeddings. The task is skipped,
which stops the pipeline there, when no dataset is waiting for embeddings. The check is on the queue,
not on the changes of this run, so datasets left over by a failed embeddings run still trigger it.
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from airflow.sdk import Param, Variable, dag, get_current_context, task
from airflow.sdk.exceptions import AirflowSkipException
from sqlalchemy import text

from data_commons_search import dataset_models, db_models
from search_pipeline.assets import DATASET_TABLE
from search_pipeline.config import get_settings
from search_pipeline.db import appdb_engine
from search_pipeline.embeddings import queued_count
from search_pipeline.transform import has_datasets, populate_appdb_dataset

# Records committed in datasetdb after the previous run started can carry an older updated_at
OVERLAP = timedelta(hours=6)
# Airflow Variable holding {repository code: start of its last complete read}, on the datasetdb clock
WATERMARKS_VARIABLE = "populate_appdb_dataset_watermarks"


def load_watermarks() -> dict[str, datetime]:
    stored = Variable.get(WATERMARKS_VARIABLE, default={}, deserialize_json=True)
    return {code: datetime.fromisoformat(ts) for code, ts in stored.items()}


def save_watermarks(watermarks: dict[str, datetime]) -> None:
    Variable.set(
        WATERMARKS_VARIABLE,
        {code: ts.isoformat() for code, ts in sorted(watermarks.items())},
        description="Start of the last complete read of each datasetdb repository by populate_appdb_dataset. "
        "Delete a key (or all) to re-read that repository in full on the next run",
        serialize_json=True,
    )


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
            description="Re-read every datasetdb record, not only the ones updated since the last complete "
            "read of their repository. "
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
        # Forgotten when appdb was reset, a repository without watermark is read in full
        watermarks = load_watermarks() if has_datasets(settings) else {}
        since = {} if params["full"] else {code: ts - OVERLAP for code, ts in watermarks.items()}
        stats = populate_appdb_dataset(settings, since=since, limit=params["limit"], repositories=repositories)
        # Only a complete read moves the watermarks, of the repositories it covered. A failed run raised above
        if not params["limit"] and stats.read_started:
            save_watermarks(watermarks | dict.fromkeys(stats.repositories, stats.read_started))
        # A skipped task emits no asset event, so the next DAGs do not run
        if not queued_count(settings):
            raise AirflowSkipException(f"No dataset to embed: {stats.as_dict()}")
        context["outlet_events"][DATASET_TABLE].extra = stats.as_dict()
        return stats.as_dict()

    create_appdb_tables() >> transform()


populate_appdb_dataset_dag()
