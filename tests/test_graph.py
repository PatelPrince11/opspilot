"""agent/graph.py with the scripted stub LLM and fake tools: no database, no network."""

import pytest

from agent import graph as graph_mod
from agent.graph import MAX_LOOPS, build_graph, initial_state
from agent.tools import Evidence, ToolError, ToolOutput
from llm.client import LLMError
from tests.stub_llm import TRIAGE, StubLLM, calls, draft, say

Q = "The payment API is returning connection-refused errors. What happened?"
AS_OF = "2026-10-06T14:52:00Z"
LOGS = {"service": "payments-api", "start": "2026-10-06T14:00:00Z", "end": AS_OF}
DONE = say("done")  # a gather turn with no tool call ends the loop


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
    """The id-check tests run with the quote check off; the quote tests turn it on."""
    kw.setdefault("verify_quotes", False)
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
    llm = StubLLM([TRIAGE, calls(("query_logs", LOGS)), DONE, draft(("refused", ["log:1"]))])
    final, events = run(llm, monkeypatch)
    d = final["diagnosis"]
    assert d["citations_verified"] is True and d["claims"][0]["evidence_ids"] == ["log:1"]
    assert final["loops"] == 0 and llm.script == []
    assert kinds(events) == ["tool_call", "tool_result", "sources", "diagnosis"]
    assert [c["name"] for c in final["llm_calls"]] == ["triage", "gather", "gather", "draft"]
    assert all(c["prompt_tokens"] == 100 and not c["estimated"] for c in final["llm_calls"])


def test_verify_loop_fires_on_a_bad_citation_and_stops_at_two(monkeypatch):
    bad = draft(("ok claim", ["log:1"]), ("made up", ["log:999"]), confidence="high")
    script = [TRIAGE, calls(("query_logs", LOGS)), DONE]
    for _ in range(MAX_LOOPS):
        script += [bad, DONE]  # draft, then a gather turn that finds nothing more
    llm = StubLLM([*script, bad])
    final, _ = run(llm, monkeypatch)
    names = [c["name"] for c in final["llm_calls"]]
    assert final["loops"] == MAX_LOOPS == 2
    assert names.count("draft") == 3 and names.count("gather") == 4
    assert llm.script == []  # no fourth draft
    d = final["diagnosis"]
    assert d["citations_verified"] is False and d["confidence"] == "medium"
    assert [c["text"] for c in d["claims"]] == ["ok claim"]
    assert [c["text"] for c in d["dropped_claims"]] == ["made up"]
    assert "dropped after 2 verify loop(s)" in d["note"]
    # The resumed gather and the redraft are both told which ids were invalid.
    assert "log:999" in llm.bodies[4]["messages"][1]["content"]
    assert "log:999" in llm.bodies[5]["messages"][1]["content"]


def test_verify_loop_recovers_when_the_redraft_cites_gathered_evidence(monkeypatch):
    script = [
        TRIAGE,
        calls(("query_logs", LOGS)),
        DONE,
        draft(("x", ["log:999"])),
        calls(("search_runbooks", {"query": "pgbouncer"})),
        DONE,
        draft(("x", ["search_runbooks:1_0"])),
    ]
    final, _ = run(StubLLM(script), monkeypatch)
    assert final["loops"] == 1 and final["diagnosis"]["citations_verified"] is True
    assert final["diagnosis"]["confidence"] == "high"


def test_uncited_claims_fail_verify(monkeypatch):
    script = [TRIAGE, draft(("no ids", []))]
    final, _ = run(StubLLM(script), monkeypatch, max_seconds=0)  # out of time: no gather, no loop
    assert final["verify"]["invalid"][0]["bad_ids"] == ["(none cited)"]
    assert final["diagnosis"]["claims"] == []


def test_flag_disables_verify(monkeypatch):
    llm = StubLLM([TRIAGE, calls(("query_logs", LOGS)), DONE, draft(("made up", ["log:999"]))])
    final, events = run(llm, monkeypatch, verify_enabled=False)
    d = final["diagnosis"]
    assert d["citations_verified"] is None and d["claims"][0]["evidence_ids"] == ["log:999"]
    assert final.get("verify") is None and llm.script == []
    assert kinds(events)[-2:] == ["sources", "diagnosis"]


