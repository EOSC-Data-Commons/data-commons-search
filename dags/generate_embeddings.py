"""appdb.dataset -> appdb.dataset_embedding, second DAG of the search pipeline.

Triggered by populate_appdb_dataset, embeds the datasets it queued (new, or with a changed title,
keywords or description). Its `appdb_embedding_table` asset event triggers rebuild_indexes. The
task is skipped, which stops the pipeline there, when no dataset was embedded.
"""

from datetime import datetime, timezone
from typing import Any

from airflow.sdk import Param, dag, get_current_context, task
from airflow.sdk.exceptions import AirflowSkipException
from search_pipeline.assets import DATASET_TABLE, EMBEDDING_TABLE
from search_pipeline.config import get_settings
from search_pipeline.embeddings import generate_embeddings


@dag(
    dag_id="generate_embeddings",
    description="Generate the named embeddings of the appdb datasets",
    schedule=[DATASET_TABLE],
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    # Asset triggered, a paused DAG drops the events of populate_appdb_dataset
    is_paused_upon_creation=False,
    params={
        "full": Param(
            False,
            type="boolean",
            description="Re-embed every dataset, e.g. after changing the model. Drops the DiskANN indexes "
            "during the load, rebuild_indexes recreates them",
        ),
        "batch_size": Param(64, type="integer", minimum=1, description="Texts per embeddings API request"),
        "concurrency": Param(
            4, type="integer", minimum=1, description="Embeddings API requests in flight, EGI returns 429 from 8"
        ),
        "verify": Param(
            False,
            type="boolean",
            description="Re-embed 2 texts per page alone and fail if they differ from their batched vector "
            "(some APIs return wrong vectors in batches)",
        ),
        "limit": Param(None, type=["null", "integer"], description="Stop after this many datasets, for testing"),
    },
    tags=["search"],
)
def generate_embeddings_dag() -> None:
    @task(outlets=[EMBEDDING_TABLE])
    def embed() -> dict[str, Any]:
        context = get_current_context()
        params = context["params"]
        stats = generate_embeddings(
            get_settings(),
            full=params["full"],
            batch_size=params["batch_size"],
            concurrency=params["concurrency"],
            verify=params["verify"],
            limit=params["limit"],
        )
        # A skipped task emits no asset event, so rebuild_indexes does not run
        if not stats.datasets:
            raise AirflowSkipException("No dataset to embed")
        context["outlet_events"][EMBEDDING_TABLE].extra = {"full": params["full"], **stats.as_dict()}
        return stats.as_dict()

    embed()


generate_embeddings_dag()
