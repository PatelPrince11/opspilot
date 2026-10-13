"""eval/judge.py and eval/generation_eval.py on fixtures: no network, no database."""

import json

import httpx
import pytest

from eval import generation_eval as ge
from eval.judge import (
    NOT_GATHERED,
    PROMPT_VERSION,
    JudgeClient,
    RateLimiter,
    Verdict,
    fits,
    judge,
    prompt,
)
from llm.client import LLMError

Q = {
    "id": "Q900",
    "question": "Why did logs-01 fill up in March 2026?",
    "expected_key_facts": ["log rotation stopped", "old archives were deleted"],
    "difficulty": "exact-token",
    "author": "claude",
}
EVIDENCE = {
    "INC-9002#0": {
        "id": "INC-9002#0",
        "source": "incident",
        "metadata": {},
        "text": "The disk"
        " on logs-01 filled up because log rotation stopped; the node went read-only.",
    },
    "INC-9002#1": {
        "id": "INC-9002#1",
        "source": "incident",
        "metadata": {},
        "text": "The team deleted old archives and restarted logrotate on logs-01.",
    },
}


def claim(text, ids, quote):
    return {"text": text, "evidence_ids": ids, "quote": quote}


DIAG = {
    "summary": "Log rotation stopped on logs-01.",
    "likely_cause": "logrotate stopped",
    "confidence": "high",
    "claims": [
        claim("rotation stopped", ["INC-9002#0"], "because log rotation stopped"),
        claim("archives deleted", ["INC-9002#1"], "deleted old archives and restarted"),
        claim("bad quote", ["INC-9002#1"], "the disk was replaced by the team"),
        claim("not gathered", ["RB-404#0"], "anything at all here"),
    ],
    "dropped_claims": [],
    "next_checks": [],
}
VERDICT = {
    "claims": [{"claim": i, "supported": i < 3} for i in range(1, 5)],
    "key_facts": [{"fact": 1, "present": True}, {"fact": 2, "present": False}],
    "faithfulness": 4,
    "premise_corrected": None,
    "unrecorded_respected": None,
}


def judge_client(handler, **kw):
    return JudgeClient(
        "gemini", "judge-m", transport=httpx.MockTransport(handler), sleep=lambda s: None, **kw
    )


@pytest.fixture
def keys(monkeypatch):
    from config import get_settings

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    get_settings.cache_clear()


