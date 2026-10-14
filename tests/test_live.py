"""The live-incident set (eval/live_questions.jsonl, data/live/LIVE-*.toml): answer keys that the
logs can answer, logs visible only to their own case, and the deterministic scoring."""

import json
import re
import tomllib
from datetime import datetime, timedelta

import psycopg
import pytest

from agent.tools import connect, run_tool
from eval import live_eval as le
from scripts.corpus.common import DATA, PROCESSED
from tests.test_tools import url  # noqa: F401  (fixture)

CASES = le.load_cases()
DEMO = tomllib.loads((DATA.parent / "eval" / "corpus" / "demo_answer.toml").read_text())
DEMO_AS_OF = datetime.fromisoformat("2026-10-06T14:52:00+00:00")
PAST = re.compile(
    r"\b(19|20)\d\d\b|\b(january|february|march|april|may|june|july|august|september|october"
    r"|november|december|yesterday|last (week|night|month|year)|INC-\w+)\b",
    re.I,
)


def as_of(c) -> datetime:
    return datetime.fromisoformat(c["as_of"].replace("Z", "+00:00"))


def test_ten_cases_five_cause_types_two_distractors_one_spec_each():
    assert [c["id"] for c in CASES] == [f"LIVE-{i:02d}" for i in range(1, 11)]
    assert len({c["cause_type"] for c in CASES}) >= 5
    assert sum(c["distractor"] is not None for c in CASES) == 2
    assert sorted(p.stem for p in le.SPECS.glob("LIVE-*.toml")) == [c["id"] for c in CASES]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_question_is_live_with_no_past_date_or_incident(case):
    assert not PAST.search(case["question"]), case["question"]
    assert "right now" in case["question"] or "a little while ago" in case["question"]
    assert as_of(case) > DEMO_AS_OF


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_the_logs_hold_the_answer_and_the_key_is_consistent(case):
    events = le.case_events(case["id"])  # also runs the services.toml host and code checks
    text = "\n".join(e["message"] for e in events)
    assert all(as_of(case) - timedelta(hours=1) <= e["ts"] <= as_of(case) for e in events)
    assert {e["gold"] for e in events if "gold" in e}, "no gold entry"
    for f in case["key_facts"]:
        assert any(le.has_token(text, t) for t in f["any"]), f"not in the logs: {f['fact']}"
        assert any(le.has_token(case["cause"], t) for t in f["any"]), f"key misses {f['fact']}"
    for f in case["forbidden"]:
        assert not le.forbidden_hits(case["cause"], f["pattern"]), f["claim"]
    hosts = {e["message"].split(" ", 1)[0] for e in events}
    assert set(case["hosts"]) <= hosts


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_no_spec_is_demo_like(case):
    text = (le.SPECS / f"{case['id']}.toml").read_text()
    assert "payments-pgbouncer" not in text
    assert not all(x in text for x in DEMO["leak_combination"])


def test_live_logs_never_reach_the_corpus_build():
    rows = [
        json.loads(line) for line in (PROCESSED / "log_events.live.jsonl").read_text().splitlines()
    ]
    assert rows and all(r["ts"] < "2026-10-07" for r in rows)  # the demo's logs only


def test_forbidden_claims_skip_negated_sentences_and_key_facts_are_whole_tokens():
    text = "Redis refuses writes with MISCONF. The fork succeeded, so it is not overcommit."
    assert le.forbidden_hits(text, r"MISCONF") == ["Redis refuses writes with MISCONF."]
    assert le.forbidden_hits(text, r"overcommit") == []
    assert le.forbidden_hits("kafka-01 has no space left.", r"kafka-01[^.]*no space") != []
    assert le.has_token("on kafka-02.prod.internal", "kafka-02")
    assert not le.has_token("on kafka-021", "kafka-02") and not le.has_token("x64", "64")


def test_score_counts_facts_forbidden_claims_and_gold_entries():
    case = CASES[0]
    gold = le.gold_ids(case["id"])
    one_per_entry = {}
    for i, g in gold.items():
        one_per_entry.setdefault(g, i)
    run = {
        "diagnosis": {
            "summary": "kafka-02 ran out of disk.",
            "likely_cause": "/data/kafka hit ENOSPC.",
            "claims": [{"text": "kafka-03 is down."}],
        },
        "log_ids": [one_per_entry["line0"], 1],
        "tool_calls": [{"success": True}, {"success": False}],
        "triage": {"incident_time": "live"},
    }
    s = le.score(case, run, gold)
    assert s["key_facts_found"] == 3 and s["all_key_facts"]
    assert list(s["forbidden_hits"]) == ["kafka-01 or kafka-03 is full or down"]
    assert (s["gold_covered"], s["gold_total"]) == (1, 3)
    assert (s["tool_calls"], s["tool_failures"]) == (2, 1)


def test_sign_test_is_two_sided_exact():
    assert le.sign_test(0, 0) == 1.0 and le.sign_test(5, 5) == 1.0
    assert le.sign_test(8, 0) == pytest.approx(2 / 256)


@pytest.mark.db
def test_live_logs_are_visible_only_to_their_own_case(url):  # noqa: F811
    with psycopg.connect(url) as conn:
        n = le.load_corpus(conn, CASES)
    assert n == sum(len(le.case_events(c["id"])) for c in CASES)
    args = {"service": "kafka", "start": "2026-10-08T09:40:00Z", "end": "2026-10-08T10:40:00Z"}
    with connect(url) as ro:
        assert run_tool("query_logs", args, ro).items == []  # no case: what every other eval sees
        for other in ("LIVE-02", "INJ-01"):
            ro.execute("SELECT set_config('opspilot.injection_case', %s, false)", [other])
            assert run_tool("query_logs", args, ro).items == []
        ro.execute("SELECT set_config('opspilot.injection_case', 'LIVE-01', false)")
        items = run_tool("query_logs", args, ro).items
        assert items and all(int(e.id[4:]) // 1000 == 951 for e in items)
    with psycopg.connect(url) as conn:  # a reload replaces, never duplicates
        le.load_corpus(conn, CASES)
        assert (
            conn.execute("SELECT count(*) FROM log_events WHERE corpus = 'live'").fetchone()[0] == n
        )


def test_posthoc_ascii_fold_only_changes_unicode_hyphens_and_spaces():
    d = {"summary": "EDGE‑RL01 at 250 tokens", "likely_cause": "x", "claims": []}
    assert not le.has_token(le.answer_text(d), "EDGE-RL01")
    assert le.has_token(le.answer_text(d, ascii_fold=True), "EDGE-RL01")
    assert le.answer_text(d, ascii_fold=True) == "EDGE-RL01 at 250 tokens\nx"
