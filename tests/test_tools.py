"""agent/tools on a fixture DB (opspilot_tools_test), as the read-only role (needs `make up`)."""

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import errors

from agent import tools
from agent.graph import build_graph, initial_state
from agent.tools import (
    MAX_LOG_ROWS,
    PAGE_TOKENS,
    SNIPPET_CHARS,
    ToolError,
    connect,
    run_tool,
    tool_schemas,
)
from config import get_settings
from llm.client import estimate_tokens
from tests.stub_llm import TRIAGE, StubLLM, calls, draft, say
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
        # Logs of the past incident INC-9001 (5 January 2026), long before the demo's as_of.
        c.execute(
            "INSERT INTO log_events (ts, service, level, message, source, incident_id)"
            " SELECT '2026-01-05 14:00:00+00'::timestamptz + make_interval(mins => g),"
            " 'payments-api', 'ERROR', 'PAYAPI-E1042 pool exhausted', 'synthetic', 'INC-9001'"
            " FROM generate_series(1, 5) g"
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
    assert names == [
        "search_runbooks",
        "search_incidents",
        "query_logs",
        "get_service_status",
        "read_document",
    ]
    logs = tool_schemas()[2]["function"]["parameters"]
    assert set(logs["required"]) == {"service", "start", "end"}


def test_a_past_incident_question_retrieves_that_incidents_logs(ro):
    """The stub plays a model that follows the prompt: an explicit window around the named date,
    nine months before as_of. The real tool returns exactly that incident's logs."""
    as_of = "2026-10-06T14:52:00Z"
    window = {
        "service": "payments-api",
        "start": "2026-01-05T13:30:00Z",
        "end": "2026-01-05T15:00Z",
    }
    llm = StubLLM([TRIAGE, calls(("query_logs", window)), say("done"), draft()])
    g = build_graph(llm, ro, verify_quotes=False)
    final = g.invoke(initial_state("What broke payments-api on 5 January 2026?", as_of))
    want = {
        f"log:{i}"
        for (i,) in ro.execute("SELECT id FROM log_events WHERE incident_id = %s", ["INC-9001"])
    }
    assert len(want) == 5 and {k for k in final["evidence"] if k.startswith("log:")} == want
    system = llm.bodies[1]["messages"][0]["content"]  # the first gather turn
    assert f"(as_of) is {as_of}" in system and "past date" in system
    assert "set the window around it" in system


# ---------------------------------------------------------------------- read_document


def test_read_document_returns_every_chunk_in_order_with_search_ids(ro):
    out = run_tool("read_document", {"doc_id": "INC-9002"}, ro)
    assert [e.id for e in out.items] == ["INC-9002#0", "INC-9002#1"]
    assert out.items[1].text.startswith("The team deleted old archives")  # full text, not capped
    assert {e.source for e in out.items} == {"incident"} and out.items[0].metadata["read"]
    assert out.note == "INC-9002 part 1 of 1"
    # A search result id names its document.
    assert run_tool("read_document", {"doc_id": "INC-9002#1"}, ro).items == out.items


def test_read_document_pages_hold_the_budget(ro, monkeypatch):
    monkeypatch.setattr(tools, "PAGE_TOKENS", 10)  # each fixture chunk alone is over 10
    one = run_tool("read_document", {"doc_id": "INC-9002", "part": 1}, ro)
    two = run_tool("read_document", {"doc_id": "INC-9002", "part": 2}, ro)
    assert [e.id for e in one.items] == ["INC-9002#0"] and [e.id for e in two.items] == [
        "INC-9002#1"
    ]
    assert "part 1 of 2; read_document part 2" in one.note and two.note == "INC-9002 part 2 of 2"
    with pytest.raises(ToolError, match="2 part"):
        run_tool("read_document", {"doc_id": "INC-9002", "part": 3}, ro)


def test_pages_never_exceed_page_tokens_unless_one_chunk_does():
    rows = [("incident", i, "word " * n, {}) for i, n in enumerate([300, 300, 2000, 3000, 10])]
    paged = tools.pages(rows)
    assert [[r[1] for r in p] for p in paged] == [[0, 1], [2], [3], [4]]
    for p in paged:
        cost = sum(estimate_tokens(r[2]) for r in p)
        assert cost <= PAGE_TOKENS or len(p) == 1
    assert tools.pages([]) == [[]]


@pytest.mark.parametrize("doc_id", ["INC-0000", "RB-999"])
def test_read_document_missing_id_is_a_failed_call(ro, doc_id):
    with pytest.raises(ToolError, match="no document"):
        run_tool("read_document", {"doc_id": doc_id}, ro)


def test_quote_from_a_read_chunk_verifies_and_an_invented_one_does_not(ro):
    """search shows INC-9002's best chunk; read_document brings both chunks in full. A quote from
    the second chunk verifies; an invented quote fails and is dropped after the loops."""
    good = ("logrotate was restarted", ["INC-9002#1"], "restarted logrotate on logs-01")
    bad = ("the disk was replaced", ["INC-9002#1"], "the disk on logs-01 was replaced")
    llm = StubLLM(
        [
            TRIAGE,
            calls(("read_document", {"doc_id": "INC-9002"})),
            say("done"),
            draft(good, bad),
            say("done"),  # loop 1
            draft(good, bad),
            say("done"),  # loop 2
            draft(good, bad),
        ]
    )
    final = build_graph(llm, ro).invoke(initial_state("logs-01 disk full?", "2026-10-06T14:52Z"))
    d = final["diagnosis"]
    assert [c["text"] for c in d["claims"]] == ["logrotate was restarted"]
    assert [c["text"] for c in d["dropped_claims"]] == ["the disk was replaced"]
    assert max(llm.checked) <= llm.budget
    # The draft saw the read chunk whole.
    draft_prompt = llm.bodies[3]["messages"][1]["content"]
    assert "[INC-9002#1] The team deleted old archives and restarted logrotate" in draft_prompt


def test_later_gather_turns_still_show_a_read_documents_part_count(ro):
    """The part count must outlive the turn that read it, or the model re-reads (dev runs)."""
    llm = StubLLM(
        [
            TRIAGE,
            calls(("read_document", {"doc_id": "INC-9002"})),
            calls(("get_service_status", {})),
            say("done"),
            draft(),
        ]
    )
    build_graph(llm, ro, verify_enabled=False).invoke(initial_state("disk?", "2026-10-06T14:52Z"))
    third_turn = llm.bodies[3]["messages"]
    assert (
        '1. read_document {"doc_id": "INC-9002"} -> 2 items (INC-9002 part 1 of 1)'
        in (third_turn[1]["content"])
    )
    assert "do not read a document again" in third_turn[0]["content"]