def reply(obj):
    content = obj if isinstance(obj, str) else json.dumps(obj)
    return {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


# ---------------------------------------------------------------------- judge


def test_prompt_shows_only_cited_evidence_and_flags_ungathered_ids():
    extra = EVIDENCE | {"LOG:1": {"id": "LOG:1", "text": "an uncited log line SECRET"}}
    user = prompt(Q, DIAG, extra)[1]["content"]
    assert "[INC-9002#0] The disk on logs-01" in user and "[INC-9002#1] The team" in user
    assert f"[RB-404#0] {NOT_GATHERED}" in user
    assert "SECRET" not in user  # gathered but not cited: the judge never sees it
    assert "1. log rotation stopped" in user and "premise_corrected: null" in user
    assert PROMPT_VERSION in prompt(Q, DIAG, extra)[0]["content"]


def test_false_premise_and_unrecorded_questions_get_their_rubric():
    q = Q | {"false_premise": True, "unrecorded": ["which table"]}
    user = prompt(q, DIAG, EVIDENCE)[1]["content"]
    assert "false premise" in user and "not invent" in user and ": null" not in user


def test_long_evidence_is_cut_to_fit_the_budget():
    big = {k: v | {"text": "word " * 2000} for k, v in EVIDENCE.items()}
    llm = JudgeClient("gemini", "m", budget=3000, cache_dir=None)
    msgs = fits(llm, Q, DIAG, big)
    assert len(msgs[1]["content"]) < len(prompt(Q, DIAG, big)[1]["content"])


def test_judge_parses_a_valid_verdict_at_temperature_zero(keys):
    sent = []

    def handler(req):
        sent.append(json.loads(req.content))
        return httpx.Response(200, json=reply(VERDICT))

    v, calls = judge(judge_client(handler, cache_dir=None), Q, DIAG, EVIDENCE)
    assert isinstance(v, Verdict) and v.faithfulness == 4 and len(calls) == 1
    assert sent[0]["temperature"] == 0.0 and sent[0]["response_format"]["type"] == "json_schema"


def test_incomplete_verdict_is_retried_then_given_up(keys):
    short = VERDICT | {"claims": VERDICT["claims"][:2]}
    replies = [reply(short), reply("not json")]
    v, calls = judge(
        judge_client(lambda r: httpx.Response(200, json=replies.pop(0)), cache_dir=None),
        Q,
        DIAG,
        EVIDENCE,
    )
    assert v is None and [c["name"] for c in calls] == ["judge", "judge_retry"]


def test_judge_is_cached_and_a_cache_hit_skips_the_rate_limiter(keys, tmp_path):
    n = []
    llm = judge_client(
        lambda r: n.append(1) or httpx.Response(200, json=reply(VERDICT)), cache_dir=tmp_path
    )
    clock = [0.0]
    llm.limiter = RateLimiter(
        15,
        500,
        tmp_path / "count.json",
        clock=lambda: clock[0],
        sleep=lambda s: clock.__setitem__(0, clock[0] + s),
    )
    judge(llm, Q, DIAG, EVIDENCE)
    judge(llm, Q, DIAG, EVIDENCE)
    assert len(n) == 1 and json.loads((tmp_path / "count.json").read_text()) == {
        "1969-12-31": 1
    }  # Pacific


def test_rate_limiter_spaces_requests_and_stops_at_the_daily_cap(tmp_path):
    clock, slept = [1000.0], []

    def sleep(s):
        slept.append(s)
        clock[0] += s

    lim = RateLimiter(15, 3, tmp_path / "c.json", clock=lambda: clock[0], sleep=sleep)
    for _ in range(3):
        lim.acquire()
    assert slept == [4.0, 4.0]  # 60 / 15 seconds apart
    with pytest.raises(LLMError, match="3 requests already"):
        lim.acquire()
    resumed = RateLimiter(15, 3, tmp_path / "c.json", clock=lambda: clock[0], sleep=sleep)
    with pytest.raises(LLMError):  # the count survives a restart
        resumed.acquire()


# ---------------------------------------------------------------------- scoring


def run_row(diag=DIAG, loops=1, calls=None):
    return {
        "id": "Q900",
        "diagnosis": diag,
        "cited_evidence": EVIDENCE,
        "tool_calls": calls
        or [{"name": "search_incidents", "success": True, "error": None, "latency_ms": 5.0}] * 3
        + [{"name": "read_document", "success": False, "error": "x", "latency_ms": 1.0}],
        "llm_calls": [
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "cost_usd": 0.001,
                "cached": False,
                "latency_ms": 50.0,
                "wait_s": 0.0,
            }
        ],
        "loops": loops,
        "wall_s": 3.0,
    }


def test_score_citation_a_is_deterministic_and_b_comes_from_the_judge():
    s = ge.score(Q, run_row(), VERDICT)
    assert s["citation_a"] == [2, 4]  # bad quote and ungathered id fail
    assert s["citation_b"] == [2, 4] and s["completeness"] == [1, 2]
    assert s["faithfulness"] == [4, 1] and s["tool_success"] == [3, 4]
    assert s["verify_loop"] == [1, 1] and s["no_diagnosis"] == [0, 1]
    assert s["premise_corrected"] is None and s["unrecorded_respected"] is None


def test_score_without_a_verdict_keeps_only_deterministic_metrics():
    s = ge.score(Q, run_row(), None)
    assert s["citation_a"] == [2, 4] and s["citation_b"] is None and s["completeness"] is None


def test_score_flags_fallback_and_premise():
    empty = DIAG | {"summary": ge.NO_DIAGNOSIS, "claims": []}
    v = VERDICT | {"claims": [], "premise_corrected": True, "unrecorded_respected": False}
    q = Q | {"false_premise": True, "unrecorded": ["x"]}
    s = ge.score(q, run_row(empty, loops=0), v)
    assert s["no_diagnosis"] == [1, 1] and s["citation_a"] is None and s["citation_b"] is None
    assert s["premise_corrected"] == [1, 1] and s["unrecorded_respected"] == [0, 1]


