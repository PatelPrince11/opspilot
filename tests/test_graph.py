"""agent/graph.py with the scripted stub LLM and fake tools: no database, no network."""

import pytest

from agent import graph as graph_mod
from agent.graph import MAX_LOOPS, build_graph, initial_state
from agent.tools import Evidence, ToolError, ToolOutput
from tests.stub_llm import TRIAGE, StubLLM, calls, draft, say

Q = "The payment API is returning connection-refused errors. What happened?"
AS_OF = "2026-10-06T14:52:00Z"
LOGS = {"service": "payments-api", "start": "2026-10-06T14:00:00Z", "end": AS_OF}


class FakeConn:
    def execute(self, sql, params=None):
        return [("payments-api",), ("payments-pgbouncer",)]


def fake_tool(n_logs=3, text_chars=60):
    """Ids are unique across calls after the first (log:0.. on the first call, as scripts cite);
    log lines differ in words, so they are not collapsed as near-duplicates."""
    seen = []

    def run_tool(name, args, conn):
        k = len(seen)
        seen.append(name)
        tag = "" if k == 0 else f"{k}_"
        if name == "boom":
            raise ToolError("boom: invalid arguments")
        if name == "query_logs":
            items = [
                Evidence(
                    id=f"log:{tag}{i}",
                    source="log",
                    metadata={"service": args["service"]},
                    text=f"2026-10-06T14:00:00Z ERROR {word(k, i)} " + "x" * text_chars,
                )
                for i in range(n_logs)
            ]
        else:
            src = {"search_runbooks": "runbook", "search_incidents": "incident"}.get(name, "status")
            items = [
                Evidence(id=f"{name}:{tag}{i}", source=src, text=f"{name} {i} " + "y" * text_chars)
                for i in range(5)
            ]
        return ToolOutput(items=items)

    return run_tool


def word(*ns) -> str:
    return "".join("abcdefghij"[int(d)] for n in ns for d in str(n)) + "z"


def run(llm, monkeypatch, tool=None, **kw):
    monkeypatch.setattr(graph_mod, "run_tool", tool or fake_tool())
    g = build_graph(llm, FakeConn(), **kw)
    events, final = [], None
    for mode, ev in g.stream(initial_state(Q, AS_OF), stream_mode=["custom", "values"]):
        if mode == "custom":
            events.append(ev)
        else:
            final = ev
    return final, events


def kinds(events):
    return [e["event"] for e in events]


def test_happy_path_one_pass(monkeypatch):
    llm = StubLLM([TRIAGE, calls(("query_logs", LOGS)), draft(("refused", ["log:1"]))])
    final, events = run(llm, monkeypatch)
    d = final["diagnosis"]
    assert d["citations_verified"] is True and d["claims"][0]["evidence_ids"] == ["log:1"]
    assert final["loops"] == 0 and llm.script == []
    assert kinds(events) == ["tool_call", "tool_result", "sources", "diagnosis"]
    assert [c["name"] for c in final["llm_calls"]] == ["triage", "plan", "draft"]
    assert all(c["prompt_tokens"] == 100 and not c["estimated"] for c in final["llm_calls"])


def test_verify_loop_fires_on_a_bad_citation_and_stops_at_two(monkeypatch):
    bad = draft(("ok claim", ["log:1"]), ("made up", ["log:999"]), confidence="high")
    script = [TRIAGE, calls(("query_logs", LOGS))]
    for _ in range(MAX_LOOPS):
        script += [bad, calls()]  # draft, then gather's follow-up call (finds nothing)
    llm = StubLLM([*script, bad])
    final, _ = run(llm, monkeypatch)
    names = [c["name"] for c in final["llm_calls"]]
    assert final["loops"] == MAX_LOOPS == 2
    assert names.count("draft") == 3 and names.count("gather_follow_up") == 2
    assert llm.script == []  # no fourth draft
    d = final["diagnosis"]
    assert d["citations_verified"] is False and d["confidence"] == "medium"
    assert [c["text"] for c in d["claims"]] == ["ok claim"]
    assert [c["text"] for c in d["dropped_claims"]] == ["made up"]
    assert "dropped after 2 verify loop(s)" in d["note"]
    # The redraft is told which ids were invalid.
    assert "log:999" in llm.bodies[4]["messages"][1]["content"]


def test_verify_loop_recovers_when_the_redraft_cites_gathered_evidence(monkeypatch):
    script = [
        TRIAGE,
        calls(("query_logs", LOGS)),
        draft(("x", ["log:999"])),
        calls(("search_runbooks", {"query": "pgbouncer"})),
        draft(("x", ["search_runbooks:1_0"])),
    ]
    final, _ = run(StubLLM(script), monkeypatch)
    assert final["loops"] == 1 and final["diagnosis"]["citations_verified"] is True
    assert final["diagnosis"]["confidence"] == "high"


