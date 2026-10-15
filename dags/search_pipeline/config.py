"""Pipeline settings, read from the environment of the airflow worker.

The same POSTGRES_* variables as the search API, so one env file configures both. Each DB can be
pointed somewhere else with a full DSN (e.g. read the staging datasetdb, write a local appdb).
"""

import os
from dataclasses import dataclass, field

from psycopg2.extensions import make_dsn


def _env(name: str, default: str = "") -> str:
    """Environment variable, the default also applies to empty values (compose sets unset ones to "")."""
    return os.environ.get(name) or default


@dataclass(frozen=True)
class PipelineSettings:
    postgres_host: str = field(default_factory=lambda: _env("POSTGRES_HOST", "postgres"))
    postgres_port: int = field(default_factory=lambda: int(_env("POSTGRES_PORT", "5432")))
    # The DAGs connect as the ingest role: reads the warehouse databases, owns and writes the appdb tables
    postgres_appdb_ingest_user: str = field(default_factory=lambda: _env("POSTGRES_APPDB_INGEST_USER", "appdb_ingest"))
    postgres_appdb_ingest_password: str = field(default_factory=lambda: _env("POSTGRES_APPDB_INGEST_PASSWORD"))
    appdb_name: str = field(default_factory=lambda: _env("POSTGRES_DB", "appdb"))
    datasetdb_name: str = field(default_factory=lambda: _env("DATASETDB_NAME", "datasetdb"))
    # Optional full DSNs, override the POSTGRES_* above for one database
    appdb_dsn: str = field(default_factory=lambda: _env("APPDB_DSN"))
    datasetdb_dsn: str = field(default_factory=lambda: _env("DATASETDB_DSN"))

    # OpenAI compatible embeddings API. The search API must embed queries with the same model.
    # The document prefix is read from the API settings (data_commons_search.config), next to the query prefix
    embed_api_url: str = field(default_factory=lambda: _env("EMBED_API_URL", "https://llm.ai.egi.eu/v1"))
    # FEDLLM_API_KEY is the key of the EGI endpoint
    embed_api_key: str = field(default_factory=lambda: _env("EMBED_API_KEY") or _env("FEDLLM_API_KEY"))
    embed_api_model: str = field(default_factory=lambda: _env("EMBED_API_MODEL", "nomic-embed-text-v2-moe"))
    # Also read by models.py for the vector column type
    embed_dimensions: int = field(default_factory=lambda: int(_env("EMBED_DIMENSIONS", "768")))
    # Hugging Face repo of the model tokenizer, chunks are sized in its tokens to fit the model window
    embed_tokenizer: str = field(default_factory=lambda: _env("EMBED_TOKENIZER", "nomic-ai/nomic-embed-text-v2-moe"))
    embed_max_tokens: int = field(default_factory=lambda: int(_env("EMBED_MAX_TOKENS", "512")))

    def dsn(self, db_name: str) -> str:
        """libpq connection string for a database on the POSTGRES_* server, its values quoted."""
        return make_dsn(
            host=self.postgres_host,
            port=self.postgres_port,
            dbname=db_name,
            user=self.postgres_appdb_ingest_user,
            password=self.postgres_appdb_ingest_password,
        )


def get_settings() -> PipelineSettings:
    """Read the settings at task run time, not at DAG parse time."""
    return PipelineSettings()
