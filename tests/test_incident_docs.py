"""Incident write-ups and synthetic logs (Phase 1 step 3, batch 5). The build validator
(scripts/corpus/incidents.py, synthetic_logs.py) checks a write-up against its skeleton row;
these tests add the checks that need eval-only labels (eval/corpus/), arithmetic, diversity
and leakage, plus one broken input per validator rule."""

import copy
import re
import tomllib
from datetime import date, timedelta
from itertools import combinations

import pytest

from scripts import build_corpus
from scripts.corpus import synthetic_logs
from scripts.corpus.common import DATA, SIDECAR
from scripts.corpus.incidents import (
    DOC_FORMATS,
    TIME_RE,
    doc_text,
    load_documents,
    load_skeleton,
    validate_documents,
)
from scripts.corpus.runbooks import headings, load_runbooks, word_count
from scripts.corpus.services import CorpusError, load_services
from tests.test_corpus import TRIGRAM_JACCARD_THRESHOLD, leaks_demo, trigrams

BATCH = {
    "INC-0023",
    "INC-0048",
    "INC-0055",
    "INC-0067",
    "INC-0099",
    "INC-0101",
    "INC-0163",
    "INC-0168",
    "INC-0171",
    "INC-0185",
}
JACCARD_TARGET = 0.025  # working ceiling; the test threshold stays TRIGRAM_JACCARD_THRESHOLD
SHARED_RUN = 8  # no run of this many words shared with a runbook or another write-up
WORDS = {
    "postmortem": (450, 900),
    "table": (350, 700),
    "ticket": (500, 950),
    "handoff": (250, 500),
    "qa": (350, 700),
    "brief": (100, 220),
    "narrative": (350, 650),
    "timeline": (300, 650),
    "chat_digest": (300, 550),
}


@pytest.fixture(scope="module")
def services_doc():
    return load_services(DATA / "services.toml")


@pytest.fixture(scope="module")
def rows():
    return load_skeleton(DATA / "incidents" / "skeleton.toml")


@pytest.fixture(scope="module")
def by_id(rows):
    return {r["id"]: r for r in rows}


@pytest.fixture(scope="module")
def side():
    with (SIDECAR / "incidents_sidecar.toml").open("rb") as f:
        return {s["id"]: s for s in tomllib.load(f)["incidents"]}


@pytest.fixture(scope="module")
def docs():
    return {d["meta"]["id"]: d for d in load_documents(DATA / "incidents")}


@pytest.fixture(scope="module")
def runbooks():
    return load_runbooks(DATA / "runbooks")


@pytest.fixture(scope="module")
def rb_ids(runbooks):
    return {r["meta"]["id"] for r in runbooks}


@pytest.fixture(scope="module")
def events(rows, docs, services_doc):
    out = {}
    for e in build_corpus.synthetic_events(rows, list(docs.values()), services_doc):
        out.setdefault(e["incident_id"], []).append(e)
    return out


def has_token(text, token):
    return re.search(rf"(?<![\w.,]){re.escape(token)}(?!\w|[.,]\d)", text) is not None


# ------------------------------------------------------------------ validator


def test_batch_documents_validate(docs, rows, services_doc, rb_ids):
    assert set(docs) == BATCH
    validate_documents(list(docs.values()), rows, services_doc, rb_ids)


def body_add(text):
    def f(d):
        d["body"] += f"\n{text}\n"

    return f


def body_replace(old, new):
    def f(d):
        d["body"] = d["body"].replace(old, new)
        for k in ("symptoms", "root_cause", "resolution", "title"):
            d["meta"][k] = d["meta"][k].replace(old, new)

    return f