def test_aggregate_pools_over_claims_and_reports_n():
    rows = [
        {
            "id": "a",
            "scores": {m: None for m in ge.METRICS} | {"citation_a": [1, 1]},
            "llm_calls": [{"prompt_tokens": 10, "completion_tokens": 1}],
            "tool_calls": [],
        },
        {
            "id": "b",
            "scores": {m: None for m in ge.METRICS} | {"citation_a": [0, 3]},
            "llm_calls": [],
            "tool_calls": [],
        },
    ]
    a = ge.aggregate(rows)
    assert a["citation_a"] == {"value": 0.25, "n": 2, "den": 4}
    assert a["citation_b"] == {"value": None, "n": 0, "den": 0}
    assert a["per_run"]["n"] == 2 and a["per_run"]["prompt_tokens"] == 5


def test_paired_bootstrap_is_seeded_and_zero_for_identical_configs():
    a = {str(i): [i % 2, 1] for i in range(40)}
    assert ge.paired_bootstrap(a, a, sorted(a)) == (0.0, 0.0, 0.0)
    b = {k: [1, 1] for k in a}
    d, lo, hi = ge.paired_bootstrap(b, a, sorted(a))
    assert d == 0.5 and 0 < lo < 0.5 < hi and ge.paired_bootstrap(b, a, sorted(a)) == (d, lo, hi)


def test_latency_ignores_cache_hits():
    r = run_row()
    r["llm_calls"].append(r["llm_calls"][0] | {"cached": True, "latency_ms": 0.0})
    lat = ge.latency([r, run_row()])
    assert lat["llm_call_ms"] == {"p50": 50.0, "p95": 50.0, "n": 2}
    assert lat["end_to_end_s"]["n"] == 1  # the run with a cache hit is not timed


# ---------------------------------------------------------------------- resumable agent stage


def test_agent_stage_resumes_and_stops_at_the_spend_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(ge, "PROGRESS", tmp_path)
    ran = []

    def fake_run(q, verify, spec):
        ran.append(q["id"])
        return run_row() | {"id": q["id"], "verify_enabled": verify}

    monkeypatch.setattr(ge, "run_one", fake_run)
    qs = [Q | {"id": f"Q{i}"} for i in range(5)]
    ge.append_jsonl(ge.runs_path("dev", "verify_on"), run_row() | {"id": "Q0"})
    ge.agent_stage(qs, "verify_on", "dev", "x:y", workers=1, max_spend=0.0025)
    # Q0 was done; each run costs $0.001 new and Q0's counts too, so two more fit under $0.0025.
    assert ran == ["Q1", "Q2"]
    assert sorted(ge.read_jsonl(ge.runs_path("dev", "verify_on"))) == ["Q0", "Q1", "Q2"]
    ge.agent_stage(qs, "verify_on", "dev", "x:y", workers=2, max_spend=1.0)
    assert ran == ["Q1", "Q2", "Q3", "Q4"]  # never re-runs a finished question


def test_cached_calls_are_not_new_spend():
    r = run_row()
    r["llm_calls"].append(r["llm_calls"][0] | {"cached": True})
    assert ge.new_spend([r]) == pytest.approx(0.001)


# ---------------------------------------------------------------------- hand-label sheet


def test_pick_stratified_is_proportional_and_seeded():
    strata = {
        ("a", "x"): [f"a{i}" for i in range(50)],
        ("b", "y"): [f"b{i}" for i in range(25)],
        ("c", "z"): [f"c{i}" for i in range(25)],
    }
    p = ge.pick_stratified(strata, 30, 0)
    assert len(p) == len(set(p)) == 30 and sum(i[0] == "a" for i in p) == 15
    assert p == ge.pick_stratified(strata, 30, 0) and p != ge.pick_stratified(strata, 30, 1)


