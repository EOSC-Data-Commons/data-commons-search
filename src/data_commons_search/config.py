"""Define the service settings and configurable parameters for the agent."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


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
    # Zenodo hits appended after our own results. Their API caps `size` at 25.
    zenodo_results_count: int = 10

    # Hybrid search settings (PostgreSQL: pg_textsearch BM25 + pgvectorscale DiskANN)
    search_results_count: int = 20
    # Candidate pool retrieved per channel and combined before trimming to search_results_count. Must
    # be MUCH larger than search_results_count: a small pool drops relevant near-duplicates and makes
    # min-max normalization swing wildly
    candidate_pool: int = 100
    # Hybrid combination weights [semantic (vector), lexical (BM25)]
    hybrid_weights: list[float] = [0.6, 0.4]
    # Name of the BM25 index on datasets.search_text, `to_bm25query()` takes it as an argument
    bm25_index: str = "datasets_bm25_search_text_idx"
    # Weight applied to the cosine similarity of each named embedding field, so that between two
    # equally close chunks a title match outranks a description one
    embedding_field_weights: dict[str, float] = {"title": 1.0, "description": 0.95, "keywords": 0.9}
    # Soft penalty (0..1) on the final score of datasets with no description. Introduced against
    # OpenSearch surfacing bare measurement titles ("CO2 20t") for a CO2 query, where 0.1 was
    # effectively a filter. Postgres scores BM25 over the whole `search_text` rather than a
    # title-weighted multi_match, so those titles no longer reach the top and the penalty only has
    # to break ties: measured on the CO2 queries, 1.0 and 0.1 give the same top 10 bar one row.
    description_penalty: float = 0.7
    # Boost (not hard filter) for records whose dates.date falls in a requested range
    # Boosting ranks in-range records higher while keeping undated/out-of-range ones available. <=1.0 effectively disables the boost
    date_boost: float = 2.0

    postgres_password: str = "postgres"  # noqa: S105
    postgres_user: str = "postgres"
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "appdb"

    # Number of top subjects to keep per repository in the /stats output
    # (generated offline by scripts/compute_stats.py).
    stats_top_subjects: int = 15

    # Embeddings. MUST match what metadata-warehouse used to index (scripts/postgres_data/
    # index_datasets.py), otherwise query and document vectors are not comparable.
    # EGI rather than Cesnet: better rate limits, and no degradation above a small batch size
    # (Cesnet returns wrong vectors past ~16 texts per request, which is why indexing verifies
    # every Cesnet batch and can use batches of 64 against EGI).
    embedding_api_url: str = "https://llm.ai.egi.eu/v1"
    embedding_model: str = "nomic-embed-text-v2-moe"
    embedding_dimensions: int = 768
    # nomic models are trained with their own task prefixes, documents are indexed with
    # `search_document: ` and queries must use `search_query: ` (the e5 family uses query:/passage:)
    embedding_query_prefix: str = "search_query: "
    # reranker_url: str = "https://llm.ai.e-infra.cz/v1/rerank"

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
    def postgres_url(self) -> str:
        """Computed PostgreSQL URL using the provided credentials."""
        return f"postgresql://{self.postgres_user}:{self.postgres_password}@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"

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