def test_gather_loop_sees_each_result_and_stops_on_done(monkeypatch):
    rb = ("search_runbooks", {"query": "pgbouncer refused"})
    llm = StubLLM([TRIAGE, calls(("query_logs", LOGS)), calls(rb), DONE, draft(("x", ["log:0"]))])
    final, _ = run(llm, monkeypatch)
    assert llm.script == [] and len(final["tool_calls"]) == 2
    prompts = [b["messages"][1]["content"] for b in llm.bodies[1:4]]  # the 3 gather turns
    assert "Latest result:\n(none yet)" in prompts[0]
    latest = prompts[1].split("Latest result:")[1]
    assert "[log:0]" in latest and "Tool calls so far (1 of 8)" in prompts[1]
    # Turn 3: the logs moved to the one-line index, the runbook result is the latest.
    index, latest = prompts[2].split("Latest result:")
    assert "[log:0]" in index and "[search_runbooks:1_0]" in latest
    assert "[log:0]" not in latest and "Tool calls so far (2 of 8)" in prompts[2]


def test_gather_loop_stops_at_the_cap(monkeypatch):
    # The model never says done: one call per turn until the cap, then no ninth gather turn.
    turns = [calls(("query_logs", LOGS | {"service": f"svc-{i}"})) for i in range(8)]
    llm = StubLLM([TRIAGE, *turns, draft(("x", ["log:0"]))])
    final, _ = run(llm, monkeypatch, max_tool_calls=8)
    names = [c["name"] for c in final["llm_calls"]]
    assert len(final["tool_calls"]) == 8 and names.count("gather") == 8 and llm.script == []


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
    assert [c["name"] for c in final["llm_calls"]].count("gather") == 1


def test_failed_tool_is_recorded_not_fatal(monkeypatch):
    llm = StubLLM(
        [TRIAGE, calls(("boom", {}), ("query_logs", LOGS)), DONE, draft(("x", ["log:1_0"]))]
    )
    final, _ = run(llm, monkeypatch)
    assert [t["success"] for t in final["tool_calls"]] == [False, True]
    assert "invalid arguments" in final["tool_calls"][0]["error"]


def test_no_call_exceeds_the_token_budget_on_a_full_gather(monkeypatch):
    # 8 gather turns, each returning 50 long log lines or 5 long snippets: far more than fits.
    turns = [
        c
        for i in range(4)
        for c in (
            calls(("query_logs", LOGS | {"service": f"svc-{i}"})),
            calls(("search_runbooks", {"query": f"q{i}"})),
        )
    ]
    llm = StubLLM([TRIAGE, *turns, draft(("x", ["log:0"]))])
    final, _ = run(llm, monkeypatch, tool=fake_tool(n_logs=50, text_chars=200))
    assert len(llm.checked) == 10 and max(llm.checked) <= 6000
    gathers = [b["messages"][1]["content"] for b in llm.bodies[1:9]]
    assert "more lines not shown" in gathers[1]  # a 50-line result is capped
    assert "more items not shown" in gathers[-1]  # the index is packed too
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
    llm = StubLLM([TRIAGE, calls(("query_logs", LOGS)), DONE, say("not json"), say("{}")])
    final, events = run(llm, monkeypatch, verify_enabled=False)
    names = [c["name"] for c in final["llm_calls"]]
    assert names == ["triage", "gather", "gather", "draft", "draft_retry"]
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
    llm = StubLLM([TRIAGE, calls(("query_logs", LOGS)), DONE, draft(("x", ["log:0"]))])
    final, _ = run(llm, monkeypatch, verify_enabled=verify_enabled)
    assert final["diagnosis"] and llm._tracer is None


# ---------------------------------------------------------------------- quote check
# fake_tool's first query_logs returns log:0 = "2026-10-06T14:00:00Z ERROR aaz xxx...".


def test_a_real_quote_passes(monkeypatch):
    claim = ("refused", ["log:0"], "ERROR aaz xxxxxxxxxx")
    llm = StubLLM([TRIAGE, calls(("query_logs", LOGS)), DONE, draft(claim)])
    final, _ = run(llm, monkeypatch, verify_quotes=True)
    assert final["diagnosis"]["citations_verified"] is True and final["loops"] == 0
    assert final["verify_log"] == [{"claims": 1, "bad_ids": 0, "bad_quotes": 0}]


def test_an_invented_quote_fails_loops_then_drops_the_claim(monkeypatch):
    real = ("ok", ["log:0"], "ERROR aaz xxxxxxxxxx")
    fake = ("pool exhausted", ["log:0"], "PAYAPI-E1042 pool exhausted on payments-api")
    bad = draft(real, fake, confidence="high")
    llm = StubLLM([TRIAGE, calls(("query_logs", LOGS)), DONE, bad, DONE, bad, DONE, bad])
    final, _ = run(llm, monkeypatch, verify_quotes=True)
    d = final["diagnosis"]
    assert final["loops"] == MAX_LOOPS and llm.script == []
    assert [c["text"] for c in d["dropped_claims"]] == ["pool exhausted"]
    assert d["citations_verified"] is False and d["confidence"] == "medium"
    assert final["verify_log"][-1] == {"claims": 2, "bad_ids": 0, "bad_quotes": 1}
    assert '"quote_found": false' in llm.bodies[-1]["messages"][1]["content"]  # redraft told