@pytest.mark.parametrize(
    ("inc", "mutate", "message"),
    [
        (
            "INC-0055",
            body_add("Seen on payments-db-01.prod.internal."),
            "not one of the row's hosts",
        ),
        ("INC-0185", body_add("kafka-03 was next."), "'kafka-03' is not one of the row's hosts"),
        ("INC-0185", body_add("No `ORD-5003` at all."), "'ORD-5003' is not one of the row's codes"),
        ("INC-0048", body_add("Edge returned 429."), "'429' is not one of the row's codes"),
        ("INC-0048", body_add("PAYAPI-E9999 too."), "unknown internal code"),
        ("INC-0185", body_replace("LEDGER-E455", "the retry code"), "row code 'LEDGER-E455'"),
        ("INC-0048", body_add("See RB-016."), "runbook references"),
        ("INC-0055", body_replace("RB-010", "the pool runbook"), "runbook references"),
        ("INC-0055", body_add("Like INC-0042."), "no incident references"),
        ("INC-0055", body_add("Unlike INC-LIVE-001."), "malformed reference"),
        ("INC-0055", body_add("Raised to sev1 briefly."), "severity stated"),
        ("INC-0055", body_add("The mechanism was simple."), "eval label word"),
        ("INC-0055", body_add("Later, `ndots:2` everywhere."), "exists only from"),
        ("INC-0055", body_add("Edge limit raised to 250 per pod."), "exists only from 2026-03-10"),
        (
            "INC-0055",
            body_add("auth-service has a dual-read release."),
            "exists only from 2026-07-22",
        ),
        ("INC-0101", body_add("journald was already capped by SystemMaxUse."), "never true"),
        ("INC-0055", body_add("PgBouncer listens on 6432."), "demo guard"),
        ("INC-0055", body_add("We checked again at 03:15."), "outside start-2h"),
        ("INC-0055", body_add("- 13:00 · late entry"), "timeline goes backwards"),
        ("INC-0055", body_replace("14:05", "about two"), "14:05 (start/end) not stated"),
    ],
)
def test_validator_rejects_broken_documents(docs, rows, services_doc, rb_ids, inc, mutate, message):
    d = copy.deepcopy(docs[inc])
    mutate(d)
    with pytest.raises(CorpusError, match=re.escape(message)):
        validate_documents([d], rows, services_doc, rb_ids)


def test_validator_rejects_bad_front_matter(docs, rows, services_doc, rb_ids):
    d = copy.deepcopy(docs["INC-0048"])
    d["meta"]["format"] = "essay"
    with pytest.raises(CorpusError, match="format"):
        validate_documents([d], rows, services_doc, rb_ids)
    del d["meta"]["root_cause"]
    with pytest.raises(CorpusError, match="root_cause"):
        validate_documents([d], rows, services_doc, rb_ids)


