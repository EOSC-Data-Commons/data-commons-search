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
    - [ ] Search tools
    - [ ] Search citations related to datasets or tools
- `/chat`: **HTTP POST** endpoint (JSON) for chatting with the MCP server tools via an LLM provider (API key provided through env variable at deployment)
  - Streams Server-Sent Events (SSE) response complying with the [AG-UI protocol](https://ag-ui.com).

> [!TIP]
>
> It can also be used just as a MCP server through the pip package.

## 🔌 Connect to MCP server

The system can be used directly as a MCP server using either STDIO, or Streamable HTTP transport.

> [!WARNING]
>
> You will need access to a pre-indexed OpenSearch instance for the MCP server to work.

Follow the instructions of your client, and use the `/mcp` URL of your deployed server (e.g. http://localhost:8000/mcp)

To add a new MCP server to **VSCode GitHub Copilot**:

- Open the Command Palette (`ctrl+shift+p` or `cmd+shift+p`)
- Search for `MCP: Add Server...`
- Choose `HTTP`, and provide the MCP server URL http://localhost:8000/mcp

Your VSCode `mcp.json` should look like:

```json
{
    "servers": {
        "data-commons-search-http": {
            "url": "http://localhost:8000/mcp",
            "type": "http"
        }
    },
    "inputs": []
}
```

Or with STDIO transport:

```json
{
   "servers": {
      "data-commons-search": {
         "type": "stdio",
         "command": "uvx",
         "args": ["data-commons-search"],
         "env": {
            "OPENSEARCH_URL": "OPENSEARCH_URL"
         }
      }
   }
}
```

Or using local folder for development:

```json
{
   "servers": {
      "data-commons-search": {
         "type": "stdio",
         "cwd": "~/dev/data-commons-search",
         "env": {
            "OPENSEARCH_URL": "OPENSEARCH_URL"
         },
         "command": "uv",
         "args": ["run", "data-commons-search"]
      }
   }
}
```

## 🛠️ Development

> [!IMPORTANT]
>
> Requirements:
>
> - [x] [`uv`](https://docs.astral.sh/uv/getting-started/installation/), to easily handle scripts and virtual environments
> - [x] docker, to deploy the OpenSearch service (or just access to a running instance)
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

Create a `keys.env` file with your LLM provider API key(s), and optionally other configurations:

```sh
EINFRACZ_API_KEY=YOUR_API_KEY
MISTRAL_API_KEY=YOUR_API_KEY
OPENROUTER_API_KEY=YOUR_API_KEY

OIDC_CLIENT_ID=
OIDC_CLIENT_SECRET=

LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=

OPENSEARCH_URL=http://localhost:9200
REDIS_URL=redis://localhost:6379
POSTGRES_USER=app
POSTGRES_USER=app_password
POSTGRES_HOST=localhost
```

### ⚡️ Start dev server

> [!IMPORTANT]
>
> The search system needs to connect to a PostgreSQL database to store authenticated users conversations, deploy and initialize the [metadata-warehouse](https://github.com/EOSC-Data-Commons/metadata-warehouse). See section below for more details on managing the database.

Start the server in dev at http://localhost:8000, with MCP endpoint at http://localhost:8000/mcp pointing to a running OpenSearch instance:

```sh
uv run --all-extras uvicorn src.data_commons_search.main:app --log-config logging.yml --reload
```

> Default `OPENSEARCH_URL=http://localhost:9200`

Customize server port through environment variable:

```sh
OPENSEARCH_URL=http://localhost:9200 SERVER_PORT=8001 uv run --all-extras uvicorn src.data_commons_search.main:app --host 0.0.0.0 --port 8001 --log-config logging.yml --reload
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
> 	-d '{"items": [{"type": "message", "role": "user", "content": [{"text": "Educational datasets from Switzerland covering student assessments, language competencies, and learning outcomes, including experimental or longitudinal studies on pupils or students."}]}], "model": "einfracz/qwen3-coder"}'
> ```
>
> With authenticated user access token from http://127.0.0.1:8000/auth/login:
>
> ```sh
> curl -X POST http://localhost:8000/chat -H "Content-Type: application/json" \
> -H "Cookie: access_token=$ACCESS_TOKEN" \
> -d '{"items": [{"type": "message", "role": "user", "content": [{"text": "Educational datasets from Switzerland covering student assessments, language competencies, and learning outcomes, including experimental or longitudinal studies on pupils or students."}]}], "model": "einfracz/qwen3-coder"}'
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
> curl -H "Authorization: Bearer $EINFRACZ_API_KEY" https://llm.ai.e-infra.cz/v1/models | jq ".data[].id"
> ```
>
> Recommended model: `einfracz/qwen3-coder` or `einfracz/gpt-oss-120b` (smaller, faster)

### 💾 Database

The search system needs to connect to a PostgreSQL database to store authenticated users conversations, deploy and initialize the [metadata-warehouse](https://github.com/EOSC-Data-Commons/metadata-warehouse).

```sh
cd metadata-warehouse
docker compose up postgres
```

Initialize db (from metadata-warehouse repo):

```sh
cd scripts/postgres_data
uv run create_db.py --db appdb --reset
```

> [!IMPORTANT]
>
> For staging and production environments you will want to update the `app` user password: `ALTER USER app WITH PASSWORD 'newpassword';`

Reset db:

```sh
docker compose down --volumes --remove-orphans
```

Export schema from `db.py` to metadata-warehouse (command to run at the root of the data-commons-search repo, and expect the metadata-warehouse folder to be alongside the data-commons-search folder in the same folder):

```sh
uv run scripts/export_db_schema.py ../metadata-warehouse/scripts/postgres_data/create_sql/appdb/tables.sql
```

### 📦 Build for production

Build binary in `dist/`

```sh
uv build
```

### 🐳 Deploy with Docker

Create a `keys.env` file with the API keys:

```sh
EINFRACZ_API_KEY=YOUR_API_KEY
MISTRAL_API_KEY=YOUR_API_KEY
OPENROUTER_API_KEY=YOUR_API_KEY
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
      EINFRACZ_API_KEY: "${EINFRACZ_API_KEY}"
```

Build and deploy the service:

```sh
docker compose up
```

> [!IMPORTANT]
>
> Current deployment to staging server is done automatically through GitHub Actions at each push to the `main` branch.
>
> When a push is made the workflow will:
>
> - Pull the `main` branch from the frontend repository
> - Build the frontend, and add it to `src/data_commons_search/webapp`
> - Build the docker image for the server
> - Publish the docker image as `main`/`latest`
> - The staging infrastructure then automatically pull the `latest` version of the image and deploys it.

### ✅ Run tests

> [!CAUTION]
>
> You need to first start the server on port 8000 (see start dev server section) and PostgreSQL.

```bash
uv run pytest
```

To display all logs for debugging:

```sh
uv run pytest -s
```

Run search benchmark:

```sh
uv run tests/benchmark.py
```

### 🧹 Format code and type check

```sh
uvx ruff format && uvx ruff check --fix
uv run mypy
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

### 🏷️ Release process

> [!IMPORTANT]
>
> Get a PyPI API token at [pypi.org/manage/account](https://pypi.org/manage/account).

Run the release script providing the version bump: `fix`, `minor`, or `major`

```sh
.github/release.sh fix
```

> [!TIP]
>
> Add your PyPI token to your environment, e.g. in `~/.zshrc` or `~/.bashrc`:
>
> ```sh
> export UV_PUBLISH_TOKEN=YOUR_TOKEN
> ```

## 🤝 Acknowledments

The LLM provider `einfracz` is a service provided by e-INFRA CZ and operated by CERIT-SC Masaryk University

Computational resources were provided by the e-INFRA CZ project (ID:90254), supported by the Ministry of Education, Youth and Sports of the Czech Republic.
