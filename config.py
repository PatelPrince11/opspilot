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

    # Chunking (tokens, counted with the embedding model's own tokenizer). Locked at 256/32 by
    # the Phase 2 ablation (eval/results/ablation.md); 400/50 was the Phase 1 start.
    chunk_size: int = 256
    chunk_overlap: int = 32
    # Local embedder, pinned to a Hugging Face commit so stored vectors stay comparable.
    embed_model: str = "BAAI/bge-small-en-v1.5"
    embed_revision: str = "5c38ec7c405ec4b44b94cc5a9bb96e735b38267a"
    # Local cross-encoder reranker, pinned the same way (HF commit, head of main on 2026-10-11).
    rerank_model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"
    rerank_revision: str = "233902d25c440f23af6f7d6e94d2946bac0bee0a"

    # Retrieval defaults (rag/retrieval.py), locked by the Phase 2 BM25 rerun on dev
    # (eval/results/ablation.md, B6): BM25 keyword beats dense on Recall@5 (CI excludes 0) and
    # ties hybrid and hybrid_rerank on Recall@5 and MRR, at the lowest latency. candidates and
    # bge_prefix apply to the dense and hybrid modes only; the prefix made no claimable
    # difference and stays on, as the bge model card recommends.
    retrieval_mode: str = "keyword"
    retrieval_candidates: int = 30
    bge_prefix: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()
