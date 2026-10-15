"""Create, or rebuild, the deferred search indexes declared on the models."""

import logging
import time
from collections import Counter

from sqlalchemy import text
from sqlalchemy.schema import CreateIndex

from data_commons_search.dataset_models import search_indexes
from search_pipeline.config import PipelineSettings
from search_pipeline.db import appdb_engine

logger = logging.getLogger(__name__)


def build_indexes(
    settings: PipelineSettings,
    rebuild: bool = False,
    maintenance_work_mem: str = "4GB",
    parallel_workers: int = 4,
) -> dict[str, int]:
    """Create the missing indexes, and with `rebuild` also rebuild the existing ones, then ANALYZE.

    Indexes are maintained on insert, so the daily runs only create what is missing: the DiskANN
    indexes dropped before a full embeddings load (a graph grown one insert at a time from empty
    loses its recall), or new indexes declared on the models. A rebuild uses REINDEX CONCURRENTLY,
    which builds a copy and swaps it, so search keeps working meanwhile.
    """
    counts: Counter[str] = Counter()
    # Autocommit: REINDEX CONCURRENTLY cannot run in a transaction, the settings last for the session
    with appdb_engine(settings).connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SET statement_timeout = 0"))
        conn.execute(text("SELECT set_config('maintenance_work_mem', :v, false)"), {"v": maintenance_work_mem})
        conn.execute(
            text("SELECT set_config('max_parallel_maintenance_workers', :v, false)"), {"v": str(parallel_workers)}
        )
        for index in search_indexes():
            started = time.monotonic()
            if not conn.execute(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": index.name}).scalar():
                conn.execute(CreateIndex(index))
                action = "created"
            elif rebuild:
                conn.execute(text(f"REINDEX INDEX CONCURRENTLY {index.name}"))
                action = "rebuilt"
            else:
                action = "exists"
            counts[action] += 1
            logger.info(f"Index {index.name} {action} in {time.monotonic() - started:.0f}s")
        # Refresh the planner statistics after the bulk changes of the previous DAGs
        conn.execute(text("ANALYZE dataset"))
        conn.execute(text("ANALYZE dataset_embedding"))
    return dict(counts)
