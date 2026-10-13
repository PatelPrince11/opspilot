import pytest

from config import get_settings


@pytest.fixture(autouse=True)
def no_tracing_no_llm_cache(monkeypatch):
    """.env may turn tracing on; tests never trace or write the LLM cache unless they opt in."""
    monkeypatch.setenv("TRACING_ENABLED", "false")
    monkeypatch.setenv("LLM_CACHE_DIR", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
