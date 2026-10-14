"""POST /investigate (SSE) and GET /investigations/{id} on opspilot_api_test, with the stub LLM
and fake tools (needs `make up`)."""

import json
import uuid

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

from agent import graph as graph_mod
from agent.defenses import FINAL_DEFENSES
from agent.run import SHIPPED
from api.main import agent_config, agent_llm, app, guard, spent_today
from config import get_settings
from tests.stub_llm import TRIAGE, StubLLM, calls, draft, say
from tests.test_graph import fake_tool
from tests.test_schema import ALEMBIC_INI, with_db

pytestmark = pytest.mark.db

TEST_DB = "opspilot_api_test"
BODY = {"question": "The payment API is refusing connections.", "as_of": "2026-10-06T14:52:00Z"}


@pytest.fixture(scope="module")
def db_url():
    base = get_settings().database_url
    try:
        admin = psycopg.connect(with_db(base, "postgres"), autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable (run `make up`): {str(exc).splitlines()[0]}")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["database_url"] = with_db(base, TEST_DB)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    return with_db(base, TEST_DB)


@pytest.fixture
def client(db_url, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", db_url)
    get_settings.cache_clear()
    monkeypatch.setattr(graph_mod, "run_tool", fake_tool())
    # The shipped config minus Prompt Guard, which needs Groq (tests/test_defenses.py stubs it).
    app.dependency_overrides[agent_config] = lambda: SHIPPED | {"guard": "off"}
    app.dependency_overrides[guard] = lambda: None  # tests/test_api_guard.py covers the guard
    yield TestClient(app)
    app.dependency_overrides.clear()


def use(script) -> StubLLM:
    llm = StubLLM(script)
    app.dependency_overrides[agent_llm] = lambda: llm
    return llm


def parse_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines())
        out.append((lines["event"], json.loads(lines["data"])))
    return out


def test_sse_event_order_and_persistence(client, db_url):
    use(
        [
            TRIAGE,
            calls(("search_runbooks", {"query": "connection refused"})),
            say("done"),
            # First look ran get_service_status (call 0) then query_logs (call 1: log:1_<i>).
            draft(("refused", ["log:1_0"], "ERROR baz xxxxxxxxxx")),
        ]
    )
    r = client.post("/investigate", json=BODY)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse(r.text)
    assert [e for e, _ in events] == [
        "start",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "tool_call",
        "tool_result",
        "sources",
        "diagnosis",
    ]
    assert events[0][1]["config"] == SHIPPED | {"guard": "off"}
    assert events[0][1]["investigation_id"] == r.headers["x-investigation-id"]
    assert events[-1][1]["diagnosis"]["citations_verified"] is True
    assert events[-1][1]["loops"] == 0
    assert events[-1][1]["usage"] == {
        "llm_calls": 4,
        "cached_calls": 0,
        "prompt_tokens": 400,
        "completion_tokens": 80,
        "cost_usd": 0.0,  # the stub is an ollama model: free
    }
    iid = r.headers["x-investigation-id"]

    inv = client.get(f"/investigations/{iid}").json()
    assert inv["status"] == "done" and inv["as_of"].startswith("2026-10-06T14:52:00")
    assert [t["tool"] for t in inv["tool_calls"]] == [
        "get_service_status",
        "query_logs",
        "search_runbooks",
    ]
    assert all(t["success"] and t["output_tokens"] > 0 for t in inv["tool_calls"])
    assert [c["name"] for c in inv["llm_calls"]] == ["triage", "gather", "gather", "draft"]
    assert inv["prompt_tokens"] == 400 and inv["completion_tokens"] == 80 and inv["cost_usd"] == 0
    assert inv["llm"] == "ollama:stub" and inv["verify_enabled"] is True


def test_the_api_runs_the_shipped_agent():
    assert agent_config() == SHIPPED == FINAL_DEFENSES | {"first_look": True}
    assert FINAL_DEFENSES == {"wrap": True, "flag": True, "guard": "withhold"}


def test_tool_calls_have_a_foreign_key(db_url):
    with psycopg.connect(db_url) as c, pytest.raises(psycopg.errors.ForeignKeyViolation):
        c.execute(
            "INSERT INTO tool_calls (investigation_id, tool, input, latency_ms, success)"
            " VALUES (%s, 'x', '{}', 1, true)",
            (uuid.uuid4(),),
        )


def test_a_failing_run_streams_an_error_and_is_stored(client):
    use([TRIAGE])  # the script runs out at gather
    r = client.post("/investigate", json=BODY)
    events = parse_sse(r.text)
    assert events[-1][0] == "error" and "script ran out" in events[-1][1]["message"]
    inv = client.get(f"/investigations/{r.headers['x-investigation-id']}").json()
    assert inv["status"] == "error" and "script ran out" in inv["error"]


def test_unknown_investigation_is_404(client):
    assert client.get(f"/investigations/{uuid.uuid4()}").status_code == 404


def test_request_validation(client):
    assert client.post("/investigate", json={"question": "hi"}).status_code == 422


def test_spent_today_sums_todays_costs_only(client, db_url):
    with psycopg.connect(db_url, autocommit=True) as conn:
        conn.execute("DELETE FROM investigations")
        for cost, created in [
            (0.25, "now()"),
            (0.5, "now()"),
            (None, "now()"),
            (9.0, "now() - interval '2 days'"),
        ]:
            conn.execute(
                "INSERT INTO investigations (id, question, as_of, llm, verify_enabled, cost_usd,"
                f" created_at) VALUES (%s, 'q', now(), 'x:y', true, %s, {created})",
                (uuid.uuid4(), cost),
            )
    assert spent_today() == pytest.approx(0.75)
