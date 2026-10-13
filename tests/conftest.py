import os

import psycopg
import pytest

from config import get_settings


@pytest.fixture(scope="session", autouse=True)
def require_db():
    """CI sets REQUIRE_DB=1: an unreachable Postgres fails the run instead of letting every
    database test skip itself."""
    if os.environ.get("REQUIRE_DB") != "1":
        return
    try:
        psycopg.connect(get_settings().database_url, connect_timeout=5).close()
    except psycopg.OperationalError as exc:
        pytest.exit(f"REQUIRE_DB=1 but Postgres is not reachable: {exc}", returncode=1)


@pytest.fixture(autouse=True)
def no_tracing_no_llm_cache(monkeypatch):
    """.env may turn tracing on; tests never trace or write the LLM cache unless they opt in."""
    monkeypatch.setenv("TRACING_ENABLED", "false")
    monkeypatch.setenv("LLM_CACHE_DIR", "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
