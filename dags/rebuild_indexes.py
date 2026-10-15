"""Search indexes of appdb (filters, BM25, DiskANN), last DAG of the search pipeline.

Triggered by generate_embeddings: creates the missing indexes (e.g. the DiskANN ones dropped before a
full embeddings load), and rebuilds the existing ones too when triggered with `rebuild`.
"""

from datetime import datetime, timezone

from airflow.sdk import Param, dag, get_current_context, task
from search_pipeline.assets import EMBEDDING_TABLE
from search_pipeline.config import get_settings
from search_pipeline.indexes import build_indexes


@dag(
    dag_id="rebuild_indexes",
    description="Create or rebuild the search indexes of the appdb dataset tables",
    schedule=[EMBEDDING_TABLE],
    start_date=datetime(2026, 1, 1, tzinfo=timezone.utc),
    catchup=False,
    max_active_runs=1,
    # Asset triggered, a paused DAG drops the events of generate_embeddings
    is_paused_upon_creation=False,
    params={
        "rebuild": Param(
            False,
            type="boolean",
            description="Rebuild the existing indexes too, not only the missing ones (e.g. after many updates)",
        ),
        # A DiskANN build over ~1M vectors: 163 min at the 64MB default, 64 min at 4GB
        "maintenance_work_mem": Param("4GB", type="string", pattern=r"^\d+(kB|MB|GB)$"),
        "parallel_workers": Param(4, type="integer", minimum=0, description="max_parallel_maintenance_workers"),
    },
    tags=["search"],
)
def rebuild_indexes_dag() -> None:
    @task
    def build() -> dict[str, int]:
        params = get_current_context()["params"]
        return build_indexes(
            get_settings(),
            rebuild=params["rebuild"],
            maintenance_work_mem=params["maintenance_work_mem"],
            parallel_workers=params["parallel_workers"],
        )

    build()


rebuild_indexes_dag()