def test_quote_check_flag_off_keeps_only_the_id_check(monkeypatch):
    fake = ("pool exhausted", ["log:0"], "PAYAPI-E1042 pool exhausted on payments-api")
    llm = StubLLM([TRIAGE, calls(("query_logs", LOGS)), DONE, draft(fake)])
    final, _ = run(llm, monkeypatch, verify_quotes=False)
    assert final["diagnosis"]["citations_verified"] is True


@pytest.mark.parametrize(
    "quote, ok",
    [
        ("Main process exited, code=exited", True),
        ("  main   PROCESS\n exited,  CODE=exited ", True),  # whitespace and case
        ("\u201cMain process exited, code=exited\u2026\u201d", True),  # curly quotes, ellipsis
        ("Main process crashed, code=exited", False),  # one word changed
        ("Main process", False),  # too short to mean anything
        ("", False),
    ],
)
def test_quote_normalization(quote, ok):
    ev = {
        "log:7": {
            "text": "2026-10-06T14:30:41Z ERROR pgbouncer.service: Main process exited, "
            "code=exited, status=1/FAILURE"
        }
    }
    assert graph_mod.quote_found(quote, ["log:7"], ev) is ok
    assert graph_mod.quote_found(quote, ["log:8"], ev) is False  # an uncited/ungathered id


def test_quote_must_come_from_a_cited_item_not_any_item():
    ev = {"a": {"text": "disk usage at 99 percent on payments-db-01"}, "b": {"text": "all fine"}}
    assert graph_mod.quote_found("disk usage at 99 percent", ["a"], ev) is True
    assert graph_mod.quote_found("disk usage at 99 percent", ["b"], ev) is False


def test_a_repeated_call_is_not_run_again(monkeypatch):
    # Same call in a later turn and twice in one turn: run once, the repeats fail and count.
    llm = StubLLM(
        [
            TRIAGE,
            calls(("query_logs", LOGS)),
            calls(("query_logs", LOGS), ("query_logs", LOGS)),
            DONE,
            draft(("x", ["log:0"])),
        ]
    )
    seen = []
    tool = fake_tool()
    final, _ = run(llm, monkeypatch, tool=lambda n, a, c: seen.append(n) or tool(n, a, c))
    assert seen == ["query_logs"] and len(final["tool_calls"]) == 3
    assert [t["success"] for t in final["tool_calls"]] == [True, False, False]
    assert "duplicate of call 1" in final["tool_calls"][1]["error"]
    assert "duplicate of call 1" in llm.bodies[3]["messages"][1]["content"]  # the model is told


def test_groq_json_validate_failed_is_retried_as_invalid_output(monkeypatch):
    def rejected(body):
        raise LLMError('groq 400: {"error": {"code": "json_validate_failed"}}')

    llm = StubLLM([TRIAGE, DONE, rejected, draft()])
    final, _ = run(llm, monkeypatch)
    assert final["diagnosis"]["summary"] == "s" and llm.script == []
    assert "not valid" in llm.bodies[-1]["messages"][-1]["content"]


def test_other_llm_errors_still_raise(monkeypatch):
    def down(body):
        raise LLMError("groq 503: unavailable")

    with pytest.raises(LLMError, match="503"):
        run(StubLLM([TRIAGE, DONE, down]), monkeypatch)


def test_a_first_reply_in_prose_gets_one_nudge_then_the_call_runs(monkeypatch):
    """Q003 on gpt-oss: the first gather turn described the log window in prose, no tool call."""
    prose = say("The window is 2025-02-20T00:00:00Z to 2025-02-20T23:59:59Z")
    llm = StubLLM([TRIAGE, prose, calls(("query_logs", LOGS)), DONE, draft(("x", ["log:0"]))])
    final, _ = run(llm, monkeypatch)
    assert llm.script == [] and len(final["tool_calls"]) == 1
    assert graph_mod.NUDGE in llm.bodies[2]["messages"][1]["content"]
    assert graph_mod.NUDGE not in llm.bodies[3]["messages"][1]["content"]


def test_prose_twice_or_done_first_ends_gather(monkeypatch):
    prose = say("I would look at the logs.")
    llm = StubLLM([TRIAGE, prose, prose, draft()])
    final, _ = run(llm, monkeypatch)
    assert llm.script == [] and final["tool_calls"] == []
    llm = StubLLM([TRIAGE, say("Done: nothing to look up."), draft()])
    final, _ = run(llm, monkeypatch)
    assert llm.script == [] and final["tool_calls"] == []
