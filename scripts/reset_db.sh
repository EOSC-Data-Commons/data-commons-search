# Shutdown and reset the metadata warehouse database
set -e

cd ../metadata-warehouse
docker compose down --volumes --remove-orphans
# Postgres first, waiting for its healthcheck so create_db.py can connect, then the rest of the services
docker compose up postgres -d --wait
docker compose up broker transform airflow-api-server airflow-scheduler airflow-dag-processor airflow-worker -d

# Warehouse databases first: init_roles.sql only grants the ingest role access to the ones that exist
for db in datasetdb filedb tooldb; do
  uv run --directory scripts/postgres_data create_db.py --db "$db"
done
uv run --directory scripts/postgres_data create_db.py --db appdb --reset

# Create the roles (passwords from keys.env), their grants and the extensions as the warehouse admin
env_value() { grep "^$1=" "$2" | cut -d= -f2-; }
KEYS=../data-commons-search/keys.env
docker compose exec -T postgres psql -U "$(env_value POSTGRES_ADMIN .env)" -d appdb -v ON_ERROR_STOP=1 \
  -v search_user="$(env_value POSTGRES_APPDB_SEARCH_USER $KEYS)" -v search_password="$(env_value POSTGRES_APPDB_SEARCH_PASSWORD $KEYS)" \
  -v ingest_user="$(env_value POSTGRES_APPDB_INGEST_USER $KEYS)" -v ingest_password="$(env_value POSTGRES_APPDB_INGEST_PASSWORD $KEYS)" \
  < ../data-commons-search/scripts/init_roles.sql

# # Then the tables, created as the ingest role by the first task of populate_appdb_dataset
# docker compose exec airflow-worker airflow tasks test populate_appdb_dataset create_appdb_tables