def test_sheet_round_trip_agreement_and_kappa(tmp_path, monkeypatch):
    monkeypatch.setattr(ge, "PROGRESS", tmp_path)
    monkeypatch.setattr(ge, "SHEET_N", 1)
    ge.append_jsonl(ge.runs_path("dev", "verify_on"), run_row())
    text = ge.sheet([Q])
    assert "SUPPORTED Q900 C4: ___" in text and "PRESENT Q900 K2: ___" in text
    assert "> `RB-404#0` (not gathered)" in text and "supported" not in text.split("## 1.")[1]
    results = {"split": "dev", "commit": "c" * 40, "questions": [{"id": "Q900", "judge": VERDICT}]}
    assert ge.agreement_report(text, results, [Q])["blank"] == 6
    # Prince: C1 yes, C2 yes, C3 yes (judge no), C4 no; K1 yes, K2 no -> 5 of 6 agree.
    filled = text
    for mark, val in [
        ("C1", "yes"),
        ("C2", "yes"),
        ("C3", "yes"),
        ("C4", "no"),
        ("K1", "yes"),
        ("K2", "no"),
    ]:
        filled = filled.replace(f"Q900 {mark}: ___", f"Q900 {mark}: {val}")
    a = ge.agreement_report(filled, results, [Q])
    o = a["overall"]
    assert a["blank"] == 0 and o["all"]["n"] == 6 and o["all"]["agreement"] == pytest.approx(5 / 6)
    assert o["claims"]["agreement"] == 0.75 and o["key_facts"]["agreement"] == 1.0
    assert o["key_facts"]["kappa"] == 1.0
    # Judge: C1, C2 supported; C3, C4 not. Prince: C1-C3 yes, C4 no.
    assert o["claims"]["table"] == {
        "prince_yes_judge_yes": 2,
        "prince_yes_judge_no": 1,
        "prince_no_judge_yes": 0,
        "prince_no_judge_no": 1,
    }
    assert o["claims"]["prince_yes_judge_no"] == 1 and o["claims"]["prince_no_judge_yes"] == 0
    assert a["disagreements"] == [{"id": "Q900", "item": "C3", "prince": True, "judge": False}]
    assert a["by"]["author=claude"]["claims"]["n"] == 4
    assert a["sensitivity"]["citation_b"] == {
        "n_items": 4,
        "n_questions": 1,
        "judge": 0.5,
        "prince": 0.75,
    }
    assert a["sensitivity"]["completeness"]["judge"] == a["sensitivity"]["completeness"]["prince"]


def test_kappa_known_values():
    assert ge.kappa([(True, True), (False, False)]) == 1.0
    # po 0.5, pe 0.5 -> 0
    assert ge.kappa([(True, True), (True, False), (False, True), (False, False)]) == 0.0
    assert ge.kappa([(True, True)] * 3) is None  # pe = 1: undefined
    assert ge.kappa([]) is None


def test_ledger_renders_generation_rows_from_result_files(monkeypatch):
    from scripts import results as ledger

    qs = [Q | {"id": f"Q{i}", "difficulty": d} for i, d in enumerate(["exact-token", "paraphrase"])]

    def content(config, verdict):
        rows = [
            {
                "id": q["id"],
                "scores": ge.score(q, run_row(), verdict),
                "judge": verdict,
                "claims": 4,
                "dropped_claims": 0,
                "loops": 1,
                "llm_calls": [
                    {
                        "prompt_tokens": 10,
                        "completion_tokens": 2,
                        "cost_usd": 0.001,
                        "cached": False,
                    }
                ],
                "tool_calls": [{"success": True}],
            }
            for q in qs
        ]
        c = ge.result_content("dev", config, rows, qs, [])
        lat = ge.latency([run_row()])
        return c | {"commit": "a" * 40, "uncommitted_changes": [], "latency": lat}

    files = {
        "generation_verify_on": content("verify_on", VERDICT),
        "generation_verify_off": content("verify_off", None),
    }
    monkeypatch.setattr(ledger, "load_named", lambda split, name: files.get(name))
    text = "\n".join(ledger.generation_section(qs))
    assert "| citation_a |" in text and "| verify on | 2 | 0.500 (8) |" in text
    assert "| citation_b | difference" not in text  # off has no judge: no b comparison rows
    assert "| citation_a | overall | 2 | +0.000 |" in text
    assert "| citation_a | overall | 2 | +0.000 | [+0.000, +0.000] | no |" in text
    assert "| tool_success | overall | 2 | +0.000 | [+0.000, +0.000] | not a verify effect" in text
    assert "verify off is the same trajectory's first draft" in text
    assert "| difficulty=paraphrase | 1 |" in text
    files["generation_verify_off"]["latency"]["llm_call_ms"] = {"p50": None, "p95": None, "n": 0}
    text = "\n".join(ledger.generation_section(qs))
    assert "| all | verify off | - | not measured: every LLM call was a cache hit" in text
    monkeypatch.setattr(ledger, "load_named", lambda split, name: None)
    assert ledger.generation_section(qs) == []


