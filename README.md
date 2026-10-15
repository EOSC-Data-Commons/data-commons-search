# 🔭 EOSC Data Commons Search server

[![Build](https://github.com/EOSC-Data-Commons/data-commons-search/actions/workflows/build.yml/badge.svg)](https://github.com/EOSC-Data-Commons/data-commons-search/actions/workflows/build.yml)
[![Docker image](https://img.shields.io/badge/docker-ghcr.io-blue.svg?logo=docker)](https://github.com/EOSC-Data-Commons/data-commons-search/pkgs/container/data-commons-search)
<!-- [![PyPI - Version](https://img.shields.io/pypi/v/data-commons-search.svg?logo=pypi&label=PyPI&logoColor=silver)](https://pypi.org/project/data-commons-search/) [![PyPI - Python Version](https://img.shields.io/pypi/pyversions/data-commons-search.svg?logo=python&label=Python&logoColor=silver)](https://pypi.org/project/data-commons-search/) -->

A server for the [EOSC Data Commons project](https://eosc.eu/horizon-europe-projects/eosc-data-commons/) MatchMaker service, providing natural language search over open-access datasets. It exposes an HTTP POST endpoint and supports the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) to help users discover datasets and tools via a Large Language Model–assisted search.

## 🧩 Endpoints

The HTTP API comprises 2 main endpoints:

- `/mcp`: **MCP server** that searches for relevant data to answer a user question, with a hybrid search (BM25 + vectors) over the EOSC Data Commons datasets in PostgreSQL
  - Uses Streamable HTTP transport
  - Available tools:
    - [x] Search datasets
    - [x] Get metadata for the files in a dataset (name, description, type of files)
    - [x] Search tools
    - [ ] Search citations related to datasets or tools
- `/chat`: **HTTP POST** endpoint (JSON) for chatting with the MCP server tools via an LLM provider (API key provided through env variable at deployment)
  - Streams Server-Sent Events (SSE) response complying with the [AG-UI protocol](https://ag-ui.com).

> [!TIP]
>
> It can also be used just as a MCP server through the pip package.

## 🔌 Connect to the MCP server

The system can be used directly as a MCP server using either STDIO, or Streamable HTTP transport.

> [!WARNING]
>
> You will need access to a PostgreSQL `appdb` with the `dataset` tables built by the [search pipeline](#-search-pipeline) for the MCP server to work.

Follow the instructions of your client, and use the `/mcp` URL of the public server: https://matchmaker.eosc-data-commons.eu/api/search/mcp

To add a new MCP server to **VSCode GitHub Copilot**:

- Open the Command Palette (`ctrl+shift+p` or `cmd+shift+p`)
- Search for `MCP: Add Server...`
- Choose `HTTP`, and provide the MCP server URL: https://matchmaker.eosc-data-commons.eu/api/search/mcp

Your VSCode `mcp.json` should look like:

```json
{
    "servers": {
        "data-commons-search-http": {
            "url": "https://matchmaker.eosc-data-commons.eu/api/search/mcp",
            "type": "http"
        }
    },
    "inputs": []
}
```

## 🛠️ Development

> [!IMPORTANT]
>
> Requirements:
>
> - [x] [`uv`](https://docs.astral.sh/uv/getting-started/installation/), to easily handle scripts and virtual environments
> - [x] [docker](https://docs.docker.com/get-started/get-docker/), to deploy the database
> - [x] API key for a LLM provider: [e-infra CZ](https://chat.ai.e-infra.cz/), [Mistral.ai](https://console.mistral.ai/api-keys), or [OpenRouter](https://openrouter.ai/)
>

### 📥 Install dev dependencies

```sh
uv sync --all-extras
```

Install pre-commit hooks:

```sh
uv run --all-extras pre-commit install
```

Create a **`keys.env`** file with your LLM provider API key(s), and optionally other configurations:

```sh
CESNET_API_KEY=YOUR_API_KEY
MISTRAL_API_KEY=YOUR_API_KEY
# Key for the LiteLLM API used to rerank search results
EMBED_API_KEY=YOUR_API_KEY
# Optional Zenodo personal access token, only raises the rate limit of the federated Zenodo search
ZENODO_API_KEY=YOUR_API_KEY

OIDC_CLIENT_ID=
OIDC_CLIENT_SECRET=
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
POSTGRES_HOST=localhost
# Role of the API: reads all of appdb, writes only the user tables
POSTGRES_APPDB_SEARCH_USER=app
POSTGRES_APPDB_SEARCH_PASSWORD=app_password
# Role of the ingestion (DAGs, scripts): reads all the warehouse databases, creates and writes the appdb tables
POSTGRES_APPDB_INGEST_USER=appdb_ingest
POSTGRES_APPDB_INGEST_PASSWORD=appdb_ingest_password

RATE_LIMITING_ENABLED=False
LOG_LEVEL=DEBUG
LOG_JSON=false

EMBED_API_KEY=
```

### 💾 Database

The search system needs to connect to a PostgreSQL database to store authenticated users conversations.

Deploy and initialize the [metadata-warehouse](https://github.com/EOSC-Data-Commons/metadata-warehouse), in these instructions we expect the `metadata-warehouse` folder to be alongside the `data-commons-search`,in the same folder.

```sh
cd ../metadata-warehouse
docker compose up postgres
```

To create the empty `appdb`, run from the `metadata-warehouse` repo:

```sh
uv run --directory scripts/postgres_data create_db.py --db appdb --reset
```

Its tables are created from the SQLAlchemy models of [`db_models.py`](src/data_commons_search/db_models.py) and [`dataset_models.py`](src/data_commons_search/dataset_models.py) when missing, by the `create_appdb_tables` task of the `populate_appdb_dataset` DAG (the deferred search indexes are built by the `rebuild_indexes` DAG). Two roles access `appdb`:

| Role | Env vars | Access |
| --- | --- | --- |
| `app` | `POSTGRES_APPDB_SEARCH_USER`, `POSTGRES_APPDB_SEARCH_PASSWORD` | the search API: reads and writes the rows of all the `appdb` tables, through default privileges on the tables the ingest role creates |
| `appdb_ingest` | `POSTGRES_APPDB_INGEST_USER`, `POSTGRES_APPDB_INGEST_PASSWORD` | the DAGs and `scripts/compute_stats.py`: reads all the warehouse databases (`datasetdb`, `tooldb`, `filedb`, `appdb`), creates (so owns) and writes the `appdb` tables |

Until the metadata-warehouse creates them, create the roles (with the passwords of `keys.env`), their grants and the extensions with [`init_roles.sql`](scripts/init_roles.sql), as the warehouse admin, from the `metadata-warehouse` repo:

```sh
docker compose exec -T postgres psql -U postgres -d appdb -v ON_ERROR_STOP=1 \
  -v search_user=app -v search_password=... \
  -v ingest_user=appdb_ingest -v ingest_password=... < ../data-commons-search/scripts/init_roles.sql
```

Then create the tables by running the `create_appdb_tables` task of the `populate_appdb_dataset` DAG (it runs at the start of each DAG run, the API needs the user tables before the first one):

```sh
docker compose exec airflow-worker airflow tasks test populate_appdb_dataset create_appdb_tables
```

> [!IMPORTANT]
>
> The password of an existing role is not updated, change it with:
>
> ```sql
> ALTER USER app WITH PASSWORD 'newpassword';
> ```

Reset db:

```sh
docker compose down --volumes --remove-orphans
```

### ⚡️ Start dev server

Start the server in dev at http://localhost:8000, with MCP endpoint at http://localhost:8000/mcp searching the `dataset` tables of the `POSTGRES_*` database:

```sh
uv run --all-extras uvicorn src.data_commons_search.main:app --reload
```

Customize server port through environment variable:

```sh
SERVER_PORT=8001 uv run --all-extras uvicorn src.data_commons_search.main:app --host 0.0.0.0 --port 8001 --reload
```

> [!NOTE]
>
> You can deploy the `matchmaker` frontend in dev on the side pointing to this dev server:
>
> ```sh
> cd ../matchmaker
> npm run dev
> ```

> [!TIP]
>
> Example `curl` request:
>
> ```sh
> curl -X POST http://localhost:8000/chat -H "Content-Type: application/json" \
> 	-d '{"items": [{"type": "message", "role": "user", "content": [{"text": "Educational datasets from Switzerland covering student assessments, language competencies, and learning outcomes, including experimental or longitudinal studies on pupils or students."}]}], "model": "cesnet/agentic"}'
> ```
>
> With authenticated user access token from http://127.0.0.1:8000/auth/login:
>
> ```sh
> curl -X POST http://localhost:8000/chat -H "Content-Type: application/json" \
> -H "Cookie: access_token=$ACCESS_TOKEN" \
> -d '{"items": [{"type": "message", "role": "user", "content": [{"text": "Educational datasets from Switzerland covering student assessments, language competencies, and learning outcomes, including experimental or longitudinal studies on pupils or students."}]}], "model": "cesnet/agentic"}'
> ```
>
> Get last conversation:
>
> ```sh
> curl -X GET "http://localhost:8000/conversation/$(curl -s http://localhost:8000/conversations -H "Content-Type: application/json" -H "Cookie: access_token=$ACCESS_TOKEN" | jq -r '.[-1].thread_id')" -H "Content-Type: application/json" -H "Cookie: access_token=$ACCESS_TOKEN"
> ```
>
> Find available model from Cesnet provider:
>
> ```sh
> curl -H "Authorization: Bearer $CESNET_API_KEY" https://llm.ai.e-infra.cz/v1/models | jq ".data[].id"
> ```
>
> Recommended model: `cesnet/agentic`

### 🔎 Search pipeline

The datasets searched by the API are prepared from the records harvested in `datasetdb` by 3 Airflow DAGs in the [`dags/`](dags) folder, run by the [metadata-warehouse](https://github.com/EOSC-Data-Commons/metadata-warehouse) Airflow. They are chained through Airflow assets: each successful run triggers the next DAG. A task with nothing to hand over is skipped, which stops the pipeline there: `populate_appdb_dataset` when no dataset waits for embeddings (checked on the queue, so leftovers of a failed embeddings run are caught up), `generate_embeddings` when it embedded nothing. The DAGs are created unpaused, a paused DAG drops the events that would trigger it.

1. `populate_appdb_dataset` (manually triggered for now, on CRON schedule once deployed): turns the `datasetdb` records of type `Dataset` into the `dataset` table of `appdb`, one row per unique URL. Incremental from the last successful run, `full` re-reads everything and deletes the datasets whose record is gone.
2. `generate_embeddings`: embeds the datasets new or changed since their last embedding into `dataset_embedding`, one named embedding per field. `title` and `keywords` get a single chunk 0, `description` is split in chunks. `full` re-embeds everything, e.g. after changing the model.
3. `rebuild_indexes`: creates the missing indexes (BM25 per field with `pg_textsearch`, DiskANN per field with `pgvectorscale`, filters), then runs `ANALYZE`. Indexes are maintained on insert, so only the DiskANN indexes dropped before a full embeddings run (a graph grown from empty loses its recall) or new indexes get built. Trigger it with `rebuild` to also rebuild the existing ones with `REINDEX CONCURRENTLY`, search keeps working meanwhile.

The pipeline code is in [`dags/search_pipeline`](dags/search_pipeline). The SQLAlchemy models of the tables and their indexes are in [`dataset_models.py`](src/data_commons_search/dataset_models.py), shared with the search query of the API in [`dataset_search.py`](src/data_commons_search/dataset_search.py): the module only imports SQLAlchemy and pgvector, so the Airflow worker can import it without installing the API dependencies. `appdb` needs the `pgvectorscale` and `pg_textsearch` extensions.

To load the DAGs in a local metadata-warehouse Airflow, mount `dags/` and `src/` and add them to the `PYTHONPATH` of the `airflow-dag-processor` and `airflow-worker` services in its `docker-compose.override.yml`. To build a local `appdb` (the postgres of the stack) from the records harvested on staging:

```yaml
    airflow-worker:
        volumes:
            - ../data-commons-search/dags:/opt/airflow/dags/data-commons-search
            - ../data-commons-search/src:/opt/airflow/data-commons-search/src
        env_file: [ keys.env ]
        environment:
            PYTHONPATH: /opt/airflow/warehouse:/opt/airflow/dags/data-commons-search:/opt/airflow/data-commons-search/src
            POSTGRES_HOST: postgres
            POSTGRES_PORT: "5432"
            # The DAGs connect as the ingest role, and grant the search role its access to the tables
            POSTGRES_APPDB_INGEST_USER: ${POSTGRES_APPDB_INGEST_USER}
            POSTGRES_APPDB_INGEST_PASSWORD: ${POSTGRES_APPDB_INGEST_PASSWORD}
            POSTGRES_APPDB_SEARCH_USER: ${POSTGRES_APPDB_SEARCH_USER}
            POSTGRES_DB: appdb
            # STAGING_POSTGRES_* defined in the metadata-warehouse .env
            DATASETDB_DSN: host=${STAGING_POSTGRES_HOST} port=5432 dbname=datasetdb user=${STAGING_POSTGRES_USER} password=${STAGING_POSTGRES_PASSWORD}
```

Then run the whole pipeline for a few repositories: trigger `populate_appdb_dataset` with `{"full": true, "repositories": ["DASCH", "FINBIF"]}`, the two others follow.
```sh
docker compose exec airflow-worker airflow dags test populate_appdb_dataset -c '{"full": true, "repositories": ["DASCH", "FINBIF"]}'
```

### 🔐 Secrets Store

[EGI Secret Store](https://docs.egi.eu/users/security/secrets-store/), get the token from [aai.egi.eu/token](https://aai.egi.eu/token) (decode the JWT to get the actual access token)

```sh
export BASE="https://matchmaker.eosc-data-commons.eu"
curl -s "$BASE/auth/user" --cookie "access_token=$TOKEN"

curl -s -X PUT "$BASE/auth/keys/vip" --cookie "access_token=$TOKEN" \
  -H "Content-Type: application/json" -d '{"key_value":"sk-123"}'

curl -s "$BASE/auth/keys" --cookie "access_token=$TOKEN"
curl -s "$BASE/auth/keys/all" --cookie "access_token=$TOKEN"
curl -s "$BASE/auth/keys/vip" --cookie "access_token=$TOKEN"
curl -s -X DELETE "$BASE/auth/keys/vip" --cookie "access_token=$TOKEN"
```

### 🐳 Deploy with Docker

Create a `keys.env` file with the API keys (see above for complete example):

```sh
CESNET_API_KEY=YOUR_API_KEY
MISTRAL_API_KEY=YOUR_API_KEY
EMBED_API_KEY=YOUR_API_KEY
SEARCH_API_KEY=SECRET_KEY_YOU_CAN_USE_IN_FRONTEND_TO_AVOID_SPAM
```

> [!TIP]
>
> `SEARCH_API_KEY` can be used to add a layer of protection against bots that might spam the LLM, if not provided no API key will be needed to query the API.

You can use the prebuilt docker image [`ghcr.io/eosc-data-commons/data-commons-search:main`](https://github.com/EOSC-Data-Commons/data-commons-search/pkgs/container/data-commons-search)

Example `compose.yml`:

```yaml
services:
  mcp:
    image: ghcr.io/eosc-data-commons/data-commons-search:main
    ports:
      - "127.0.0.1:8000:8000"
    environment:
      POSTGRES_HOST: "postgres"
      CESNET_API_KEY: "${CESNET_API_KEY}"
      EMBED_API_KEY: "${EMBED_API_KEY}"
```

Build and deploy the service:

```sh
docker compose up
```

### 📦 Build for production

Build package in `dist/`:

```sh
uv build
```

### ✅ Run tests

> [!CAUTION]
>
> You need to first start the server on port 8000 (see start dev server section) and PostgreSQL.

```bash
uv run pytest
```

**Run benchmark** (check success of a set of search queries):

```sh
uv run tests/benchmark.py
```

**Run LLM jailbreak tests** with [`garak`](https://github.com/NVIDIA/garak):

```sh
PYTHONPATH=tests/security uv run garak --config tests/security/garak.yaml
```

**Run stress tests** (20 concurrent uses) of the API:

```sh
uv run tests/stress_api.py -c 20
```

### 🧹 Format code and type check

```sh
uvx ruff format && uvx ruff check --fix && uvx ty check
```

### ♻️ Reset the environment

Upgrade `uv`:

```sh
uv self update
```

Clean `uv` cache:

```sh
uv cache clean
```

### 🔧 Maintenance

Pre-compute the stats served by `/stats`: reads the harvested records from `datasetdb` and appends
a row to the `stats_snapshots` table in `appdb` (same host, set `DATASETDB_NAME` to use another
database name). `/stats` then serves the new numbers without a redeploy, and the table keeps the
history of growth. Runs as the ingest role (`POSTGRES_APPDB_INGEST_USER` and `_PASSWORD`):

```sh
uv run --env-file prod.env scripts/compute_stats.py
```

Update dependencies in `pyproject.toml`:

```sh
uvx uv-bump
```

### 🏷️ Release process

Run the release script providing the version bump: `fix`, `minor`, or `major`

```sh
.github/release.sh fix
```

Or an explicit version, e.g. to align with the frontend version:

```sh
.github/release.sh 0.10.0
```

> This will create a git tag, github release, and publish a docker image

## 🤝 Acknowledments

The LLM provider `cesnet` is a service provided by e-INFRA CZ and operated by CERIT-SC Masaryk University

Computational resources were provided by the e-INFRA CZ project (ID:90254), supported by the Ministry of Education, Youth and Sports of the Czech Republic.

The authentication provider is [EGI Check-in](https://www.egi.eu/service/check-in-internal/).