def test_uncited_claims_fail_verify(monkeypatch):
    script = [TRIAGE, calls(("query_logs", LOGS)), draft(("no ids", []))]
    final, _ = run(StubLLM(script), monkeypatch, max_seconds=0)  # out of time: no loop
    assert final["verify"]["invalid"][0]["bad_ids"] == ["(none cited)"]
    assert final["diagnosis"]["claims"] == []


def test_flag_disables_verify(monkeypatch):
    llm = StubLLM([TRIAGE, calls(("query_logs", LOGS)), draft(("made up", ["log:999"]))])
    final, events = run(llm, monkeypatch, verify_enabled=False)
    d = final["diagnosis"]
    assert d["citations_verified"] is None and d["claims"][0]["evidence_ids"] == ["log:999"]
    assert final.get("verify") is None and llm.script == []
    assert kinds(events)[-2:] == ["sources", "diagnosis"]


def test_tool_call_cap_holds(monkeypatch):
    many = calls(*[("query_logs", LOGS)] * 12)
    llm = StubLLM(
        [
            TRIAGE,
            many,
            draft(("x", ["log:999"])),
            draft(("x", ["log:999"])),
            draft(("x", ["log:999"])),
        ]
    )
    final, events = run(llm, monkeypatch, max_tool_calls=8)
    assert len(final["tool_calls"]) == 8
    assert kinds(events).count("tool_call") == 8
    assert sum("tool-call cap" in e.get("message", "") for e in events) == 4
    # Cap reached: verify loops redraft without asking for more tool calls.
    assert "gather_follow_up" not in [c["name"] for c in final["llm_calls"]]


def test_failed_tool_is_recorded_not_fatal(monkeypatch):
    llm = StubLLM([TRIAGE, calls(("boom", {}), ("query_logs", LOGS)), draft(("x", ["log:1_0"]))])
    final, _ = run(llm, monkeypatch)
    assert [t["success"] for t in final["tool_calls"]] == [False, True]
    assert "invalid arguments" in final["tool_calls"][0]["error"]


def test_no_call_exceeds_the_token_budget_on_a_full_gather(monkeypatch):
    # 8 calls, each returning 50 long log lines or 5 long snippets: far more than fits.
    plan = calls(
        *[("query_logs", LOGS)] * 4,
        *[(n, {"query": "q"}) for n in ("search_runbooks", "search_incidents")] * 2,
    )
    llm = StubLLM([TRIAGE, plan, draft(("x", ["log:0"]))])
    final, _ = run(llm, monkeypatch, tool=fake_tool(n_logs=50, text_chars=200))
    assert llm.checked and max(llm.checked) <= 6000
    d = next(c for c in final["llm_calls"] if c["name"] == "draft")
    assert d["evidence_dropped"] > 0 and d["prompt_estimate"] + 1500 <= 6000


def test_near_identical_log_lines_are_collapsed_for_the_draft():
    ev = {
        f"log:{i}": {
            "id": f"log:{i}",
            "source": "log",
            "metadata": {"service": "a"},
            "text": f"2026-10-06T14:3{i}:00Z ERROR a-0{i} refused req=ab12cd3{i}",
        }
        for i in range(4)
    }
    lines = graph_mod.evidence_lines(ev)
    assert len(lines) == 1 and lines[0].startswith("[log:0]") and "3 similar" in lines[0]


def test_invalid_json_gets_one_retry_then_a_fallback(monkeypatch):
    llm = StubLLM([TRIAGE, calls(("query_logs", LOGS)), say("not json"), say("{}")])
    final, events = run(llm, monkeypatch, verify_enabled=False)
    names = [c["name"] for c in final["llm_calls"]]
    assert names == ["triage", "plan", "draft", "draft_retry"]
    assert "not valid" in llm.bodies[-1]["messages"][-1]["content"]
    assert final["diagnosis"]["confidence"] == "low" and "error" in kinds(events)


def test_json_in_fences_is_accepted():
    assert graph_mod.json_content('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert graph_mod.json_content('Here: {"a": 1} done') == '{"a": 1}'


@pytest.mark.parametrize("verify_enabled", [True, False])
def test_tracing_disabled_run_makes_no_langfuse_calls(monkeypatch, verify_enabled):
    import langfuse

    def boom(*a, **k):
        raise AssertionError("Langfuse must not be constructed when tracing is disabled")

    monkeypatch.setattr(langfuse, "Langfuse", boom)
    llm = StubLLM([TRIAGE, calls(("query_logs", LOGS)), draft(("x", ["log:0"]))])
    final, _ = run(llm, monkeypatch, verify_enabled=verify_enabled)
    assert final["diagnosis"] and llm._tracer is None
