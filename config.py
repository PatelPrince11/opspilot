"""Typed settings for every OpsPilot component, read from environment variables.

Inside Docker, Compose injects variables from .env at runtime (env_file), so
secrets never enter the image. On the host, the same .env file is read directly.
"""

from functools import lru_cache
from pathlib import Path

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
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # OpenRouter's upstream host for every call, fallbacks off, so a run never mixes backends.
    openrouter_provider: str = "groq"
    anthropic_api_key: SecretStr | None = None
    ollama_host: str = "http://host.docker.internal:11434"

    api_key: SecretStr | None = None

    # LLMs as "provider:model" (llm/client.py). Config only: switching models is an env change.
    # Agent: gpt-oss-120b via OpenRouter, pinned to Groq (Groq's paid tier was unavailable).
    # Judge: Gemini Flash-Lite, free tier, a different model family from the agent.
    agent_llm: str = "openrouter:openai/gpt-oss-120b"
    judge_llm: str = "gemini:gemini-3.5-flash-lite"
    dev_llm: str = "ollama:qwen2.5:7b"
    # Per-call cap on estimated prompt + max output tokens; callers must trim to fit. Set for
    # Groq's free tier (8K tokens per minute); kept on OpenRouter so runs stay comparable.
    llm_token_budget: int = 6000
    llm_cache_dir: str = ".cache/llm"  # empty string disables the response cache
    # Ollama's default context is small and it silently truncates longer prompts.
    ollama_num_ctx: int = Field(default=8192, ge=8192)

    # Agent (agent/graph.py). verify_enabled off skips citation checks, for the comparison;
    # verify_quotes off keeps the id check but skips the quote check.
    verify_enabled: bool = True
    verify_quotes: bool = True
    agent_max_tool_calls: int = 8
    # Look before reading: for a live incident (triage incident_time), the first named service's
    # status and WARN+ logs in the triage window are fetched before the model's own calls (they
    # count toward the cap). LOCKED off (Phase 3b.3): on dev it cost gold-doc coverage, and the
    # live-only version did not recover it (NOTES). The code stays for live-incident work.
    agent_first_look: bool = False
    agent_max_seconds: float = 300.0
    # Password of the read-only role opspilot_ro (migration 0004) that the agent's tools use.
    readonly_db_password: SecretStr = SecretStr("readonly-change-me")

    # Generation eval: agent runs (and judge calls) in flight at once.
    eval_concurrency: int = Field(default=3, ge=1)

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

    # Retrieval defaults (rag/retrieval.py), locked after the Phase 2 BM25 rerun on dev (NOTES,
    # "Phase 2 lock override"): hybrid = BM25 + dense (bge prefix on), RRF k=60, no rerank.
    # Hybrid beats dense on Recall@5 (CI excludes 0) and ties BM25 alone on every claimed
    # metric while being more robust to query wording; rerank gave no claimable gain.
    retrieval_mode: str = "hybrid"
    retrieval_candidates: int = 30
    bge_prefix: bool = True

    @property
    def llm_cache_path(self) -> Path | None:
        return Path(self.llm_cache_dir) if self.llm_cache_dir else None


@lru_cache
def get_settings() -> Settings:
    return Settings()
