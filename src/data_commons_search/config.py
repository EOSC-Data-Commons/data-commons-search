"""Define the service settings and configurable parameters for the agent."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Define the service settings for the server that can be set using environment variables."""

    # Seach results settings
    search_results_count: int = 20

    filemetrix_api: str = "https://filemetrix.labs.dansdemo.nl/api/v1"
    tool_registry_api: str = "https://tool-registry.labs.dansdemo.nl/tools"

    # Server settings
    server_port: int = 8000
    server_host: str = "0.0.0.0"  # noqa: S104
    cors_enabled: bool = True
    debug_enabled: bool = False
    # Set to False for local HTTP dev (browsers drop Secure cookies over plain HTTP). Keep True in prod.
    cookie_secure: bool = True

    # OpenSearch settings
    # opensearch_index: str = "test_datacite"
    opensearch_index: str = "20260507_datacite"
    opensearch_url: str = "http://opensearch:9200"
    opensearch_pipeline: str = "rrf-pipeline"

    postgres_password: str = "postgres"  # noqa: S105
    postgres_user: str = "postgres"
    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_db: str = "appdb"

    # Embedding models: https://qdrant.github.io/fastembed/examples/Supported_Models/#supported-text-embedding-models
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dimensions: int = 384  # 60MB
    # sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 384
    # sentence-transformers/paraphrase-multilingual-mpnet-base-v2 768
    # embedding_model: str = "intfloat/multilingual-e5-large"
    # embedding_dimensions: int = 1024  # 2.2GB
    reranker_model: str = "Xenova/ms-marco-MiniLM-L-12-v2"

    # LLM providers API keys
    default_llm_model: str = "einfracz/qwen3-coder"
    # default_llm_model: str = "openrouter/qwen/qwen3-coder-flash"
    # default_llm_model: str = "mistralai/mistral-medium-latest"
    einfracz_api_key: str = ""
    openrouter_api_key: str = ""
    llm_max_tokens: int = 8192  # or 4096
    llm_seed: int = 42

    # The name of the application used for display
    app_name: str = "EOSC Data Commons MCP"
    # Public API key used by the frontend to access the chatbot and prevent abuse from bots
    chat_api_key: str = ""

    # OIDC settings
    # oidc_config_url: str = "https://aai.egi.eu/auth/realms/egi/.well-known/openid-configuration"
    oidc_config_url: str = "https://aai-dev.egi.eu/auth/realms/egi/.well-known/openid-configuration"
    oidc_client_id: str = ""
    oidc_client_secret: str = ""

    logs_filepath: str = "./data/logs/conversations.jsonl"

    # Langfuse tracing (public/secret keys need to be set via env vars)
    langfuse_base_url: str = "https://cloud.langfuse.com"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    # EGI Secret Store (HashiCorp Vault) settings
    vault_url: str = "https://secrets.egi.eu"
    # JWT/OIDC auth mount path (e.g. "jwt" or "oidc")
    vault_jwt_mount: str = "jwt"
    # Role name configured in the Vault JWT auth method
    vault_jwt_role: str = "default"
    # KV secrets engine mount path
    vault_kv_mount: str = "secrets"
    # KV engine version (1 or 2)
    vault_kv_version: int = 2

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