def test_a_cache_replay_keeps_the_measured_latency_and_spend(tmp_path, monkeypatch):
    monkeypatch.setattr(ge, "PROGRESS", tmp_path)
    monkeypatch.setattr(ge, "RESULTS", tmp_path)
    monkeypatch.setattr(ge, "store", lambda *a: None)
    measured = run_row()
    ge.append_jsonl(ge.runs_path("dev", "verify_on"), measured)
    ge.write_results([Q], "verify_on", "dev")
    path = tmp_path / "dev_generation_verify_on.json"
    first = json.loads(path.read_text())
    assert first["latency"]["llm_call_ms"]["n"] == 1 and first["spend_usd"] == 0.001
    # The same run replayed from the cache, now with a new metric field: content changes.
    replay = run_row()
    replay["llm_calls"] = [c | {"cached": True, "latency_ms": 0.0} for c in replay["llm_calls"]]
    replay["gathered_docs"] = ["INC-9002"]
    ge.runs_path("dev", "verify_on").write_text(json.dumps(replay) + "\n")
    ge.write_results([Q | {"relevant_doc_ids": ["INC-9002"]}], "verify_on", "dev")
    second = json.loads(path.read_text())
    assert second["metrics"]["overall"]["gold_all"]["value"] == 1.0
    assert second["latency"] == first["latency"] and second["spend_usd"] == 0.001


def test_a_replay_never_inherits_another_configs_latency(tmp_path, monkeypatch):
    monkeypatch.setattr(ge, "PROGRESS", tmp_path)
    monkeypatch.setattr(ge, "RESULTS", tmp_path)
    monkeypatch.setattr(ge, "store", lambda *a: None)
    path = tmp_path / "dev_generation_verify_off.json"
    ge.append_jsonl(ge.runs_path("dev", "verify_off"), run_row())
    ge.write_results([Q], "verify_off", "dev")
    other = json.loads(path.read_text())
    other["config"]["draft_effort"] = "something else"  # a file left by another config
    path.write_text(json.dumps(other))
    replay = run_row()
    replay["llm_calls"] = [c | {"cached": True} for c in replay["llm_calls"]]
    ge.runs_path("dev", "verify_off").write_text(json.dumps(replay) + "\n")
    ge.write_results([Q], "verify_off", "dev")
    lat = json.loads(path.read_text())["latency"]
    assert lat["llm_call_ms"]["n"] == 0 and lat["end_to_end_s"]["n"] == 0


def test_kappa_ci_resamples_questions_and_is_seeded():
    items = [{"id": f"q{i}", "prince": i % 3 != 0, "judge": i % 2 == 0} for i in range(30)]
    ci = ge.kappa_ci(items)
    assert (
        ci == ge.kappa_ci(items)
        and ci[0] <= ge.kappa([(i["prince"], i["judge"]) for i in items]) <= ci[1]
    )
    assert ge.kappa_ci([{"id": "a", "prince": True, "judge": True}]) is None  # pe = 1 always


def test_ledger_rule_by_kappa():
    from scripts.results import kappa_rule

    assert kappa_rule(-0.05).startswith("unreliable")
    assert kappa_rule(0.47).startswith("moderate")
    assert kappa_rule(0.7).startswith("report normally")
    assert kappa_rule(None) == "not validated"