def log_mut(field, value=None, fn=None):
    def f(evs):
        e = evs[len(evs) // 2]
        e[field] = fn(e[field]) if fn else value

    return f


def kafka_line_from_ledger_host(evs):
    next(e for e in evs if e["service"] == "kafka")["message"] = "ledger-service-01.prod.internal x"


@pytest.mark.parametrize(
    ("inc", "mutate", "message"),
    [
        ("INC-0185", log_mut("service", "payments-api"), "not involved"),
        ("INC-0185", kafka_line_from_ledger_host, "must start"),
        ("INC-0185", log_mut("message", fn=lambda m: m + " ORD-5003"), "'ORD-5003'"),
        ("INC-0185", log_mut("message", fn=lambda m: m + " 10.0.33.11:9093"), "port"),
        ("INC-0185", log_mut("message", fn=lambda m: m + " 10.0.3.11"), "involved service"),
        ("INC-0185", log_mut("ts", fn=lambda t: t + timedelta(hours=2)), "outside"),
        ("INC-0185", log_mut("incident_id", "INC-0001"), "incident_id"),
        ("INC-0055", log_mut("message", fn=lambda m: m + " :6432"), "demo guard"),
    ],
)
def test_validator_rejects_broken_logs(events, by_id, services_doc, inc, mutate, message):
    evs = copy.deepcopy(events[inc])
    mutate(evs)
    with pytest.raises(CorpusError, match=re.escape(message)):
        synthetic_logs.validate_logs(evs, by_id[inc], services_doc)


def test_validator_requires_every_row_code_in_the_logs(events, by_id, services_doc):
    evs = [e for e in events["INC-0185"] if "LEDGER-E455" not in e["message"]]
    with pytest.raises(CorpusError, match="never logged"):
        synthetic_logs.validate_logs(evs, by_id["INC-0185"], services_doc)


WEB_CONCURRENCY_BEFORE = ["INC-0013", "INC-0024", "INC-0030", "INC-0063", "INC-0080", "INC-0097"]
WEB_CONCURRENCY_AFTER = ["INC-0172", "INC-0189"]


def test_web_concurrency_timeline(by_id, docs):
    """Unset until 2026-05-29, 16 since. Earlier fraud-scorer rows may say only that it was
    unset; later ones may not say it is unset. Write-ups for these rows come in later
    batches; the rule is tested on the rows now and applies to their documents then."""
    from scripts.corpus.incidents import anachronisms

    set_claims = ["`WEB_CONCURRENCY=4`", "we set WEB_CONCURRENCY to 8", "WEB_CONCURRENCY was set"]
    for i in WEB_CONCURRENCY_BEFORE:
        assert by_id[i]["service"] == "fraud-scorer", i
        for claim in set_claims:
            assert anachronisms(claim, by_id[i]), (i, claim)
        assert not anachronisms("WEB_CONCURRENCY was not set", by_id[i]), i
        if i in docs:
            assert not anachronisms(doc_text(docs[i]), by_id[i]), i
    for i in WEB_CONCURRENCY_AFTER:
        assert by_id[i]["service"] == "fraud-scorer", i
        assert anachronisms("WEB_CONCURRENCY was not set", by_id[i]), i
        assert not anachronisms("WEB_CONCURRENCY=16", by_id[i]), i
    assert not anachronisms(doc_text(docs["INC-0163"]), by_id["INC-0163"])


def test_codes_are_catalog_tokens_only(services_doc):
    from scripts.corpus.incidents import code_finder

    find = code_finder(services_doc)
    found, unknown = find("SRCH-E502 and CPX-W429, 200 OK, 500 ms, port 6432, loading, 503.")
    assert found == {"SRCH-E502", "CPX-W429", "503"} and not unknown
    assert find("x509: certificate has expired; READONLY You can't")[0] == {
        "x509: certificate has expired",
        "READONLY",
    }


# ------------------------------------------------------------- sidecar labels


def test_absent_codes_never_in_documents_or_logs(docs, events, side):
    for i in BATCH:
        text = doc_text(docs[i]) + "\n".join(e["message"] for e in events[i])
        for c in side[i].get("absent_codes", []):
            assert c not in text, (i, c)


# Boolean facts become text that must not appear.
FORBIDDEN_BY_FACT = {
    "wal_or_disk": [r"\bWAL\b", r"\bdisk\b"],
    "retry_storm": [r"retry storm"],
    "ledger_cpu_spin": [r"\bspin"],
    "after_rb018_review": [r"RB-018 (?:records|lists|describes this|mentions this)"],
}


def test_facts_appear_in_the_write_up(docs, side):
    for i in BATCH:
        text = doc_text(docs[i])
        for key, val in side[i].get("facts", {}).items():
            if isinstance(val, bool):
                for pat in FORBIDDEN_BY_FACT[key]:
                    assert not re.search(pat, text, re.IGNORECASE), (i, key, pat)
            elif isinstance(val, int | float):
                assert has_token(text, f"{val:g}") or has_token(text, f"{val:,}"), (i, key, val)
            elif TIME_RE.search(val):
                for t in TIME_RE.findall(val):
                    assert ":".join(x for x in t if x) in text, (i, key, val)
            elif "/" in val or "=" in val:
                assert val in text, (i, key, val)


def test_discriminators_appear(docs, side):
    for i in BATCH:
        for tok in side[i].get("discriminators", []):
            assert tok in doc_text(docs[i]), (i, tok)


def test_no_sidecar_keys_or_mechanism_tags_in_write_ups(docs, side, runbooks):
    """Labels that are also real vocabulary in a runbook (e.g. `s_client`) are exempt."""
    labels = {s["mechanism"] for s in side.values()}
    labels |= {k for s in side.values() for k in s if "_" in k}
    labels |= {k for s in side.values() for k in s.get("facts", {}) if "_" in k}
    labels -= {w for r in runbooks for w in re.findall(r"[a-z0-9_]+", r["body"])}
    for i in BATCH:
        words = set(re.findall(r"[a-z0-9_]+", doc_text(docs[i])))
        assert not labels & words, (i, labels & words)


def test_no_write_up_or_log_leaks_the_demo(docs, events):
    for i in BATCH:
        assert not leaks_demo(doc_text(docs[i])), i
        assert not leaks_demo("\n".join(e["message"] for e in events[i])), i


# ----------------------------------------------------------- times and numbers


def hm(s):
    """Minutes since midnight; seconds count as a fraction of a minute."""
    h, m, *sec = s.split(":")
    return int(h) * 60 + int(m) + (int(sec[0]) / 60 if sec else 0)


DUR_RE = re.compile(r"\b(\d+)[ -](?:minutes?|min|hours?|h)\b")
DURATION_WHITELIST = {"INC-0171": {23, 168}}


def test_stated_durations_match_the_times(docs, by_id):
    """Every "N minutes" / "N h" is the total, a gap between two stated times, or whitelisted."""
    for i in BATCH:
        text, r = doc_text(docs[i]), by_id[i]
        total = int((r["resolved_at"] - r["started_at"]).total_seconds() // 60)
        clocks = {hm(m.group(0)) for m in TIME_RE.finditer(text)}
        gaps = {(b - a) % 1440 for a in clocks for b in clocks}
        allowed = {total} | gaps | DURATION_WHITELIST.get(i, set())
        for n in DUR_RE.findall(text):
            assert int(n) in allowed, (i, n)


def at(clock, minutes=0.0):
    return hm(clock) + minutes


# (incident, value computed here, token the write-up states, tolerance). Clock-time tokens
# compare as minutes since midnight; "2,400" and "28%" parse as numbers.
ARITHMETIC = [
    ("INC-0055", 4 * 40, "160", 0),
    ("INC-0055", 160 / 2, "80", 0),
    ("INC-0055", 40 + 10, "50", 0),
    ("INC-0055", 80 - 50, "30", 0),
    ("INC-0055", 2 * 50, "100", 0),
    ("INC-0055", at("14:05") - at("13:20"), "45", 0),
    ("INC-0185", at("10:12", 5), "10:17", 0),
    ("INC-0185", at("10:17", 5), "10:22", 0),
    ("INC-0185", 10 * 1024 * 1024, "10485760", 0),
    ("INC-0168", 4 * 2, "8", 0),
    ("INC-0168", 1 * 2, "2", 0),
    ("INC-0101", (date(2025, 11, 18) - date(2025, 10, 20)).days, "29", 0),
    ("INC-0101", (date(2025, 10, 31) - date(2025, 10, 20)).days, "11", 0),
    ("INC-0101", 11 + 29, "40", 0),
    ("INC-0101", 0.9 * 40, "36", 0),
    ("INC-0101", (date(2025, 11, 14) - date(2025, 10, 20)).days, "25", 0),
    ("INC-0101", 100 * 11 / 40, "28%", 0.5),
    ("INC-0099", hm("18:04:30") + 30 / 60, "18:05:00", 0),  # + 30 s down-after-milliseconds
    ("INC-0171", 2 * 20, "40", 0),
    ("INC-0171", 40 / 0.33, "120", 1.5),
    ("INC-0171", 40 / 1.6, "25", 0),
    ("INC-0171", 40 - 25, "15", 0),
    ("INC-0171", at("17:30", 20_000 / 15 / 60), "17:52", 0.5),
    ("INC-0171", 15 * 3_600, "54,000", 0),
    ("INC-0171", 54_000 / 40 / 60, "23", 0.5),
    ("INC-0171", 15 * 10_800, "162,000", 0),
    ("INC-0171", 120 - 40, "80", 0),
    ("INC-0171", (162_000 - 20_000) / 80, "1,775", 0),
    ("INC-0171", 1_775 / 60, "29", 0.6),
    ("INC-0171", at("20:30", 1_775 / 60), "20:59", 0.6),
    ("INC-0171", 162_000 / 80, "2,025", 0),
    ("INC-0171", at("20:30", 2_025 / 60), "21:03", 0.75),
    ("INC-0163", 16 * 150, "2,400", 0),
    ("INC-0163", 32 * 150, "4,800", 0),
    ("INC-0163", 3 * 1024, "3,072", 0),
    ("INC-0163", 3_072 / 150, "20", 0.5),
    ("INC-0163", at("22:11") - at("22:00"), "11", 0),
    ("INC-0067", at("10:34") - at("10:10"), "24", 0),
    ("INC-0048", 400 / 4, "100", 0),  # "about 100 … a quarter of the 400"
    ("INC-0023", (date(2025, 3, 16) - date(2025, 2, 14)).days, "30", 0),
]


def parse_token(tok):
    if ":" in tok:
        return hm(tok)
    return float(tok.rstrip("%").replace(",", ""))


@pytest.mark.parametrize(("inc", "value", "token", "tol"), ARITHMETIC)
def test_arithmetic_stated_in_the_write_up(docs, inc, value, token, tol):
    text = doc_text(docs[inc])
    assert has_token(text, token), (inc, token)
    assert abs(value - parse_token(token)) <= tol, (inc, value, token)


# ------------------------------------------------------------------ diversity


def test_formats_and_lengths(docs):
    formats = [d["meta"]["format"] for d in docs.values()]
    assert set(formats) <= DOC_FORMATS and len(set(formats)) >= 8
    assert max(formats.count(f) for f in formats) <= 2
    for i, d in docs.items():
        lo, hi = WORDS[d["meta"]["format"]]
        assert lo <= word_count(d["body"]) <= hi, (i, word_count(d["body"]))


def test_headings_never_repeat(docs, runbooks):
    rb = {headings(r["body"]) for r in runbooks}
    seqs = [headings(d["body"]) for d in docs.values()]
    assert len(seqs) == len(set(seqs))
    assert not set(seqs) & rb
    sections = [s[1:] for s in seqs if len(s) > 1]
    assert len(sections) == len(set(sections))
    titles = [d["meta"]["title"].lower() for d in docs.values()]
    assert len(set(titles)) == len(titles)
    assert not set(titles) & {r["meta"]["title"].lower() for r in runbooks}


def jaccard(a, b):
    return len(a & b) / len(a | b)


def test_write_ups_do_not_share_passages(docs, runbooks):
    grams = {i: trigrams(d["body"]) for i, d in docs.items()}
    rgrams = {r["meta"]["id"]: trigrams(r["body"]) for r in runbooks}
    scores = {(a, b): jaccard(grams[a], grams[b]) for a, b in combinations(sorted(grams), 2)}
    scores |= {(a, b): jaccard(grams[a], rgrams[b]) for a in grams for b in rgrams}
    top = sorted(scores.items(), key=lambda kv: -kv[1])[:10]
    print("top trigram Jaccard:", ", ".join(f"{a}/{b} {s:.4f}" for (a, b), s in top))
    assert top[0][1] < TRIGRAM_JACCARD_THRESHOLD, top[0]


def prose_ngrams(text, n=SHARED_RUN):
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    words = re.findall(r"[a-z0-9_]+", text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def test_no_sentence_copied_from_a_runbook_or_another_write_up(docs, runbooks):
    mine = {i: prose_ngrams(d["body"]) for i, d in docs.items()}
    others = {r["meta"]["id"]: prose_ngrams(r["body"]) for r in runbooks}
    for a, ga in mine.items():
        for b, gb in {**others, **{k: v for k, v in mine.items() if k != a}}.items():
            assert not ga & gb, (a, b, sorted(ga & gb)[:2])


# ----------------------------------------------------------------------- logs


def test_logs_are_deterministic_and_sized(events, by_id, docs, services_doc):
    for i in BATCH:
        evs = events[i]
        assert 40 <= len(evs) <= 160, (i, len(evs))
        if by_id[i]["codes"]:  # a TLS failure the server never sees logs no errors at all
            assert len({e["level"] for e in evs}) >= 2, i
        spec = synthetic_logs.load_spec(DATA / "incidents" / "logs" / f"{i}.toml")
        again = synthetic_logs.generate(i, spec, by_id[i]["started_at"].date())
        assert again == evs, i


def counters(evs, pattern):
    return [int(m.group(1)) for e in evs if (m := re.search(pattern, e["message"]))]


@pytest.mark.parametrize(
    ("inc", "pattern", "direction"),
    [
        ("INC-0185", r"queue=(\d+)", 1),
        ("INC-0185", r"failed, (\d+) events pending", 1),
        ("INC-0185", r"backing off (\d+)s", 1),
        ("INC-0101", r"backlog (\d+)% shipped", 1),
        ("INC-0171", r"lag (\d+) above", 0),  # rises to 20:30, then falls
    ],
)
def test_log_counters_never_jump_back(events, inc, pattern, direction):
    vals = counters(events[inc], pattern)
    assert len(vals) >= 4, (inc, pattern)
    if direction:
        assert vals == sorted(vals), (inc, vals)
    else:
        peak = vals.index(max(vals))
        assert vals[: peak + 1] == sorted(vals[: peak + 1]), vals
        assert vals[peak:] == sorted(vals[peak:], reverse=True), vals
        # lines are a sample, so the peak is near, not exactly at, 162,000
        assert 150_000 <= max(vals) <= 162_000 and min(vals) >= 20_000


def test_ramp_counts_both_ways_and_only_in_bursts():
    burst = {
        "service": "s",
        "level": "INFO",
        "hosts": ["h"],
        "from": "10:00:00",
        "to": "10:10:00",
        "count": 5,
        "message": "n={ramp:50-10}",
    }
    evs = synthetic_logs.generate("X", {"burst": [burst]}, date(2026, 1, 1))
    vals = [int(e["message"].split("=")[1]) for e in evs]
    assert vals == sorted(vals, reverse=True) and 10 <= min(vals) and max(vals) <= 50
    line = {
        "service": "s",
        "level": "INFO",
        "host": "h",
        "at": "10:00:00",
        "message": "n={ramp:1-2}",
    }
    with pytest.raises(ValueError, match="ramp"):
        synthetic_logs.generate("X", {"line": [line]}, date(2026, 1, 1))


def test_loghub_windows_cover_the_documented_incidents(by_id):
    windows = build_corpus.incident_windows()
    assert [w[0] for w in windows] == sorted(BATCH)
    for i, lo, hi in windows:
        assert lo == by_id[i]["started_at"] - timedelta(minutes=10)
        assert hi == by_id[i]["resolved_at"] + timedelta(minutes=10)
