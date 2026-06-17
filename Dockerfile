FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim
# https://docs.astral.sh/uv/guides/integration/docker

WORKDIR /app
COPY . /app/

RUN uv sync --frozen --extra agent

ENV SERVER_PORT=8000
ENV SERVER_HOST='0.0.0.0'
# Emit JSON Lines logs for ELK ingestion (logging is configured in-app, see logging.py)
ENV LOG_JSON='true'
ENV PYTHONUNBUFFERED='1'
ENV WORKERS=6

# # For proper resolution in prod at https://matchmaker.eosc-data-commons.eu/api/search/docs
# ENV ROOT_PATH=/api/search

EXPOSE 8000
ENTRYPOINT ["sh", "-c", "uv run uvicorn src.data_commons_search.main:app --host $SERVER_HOST --port $SERVER_PORT --workers $WORKERS"]
