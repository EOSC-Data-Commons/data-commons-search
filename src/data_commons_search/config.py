"""Define the service settings and configurable parameters for the agent."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Define the service settings for the server that can be set using environment variables."""

    # Server settings
    server_port: int = 8000
    server_host: str = "0.0.0.0"  # noqa: S104
    # # Mount prefix when served behind a reverse proxy (e.g. "/api/search")
    # root_path: str = ""
    cors_enabled: bool = True
    rate_limiting_enabled: bool = True
    # Set to False for local HTTP dev (browsers drop Secure cookies over plain HTTP). Keep True in prod.
    cookie_secure: bool = True
    debug_enabled: bool = False
    # Logging: human-readable rich output by default, JSON Lines for prod/staging (ELK ingestion).
    log_json: bool = True
    log_level: str = "INFO"

    filemetrix_api: str = "https://filemetrix.labs.dansdemo.nl/api/v1"
    tool_registry_api: str = "https://dev.tools-registry.eosc-data-commons.eu/api/v1/tools"

    # OpenSearch settings
    opensearch_index: str = "test_datacite"
    # opensearch_index: str = "20260507_datacite"
    opensearch_url: str = "http://opensearch:9200"
    search_results_count: int = 20
    # knn candidate pool retrieved and combined before trimming to search_results_count. Must be MUCH
    # larger than search_results_count: a small pool drops relevant near-duplicates and makes min_max
    # normalization swing wildly
    candidate_pool: int = 100
    # Hybrid combination weights [knn (semantic), keyword (BM25)]
    hybrid_weights: list[float] = [0.6, 0.4]
    # Small soft penalty (0..1) applied to the keyword score of records lacking a description
    # description_penalty: float = 0.05
    description_penalty: float = 0.1
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

    # Embedding models: https://qdrant.github.io/fastembed/examples/Supported_Models/#supported-text-embedding-models
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dimensions: int = 384  # 60MB
    # sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 384
    # sentence-transformers/paraphrase-multilingual-mpnet-base-v2 768
    # embedding_model: str = "intfloat/multilingual-e5-large"
    # embedding_dimensions: int = 1024  # 2.2GB
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-12-v2"
    # reranker_url: str = "https://llm.ai.e-infra.cz/v1/rerank"

    # LLM providers API keys
    default_llm_model: str = "cesnet/qwen3-coder"
    # default_llm_model: str = "openrouter/qwen/qwen3-coder-flash"
    # default_llm_model: str = "mistralai/mistral-medium-latest"
    # Model used as a fallback when the primary provider rate-limits us (HTTP 429).
    # Set to "" to disable the fallback.
    fallback_llm_model: str = "mistralai/mistral-medium-latest"
    cesnet_api_key: str = ""
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
