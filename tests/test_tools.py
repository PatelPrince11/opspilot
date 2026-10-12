"""agent/tools on a fixture DB (opspilot_tools_test), as the read-only role (needs `make up`)."""

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import errors

from agent.tools import (
    MAX_LOG_ROWS,
    SNIPPET_CHARS,
    ToolError,
    connect,
    run_tool,
    tool_schemas,
)
from config import get_settings
from tests.test_retrieval import load_fixture
from tests.test_schema import ALEMBIC_INI, with_db

pytestmark = pytest.mark.db

TEST_DB = "opspilot_tools_test"


@pytest.fixture(scope="module")
def url():
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
    with psycopg.connect(with_db(base, TEST_DB), autocommit=True) as c:
        load_fixture(c)
        c.execute("SELECT opspilot_bm25_refresh()")
        c.execute(
            "INSERT INTO log_events (ts, service, level, message, source)"
            " SELECT '2026-10-06 14:00:00+00'::timestamptz + make_interval(secs => g),"
            " 'payments-api', CASE WHEN g % 10 = 0 THEN 'ERROR' ELSE 'INFO' END,"
            " CASE WHEN g % 10 = 0 THEN 'refused errno=ECONNREFUSED' ELSE 'ok 100%_done' END,"
            " 'synthetic' FROM generate_series(1, 120) g"
        )
    return with_db(base, TEST_DB)


@pytest.fixture
def ro(url):
    with connect(url) as c:
        yield c


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO services (name) VALUES ('x')",
        "UPDATE services SET status = 'down'",
        "DELETE FROM log_events",
    ],
)
def test_writes_fail_through_the_tools_connection(ro, sql):
    with pytest.raises(errors.ReadOnlySqlTransaction):
        ro.execute(sql)
    # Second lock: even with read-only transactions switched off, the role has SELECT only.
    with pytest.raises(errors.InsufficientPrivilege):
        with ro.transaction():
            ro.execute("SET TRANSACTION READ WRITE")
            ro.execute(sql)


def test_search_tools_filter_by_doc_type_and_cap_snippets(ro):
    out = run_tool("search_incidents", {"query": "PAYAPI-E1042 connection pool"}, ro)
    assert out.items and {e.source for e in out.items} == {"incident"}
    assert out.items[0].id == "INC-9001#0"
    out = run_tool("search_runbooks", {"query": "redis eviction"}, ro)
    assert [e.id for e in out.items] == ["RB-901#0"]
    assert all(len(e.text) <= SNIPPET_CHARS for e in out.items)
    ids = [e.id.split("#")[0] for e in run_tool("search_incidents", {"query": "logs-01"}, ro).items]
    assert len(ids) == len(set(ids))  # deduped to documents


def test_query_logs_window_level_text_and_cap(ro):
    args = {
        "service": "payments-api",
        "start": "2026-10-06T14:00:00Z",
        "end": "2026-10-06T15:00:00Z",
    }
    out = run_tool("query_logs", args, ro)
    assert len(out.items) == MAX_LOG_ROWS and "50 of 120" in out.note
    assert out.items[0].id.startswith("log:") and out.items[0].text.startswith(
        "2026-10-06T14:00:01Z"
    )
    errs = run_tool("query_logs", args | {"level": "WARN"}, ro)
    assert len(errs.items) == 12 and all(" ERROR " in e.text for e in errs.items)
    # LIKE wildcards in the text are matched literally.
    assert len(run_tool("query_logs", args | {"text": "0%_d"}, ro).items) == 50
    assert run_tool("query_logs", args | {"text": "1%_x"}, ro).items == []


@pytest.mark.parametrize(
    "args, msg",
    [
        ({"start": "2026-10-06T00:00:00Z", "end": "2026-10-07T00:00:01Z"}, "24 hours"),
        ({"start": "2026-10-06T02:00:00Z", "end": "2026-10-06T01:00:00Z"}, "after start"),
        ({"start": "2026-10-06T00:00:00Z"}, "end"),
    ],
)
def test_query_logs_rejects_bad_windows(ro, args, msg):
    with pytest.raises(ToolError, match=msg):
        run_tool("query_logs", {"service": "payments-api", **args}, ro)


def test_service_status(ro):
    out = run_tool("get_service_status", {"service": "payments-api"}, ro)
    assert [e.id for e in out.items] == ["status:payments-api"]
    assert "healthy" in out.items[0].text
    out = run_tool("get_service_status", {"service": "payment api"}, ro)
    assert out.items == [] and "known services: logging, payments-api" in out.note
    assert len(run_tool("get_service_status", {}, ro).items) == 2


def test_unknown_tool_and_schemas():
    with pytest.raises(ToolError, match="unknown tool"):
        run_tool("drop_tables", {}, None)
    names = [t["function"]["name"] for t in tool_schemas()]
    assert names == ["search_runbooks", "search_incidents", "query_logs", "get_service_status"]
    logs = tool_schemas()[2]["function"]["parameters"]
    assert set(logs["required"]) == {"service", "start", "end"}
