#!/bin/bash

# Expect the `metadata-warehouse` to be in the same parent directory as `data-commons-search`

cd ../metadata-warehouse
docker compose down --volumes --remove-orphans
docker compose up postgres -d
# docker compose up opensearch -d
sleep 3 # Wait for the db to be ready
docker compose exec postgres sh -c "cd /scripts && ./init_all.sh"
