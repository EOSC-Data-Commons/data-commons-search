# 🔭 EOSC Data Commons Search server

[![Build](https://github.com/EOSC-Data-Commons/data-commons-search/actions/workflows/build.yml/badge.svg)](https://github.com/EOSC-Data-Commons/data-commons-search/actions/workflows/build.yml)
[![Docker image](https://img.shields.io/badge/docker-ghcr.io-blue.svg?logo=docker)](https://github.com/EOSC-Data-Commons/data-commons-search/pkgs/container/data-commons-search)
<!-- [![PyPI - Version](https://img.shields.io/pypi/v/data-commons-search.svg?logo=pypi&label=PyPI&logoColor=silver)](https://pypi.org/project/data-commons-search/) [![PyPI - Python Version](https://img.shields.io/pypi/pyversions/data-commons-search.svg?logo=python&label=Python&logoColor=silver)](https://pypi.org/project/data-commons-search/) -->

A server for the [EOSC Data Commons project](https://eosc.eu/horizon-europe-projects/eosc-data-commons/) MatchMaker service, providing natural language search over open-access datasets. It exposes an HTTP POST endpoint and supports the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) to help users discover datasets and tools via a Large Language Model–assisted search.

## 🧩 Endpoints

The HTTP API comprises 2 main endpoints:

- `/mcp`: **MCP server** that searches for relevant data to answer a user question using the EOSC Data Commons OpenSearch service
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
> You will need access to a pre-indexed OpenSearch instance for the MCP server to work.

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
> - [x] [docker](https://docs.docker.com/get-started/get-docker/), to deploy the database and OpenSearch service
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

OIDC_CLIENT_ID=
OIDC_CLIENT_SECRET=
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
POSTGRES_HOST=localhost
POSTGRES_USER=app
POSTGRES_PASSWORD=app_password

RATE_LIMITING_ENABLED=False
LOG_LEVEL=DEBUG
LOG_JSON=false

OPENSEARCH_URL=http://localhost:9200
```

### 💾 Database

The search system needs to connect to a PostgreSQL database to store authenticated users conversations.

Deploy and initialize the [metadata-warehouse](https://github.com/EOSC-Data-Commons/metadata-warehouse), in these instructions we expect the `metadata-warehouse` folder to be alongside the `data-commons-search`,in the same folder.

```sh
cd ../metadata-warehouse
docker compose up postgres
```

To initialize db, run from the `metadata-warehouse` repo:

```sh
uv run --directory scripts/postgres_data create_db.py --db appdb --reset
```

> [!IMPORTANT]
>
> For publicly available environments you will want to update the `app` user password:
>
> ```sql
> ALTER USER app WITH PASSWORD 'newpassword';
> ```

Reset db:

```sh
docker compose down --volumes --remove-orphans
```

Export the schema from `db.py` to the metadata-warehouse (command to run at the root of the data-commons-search repo):

```sh
uv run scripts/export_db_schema.py ../metadata-warehouse/scripts/postgres_data/create_sql/appdb/tables.sql
```

### ⚡️ Start dev server

Start the server in dev at http://localhost:8000, with MCP endpoint at http://localhost:8000/mcp pointing to a running OpenSearch instance:

```sh
uv run --all-extras uvicorn src.data_commons_search.main:app --reload
```

> Default `OPENSEARCH_URL=http://localhost:9200`

Customize server port through environment variable:

```sh
OPENSEARCH_URL=http://localhost:9200 SERVER_PORT=8001 uv run --all-extras uvicorn src.data_commons_search.main:app --host 0.0.0.0 --port 8001 --reload
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
      OPENSEARCH_URL: "http://opensearch:9200"
      CESNET_API_KEY: "${CESNET_API_KEY}"
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

Pre-compute stats for the datasets in the db to `src/data_commons_search/stats.json`:

```sh
POSTGRES_DB=datasetdb uv run scripts/compute_stats.py
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

> This will create a git tag, github release, and publish a docker image

## 🤝 Acknowledments

The LLM provider `cesnet` is a service provided by e-INFRA CZ and operated by CERIT-SC Masaryk University

Computational resources were provided by the e-INFRA CZ project (ID:90254), supported by the Ministry of Education, Youth and Sports of the Czech Republic.

The authentication provider is [EGI Check-in](https://www.egi.eu/service/check-in-internal/).
