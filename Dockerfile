FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim
# https://docs.astral.sh/uv/guides/integration/docker

WORKDIR /app
COPY . /app/

# Arguments when building
ARG UV_HTTP_TIMEOUT=120
ARG PYPI_INDEX_URL=https://pypi.org/simple

# Setting env when building
ENV UV_INDEX_URL=${PYPI_INDEX_URL}
ENV UV_HTTP_TIMEOUT=${UV_HTTP_TIMEOUT}

RUN uv sync --frozen --extra agent

ENV SERVER_PORT=8000
ENV SERVER_HOST='0.0.0.0'
ENV PYTHONUNBUFFERED='1'
ENV WORKERS=6
EXPOSE 8000
ENTRYPOINT ["sh", "-c", "uv run uvicorn src.data_commons_search.main:app --host $SERVER_HOST --port $SERVER_PORT --workers $WORKERS --log-config logging.yml"]
