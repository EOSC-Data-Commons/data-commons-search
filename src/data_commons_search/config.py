"""Define the service settings and configurable parameters for the agent."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Define the service settings for the server that can be set using environment variables."""

    # Seach results settings
    opensearch_results_count: int = 100
    reranking_results_count: int = 20

    filemetrix_api: str = "https://filemetrix.labs.dansdemo.nl/api/v1"
    tool_registry_api: str = "https://tool-registry.labs.dansdemo.nl/tools"

    # Server settings
    server_port: int = 8000
    server_host: str = "0.0.0.0"  # noqa: S104
    cors_enabled: bool = True
    debug_enabled: bool = False

    # OpenSearch settings
    opensearch_index: str = "test_datacite"
    opensearch_url: str = "http://localhost:9200"
    # opensearch_url: str = "http://opensearch:9200"

    # Embedding models: https://qdrant.github.io/fastembed/examples/Supported_Models/#supported-text-embedding-models
    use_online_embedding_model: bool = False
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_dimensions: int = 384  # 60MB
    # embedding_model: str = "intfloat/multilingual-e5-large"
    # embedding_dimensions: int = 1024  # 2.2GB

    # LLM providers API keys
    default_llm_model: str = "einfracz/qwen3-coder"
    einfracz_api_key: str = ""
    openrouter_api_key: str = ""
    dashscope_api_key: str = ""
    llm_max_tokens: int = 8192  # or 4096
    llm_seed: int = 42

    # The name of the application used for display
    app_name: str = "EOSC Data Commons MCP"
    # Public API key used by the frontend to access the chatbot and prevent abuse from bots
    chat_api_key: str = ""

    logs_filepath: str = "./data/logs/conversations.jsonl"

    model_config = SettingsConfigDict(
        env_file="keys.env",
        env_file_encoding="utf-8",
        extra="allow",
    )

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
