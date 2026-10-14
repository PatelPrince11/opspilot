"""The /investigate guard: API key (401), rate limits (429), daily spend ceiling (503). No DB:
spent_today is stubbed and the route behind the guard is replaced, so nothing reaches the agent."""

import pytest
from fastapi.testclient import TestClient

from api import main
from api.main import RateLimiter, agent_config, agent_llm, app
from config import get_settings

BODY = {"question": "The payment API is refusing connections."}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("API_KEYS", "alpha, beta")
    monkeypatch.setenv("RATE_PER_MINUTE", "2")
    monkeypatch.setenv("RATE_PER_DAY", "3")
    monkeypatch.setenv("DAILY_SPEND_LIMIT_USD", "0.50")
    get_settings.cache_clear()
    monkeypatch.setattr(main, "limiter", RateLimiter())
    monkeypatch.setattr(main, "spent_today", lambda: 0.0)
    # Past the guard, the route would run the agent: stub it out with an empty stream.
    monkeypatch.setattr(main, "investigate", lambda *a, **k: iter(()))
    app.dependency_overrides[agent_llm] = lambda: None
    app.dependency_overrides[agent_config] = lambda: {}
    yield TestClient(app)
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def post(client, key=None):
    headers = {"X-API-Key": key} if key is not None else {}
    return client.post("/investigate", json=BODY, headers=headers)


@pytest.mark.parametrize("key", [None, "", "gamma", "alph", "alpha,beta"])
def test_missing_or_bad_key_is_401(client, key):
    r = post(client, key)
    assert r.status_code == 401
    assert r.json()["detail"] == "missing or invalid X-API-Key"


def test_every_listed_key_is_accepted(client):
    assert post(client, "alpha").status_code == 200
    assert post(client, "beta").status_code == 200


def test_no_keys_configured_refuses_everything(client, monkeypatch):
    monkeypatch.setenv("API_KEYS", "")
    get_settings.cache_clear()
    assert post(client, "").status_code == 401
    assert post(client, "alpha").status_code == 401


def test_per_minute_limit_is_429_with_retry_after(client):
    assert [post(client, "alpha").status_code for _ in range(2)] == [200, 200]
    r = post(client, "alpha")
    assert r.status_code == 429
    assert 1 <= int(r.headers["Retry-After"]) <= 60


def test_per_ip_limit_spans_keys(client):
    # Same client IP, two keys: the IP bucket fills even though each key has room.
    assert post(client, "alpha").status_code == 200
    assert post(client, "beta").status_code == 200
    assert post(client, "beta").status_code == 429


def test_health_stays_open(client):
    assert client.get("/health").status_code == 200


def test_spend_ceiling_is_503_until_utc_midnight(client, monkeypatch):
    monkeypatch.setattr(main, "spent_today", lambda: 0.50)
    r = post(client, "alpha")
    assert r.status_code == 503
    assert 1 <= int(r.headers["Retry-After"]) <= 86400
    monkeypatch.setattr(main, "spent_today", lambda: 0.49)
    assert post(client, "alpha").status_code == 200


def test_bad_key_is_refused_before_spend_is_read(client, monkeypatch):
    def boom():
        raise AssertionError("spend read for an unauthenticated request")

    monkeypatch.setattr(main, "spent_today", boom)
    assert post(client, "nope").status_code == 401


def test_limiter_windows():
    lim = RateLimiter()
    limits = [(2, 60.0), (3, 86400.0)]
    assert lim.hit(["k"], limits, 0.0) == 0
    assert lim.hit(["k"], limits, 10.0) == 0
    assert lim.hit(["k"], limits, 20.0) == pytest.approx(40.0)  # the hit at 0 leaves at 60
    assert lim.hit(["k"], limits, 60.0) == 0  # minute window has room; day now at 3
    assert lim.hit(["k"], limits, 120.0) == pytest.approx(86400.0 - 120.0)  # day full
    # A refused hit records nothing, in any bucket.
    assert lim.hit(["k", "other"], limits, 130.0) > 0
    assert lim.hit(["other"], limits, 130.0) == 0
