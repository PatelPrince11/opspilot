"""Typed settings for every OpsPilot component, read from environment variables.

Inside Docker, Compose injects variables from .env at runtime (env_file), so
secrets never enter the image. On the host, the same .env file is read directly.
"""

from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Host-side default (DB_HOST_PORT); inside Compose, DATABASE_URL points at db:5432.
    database_url: str = "postgresql://opspilot:change-me@localhost:5433/opspilot"

    # Tracing is opt-in: nothing is sent to Langfuse unless TRACING_ENABLED=true.
    tracing_enabled: bool = False
    # Fraction of LLM calls to trace; lower it for bulk eval runs to save quota.
    tracing_sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)

    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    groq_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    ollama_host: str = "http://host.docker.internal:11434"

    api_key: SecretStr | None = None

    # Chunking (tokens, counted with the embedding model's own tokenizer).
    chunk_size: int = 400
    chunk_overlap: int = 50
    # Local embedder, pinned to a Hugging Face commit so stored vectors stay comparable.
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_revision: str = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"


@lru_cache
def get_settings() -> Settings:
    return Settings()
