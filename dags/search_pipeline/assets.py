"""Assets chaining the DAGs: each table update triggers the DAG that depends on it."""

from airflow.sdk import Asset

# Emitted by populate_appdb_dataset, schedules generate_embeddings
DATASET_TABLE = Asset("appdb_dataset_table")
# Emitted by generate_embeddings, schedules rebuild_indexes
EMBEDDING_TABLE = Asset("appdb_embedding_table")
