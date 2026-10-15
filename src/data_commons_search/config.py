"""Define the service settings and configurable parameters for the agent."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL


class Settings(BaseSettings):
    """Define the service settings for the server that can be set using environment variables."""

    # Server settings
    server_port: int = 8000
    server_host: str = "0.0.0.0"  # noqa: S104
    # Mount prefix when served behind a reverse proxy (e.g. "/api/search"). Empty for local dev.
    root_path: str = ""
    cors_enabled: bool = True
    rate_limiting_enabled: bool = True
    # Set to False for local HTTP dev (browsers drop Secure cookies over plain HTTP). Keep True in prod.
    cookie_secure: bool = True
    debug_enabled: bool = False
    # Logging: human-readable rich output by default, JSON Lines for prod/staging (ELK ingestion).
    log_json: bool = True
    log_level: str = "INFO"

    filemetrix_api: str = "https://filemetrix.eosc-data-commons.dansdemo.nl/api/v1"
    tool_registry_api: str = "https://tools-registry.eosc-data-commons.eu/api/v1/tools"

    # ZENODO FEDERATION - query Zenodo alongside our own index in `search_data`. Set
    # zenodo_search_enabled=false to turn it off; see zenodo.py to remove it entirely.
    zenodo_search_enabled: bool = True
    # Optional Zenodo personal access token. Only raises our rate limit (their search needs no
    # auth), so an empty value just means anonymous requests.
    zenodo_api_key: str = ""
    # Share of the returned results reserved for our own index when Zenodo federation is on. The
    # reranker scores both backends on one scale, and Zenodo (much larger) can otherwise take every
    # slot, hiding our own records entirely. 0 disables the reservation (pure rerank order).
    own_results_min_share: float = 0.5

    # Hybrid search on the appdb `dataset` tables (see dataset_search.py, built by the DAGs in dags/)
    # Default for the `limit` argument of the search MCP tools: how many results a tool returns, and
    # how many each search input retrieves from our index and from Zenodo before reranking.
    search_results_count: int = 10
    # Candidates retrieved per method and field, then combined before trimming to the requested
    # limit. Must be larger than that limit: a small pool makes min-max normalization swing
    candidate_pool: int = 100
    # Hybrid combination weights [semantic (vectors), lexical (BM25)]
    hybrid_weights: list[float] = [0.6, 0.4]
    # Weight of each field within a method, normalized to sum to 1
    search_field_weights: dict[str, float] = {"title": 0.4, "keywords": 0.2, "description": 0.4}
    # Multiplier (not a filter) on the score of datasets with a coverage date (Collected, Coverage,
    # Valid) in the requested range. 1.0 disables it
    date_boost: float = 1.2

    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "appdb"
    # Role of the search API: reads all of appdb, writes only the user tables (users, conversations,
    # messages, rate_limits)
    postgres_appdb_search_user: str = "app"
    postgres_appdb_search_password: str = ""
    # Role of the ingestion (search pipeline DAGs, scripts): reads all the warehouse databases, owns
    # and writes the appdb tables
    postgres_appdb_ingest_user: str = "appdb_ingest"
    postgres_appdb_ingest_password: str = ""
    # Database holding the harvested records, read by scripts/compute_stats.py with the ingest role
    datasetdb_name: str = "datasetdb"

    # Number of top subjects to keep per repository in the /stats output
    # (generated offline by scripts/compute_stats.py).
    stats_top_subjects: int = 15

    # LiteLLM API used for embeddings (OpenAI-compatible /embeddings) and reranking (/rerank)
    embed_api_url: str = "https://llm.ai.egi.eu/v1"
    embed_api_key: str = ""
    reranker_model: str = "qwen3-reranker-4b"
    # Apply the Qwen3-Reranker prompt template to the /rerank query and documents ourselves, which
    # the model needs and vLLM does not do. Off for a reranker that templates server-side
    rerank_prompt_template: bool = True
    # Must be the model the generate_embeddings DAG indexed with (EMBED_API_MODEL there)
    embed_api_model: str = "nomic-embed-text-v2-moe"
    # Task prefixes the model family expects ("search_query: " / "search_document: " for nomic,
    # "query: " / "passage: " for e5). The document prefix is also read by the generate_embeddings DAG
    embed_query_prefix: str = "search_query: "
    embed_document_prefix: str = "search_document: "

    # LLM providers API keys
    default_llm_model: str = "cesnet/agentic"
    # default_llm_model: str = "openrouter/qwen/qwen3-coder-flash"
    # default_llm_model: str = "mistralai/mistral-medium-latest"
    # Model used as a fallback when the primary provider errors (rate-limit, invalid
    # model name, auth error, outage, etc.). Set to "" to disable the fallback.
    fallback_llm_model: str = "mistralai/mistral-medium-latest"
    cesnet_api_key: str = ""
    blablador_api_key: str = ""
    fedllm_api_key: str = ""
    openrouter_api_key: str = ""
    mistral_api_key: str = ""
    llm_max_tokens: int = 8192  # or 4096
    llm_seed: int = 42
    # Whether to forward the model's <think>...</think> reasoning to the frontend.
    # Off until the frontend can render thinking content properly.
    stream_thinking: bool = False

    # The name of the application used for display
    app_name: str = "EOSC Data Commons MCP"
    # Public API key used by the frontend to access the chatbot and prevent abuse from bots
    chat_api_key: str = ""

    # OIDC settings
    # oidc_config_url: str = "https://aai.egi.eu/auth/realms/egi/.well-known/openid-configuration"
    oidc_config_url: str = "https://aai-dev.egi.eu/auth/realms/egi/.well-known/openid-configuration"
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    # Public base URL of this service as reached from the browser (e.g. https://dev.matchmaker.eosc-data-commons.eu/api/search")
    # Used to build the OIDC redirect_uri
    api_public_url: str = ""
    # Hosts allowed as post-login redirect targets for external systems using /auth/login?redirect=...
    # Entries starting with "." match the host and any subdomain (e.g. ".eosc-data-commons.eu").
    allowed_redirect_hosts: list[str] = [".eosc-data-commons.eu"]
    # Public base URL used as the MCP resource identifier in the OAuth Protected Resource Metadata
    # (RFC 9728). Falls back to api_public_url then server_url. Leave empty to disable PRM discovery.
    mcp_resource_url: str = ""

    logs_filepath: str = "./data/logs/conversations.jsonl"

    # Langfuse tracing (public/secret keys need to be set via env vars)
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # EGI Secret Store (HashiCorp Vault) settings
    vault_url: str = "https://secrets.egi.eu"
    # JWT/OIDC auth mount path (e.g. "jwt" or "oidc")
    vault_jwt_mount: str = "jwt"
    # Role name for the Vault JWT auth method. EGI's mount has a default_role configured and
    # rejects an explicit role ("role ... could not be found"), so leave empty to omit it.
    vault_jwt_role: str = ""
    # KV secrets engine mount path
    vault_kv_mount: str = "secrets"
    # KV engine version (1 or 2). EGI Secret Store's "secrets/" mount is KV v1.
    vault_kv_version: int = 1

    model_config = SettingsConfigDict(
        env_file="keys.env",
        env_file_encoding="utf-8",
        extra="allow",
    )

    @property
    def appdb_search_url(self) -> URL:
        """appdb as the search role, used by the API."""
        return self.postgres_url_for(
            self.postgres_db, self.postgres_appdb_search_user, self.postgres_appdb_search_password
        )

    @property
    def appdb_ingest_url(self) -> URL:
        """appdb as the ingest role, which creates and fills the tables."""
        return self.postgres_url_for(
            self.postgres_db, self.postgres_appdb_ingest_user, self.postgres_appdb_ingest_password
        )

    @property
    def datasetdb_url(self) -> URL:
        """The harvested-records database, read by the ingest role."""
        return self.postgres_url_for(
            self.datasetdb_name, self.postgres_appdb_ingest_user, self.postgres_appdb_ingest_password
        )

    def postgres_url_for(self, db_name: str, user: str, password: str) -> URL:
        """PostgreSQL URL of `db_name` on the configured server, the password escaped."""
        return URL.create("postgresql", user, password, self.postgres_host, self.postgres_port, db_name)

    @property
    def server_url(self) -> str:
        """Computed server URL using the host and port, for accessing locally for /mcp calls.

        Returns:
            A string like 'http://127.0.0.1:8888'.
        """
        # Use 127.0.0.1 for connecting to the service (0.0.0.0 is only for binding)
        host = "127.0.0.1" if self.server_host == "0.0.0.0" else self.server_host  # noqa: S104
        return f"http://{host}:{self.server_port}"


settings = Settings()
