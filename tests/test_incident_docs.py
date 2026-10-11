"""Incident write-ups and synthetic logs (Phase 1 step 3, batch 5). The build validator
(scripts/corpus/incidents.py, synthetic_logs.py) checks a write-up against its skeleton row;
these tests add the checks that need eval-only labels (eval/corpus/), arithmetic, diversity
and leakage, plus one broken input per validator rule."""

import copy
import re
import tomllib
from datetime import date, timedelta

import pytest

from scripts import build_corpus
from scripts.corpus import synthetic_logs
from scripts.corpus.checks import (
    FORBIDDEN_BY_FACT,
    TRIGRAM_JACCARD_THRESHOLD,
    copied_runs,
    duration_errors,
    fact_errors,
    has_token,
    heading_errors,
    hm,
    jaccard_scores,
    label_errors,
    leaks_demo,
    log_size_errors,
    sidecar_labels,
    word_count_errors,
)
from scripts.corpus.common import DATA, SIDECAR
from scripts.corpus.incidents import (
    BLOCKED_EXEMPT,
    DOC_FORMATS,
    doc_text,
    load_documents,
    load_skeleton,
    validate_documents,
)
from scripts.corpus.runbooks import load_runbooks
from scripts.corpus.services import CorpusError, load_services

BATCH5 = {
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
# Batch 6: planned ids. A write-up lands when its writer finishes, so the tests take the
# documents present; every present document must be from a planned batch.
BATCH6 = {
    *("INC-0001", "INC-0011", "INC-0079", "INC-0089", "INC-0109", "INC-0127", "INC-0149"),
    *("INC-0183", "INC-0094", "INC-0013", "INC-0024", "INC-0030", "INC-0063", "INC-0080"),
    *("INC-0097", "INC-0172", "INC-0189", "INC-0152", "INC-0016", "INC-0025", "INC-0178"),
    *("INC-0197", "INC-0031", "INC-0015", "INC-0194", "INC-0096"),
}
BATCH7 = {
    *("INC-0042", "INC-0084", "INC-0157", "INC-0174", "INC-0026", "INC-0124", "INC-0093"),
    *("INC-0034", "INC-0156", "INC-0004", "INC-0162", "INC-0177", "INC-0137", "INC-0007"),
    *("INC-0060", "INC-0128", "INC-0059", "INC-0151", "INC-0142", "INC-0125", "INC-0078"),
    *("INC-0087", "INC-0108", "INC-0070", "INC-0120", "INC-0107", "INC-0133"),
}
BATCH8 = {
    *("INC-0081", "INC-0033", "INC-0038", "INC-0068", "INC-0086", "INC-0061", "INC-0110"),
    *("INC-0073", "INC-0119", "INC-0103", "INC-0134", "INC-0046", "INC-0077", "INC-0091"),
    *("INC-0095", "INC-0065", "INC-0049", "INC-0074", "INC-0186", "INC-0195", "INC-0167"),
    *("INC-0028", "INC-0005", "INC-0145", "INC-0155", "INC-0123", "INC-0022"),
}
BATCH9 = {
    *("INC-0035", "INC-0182", "INC-0136", "INC-0043", "INC-0082", "INC-0126", "INC-0166"),
    *("INC-0088", "INC-0187", "INC-0146", "INC-0014", "INC-0159", "INC-0196", "INC-0179"),
    *("INC-0076", "INC-0008", "INC-0037", "INC-0165", "INC-0169", "INC-0184", "INC-0054"),
    *("INC-0069", "INC-0039", "INC-0006", "INC-0083", "INC-0112", "INC-0117"),
}
# most uses of one format
FORMAT_CAP = {
    "batch5": (BATCH5, 2),
    "batch6": (BATCH6, 4),
    "batch7": (BATCH7, 4),
    "batch8": (BATCH8, 4),
    "batch9": (BATCH9, 4),
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


# ------------------------------------------------------------------ validator


def test_batch_documents_validate(docs, rows, services_doc, rb_ids):
    assert BATCH5 <= set(docs) <= BATCH5 | BATCH6 | BATCH7 | BATCH8 | BATCH9
    validate_documents(list(docs.values()), rows, services_doc, rb_ids)


def test_no_blocked_phrase_exemptions():
    assert BLOCKED_EXEMPT == set()


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
        ("INC-0101", body_add("This VM had SystemMaxUse set long ago."), "never true"),
        ("INC-0101", body_add("SystemMaxUse was in place here."), "never true"),
        ("INC-0163", body_add("Workers came from `WEB_CONCURRENCY=8`."), "never true"),
        ("INC-0048", body_add("See RB-016."), "RB-016 did not exist before 2026-05-26"),
        # post-dated RB-021 (written about INC-0168) is a label only: naming it fails, and an
        # ordinary missing runbook in the same row still fails
        ("INC-0168", body_add("See RB-021."), "RB-021 did not exist before 2026-06-15"),
        ("INC-0168", body_replace("RB-020", "the DNS runbook"), "runbook references must equal"),
        # RB-023's samples and History are INC-0004's own night: a label only, never named
        ("INC-0004", body_add("See RB-023."), "RB-023 did not exist before 2025-01-14"),
        ("INC-0055", body_add("The processor confirmed the next morning."), "unsupported claim"),
        ("INC-0055", body_add("The ticket sat in the queue."), "unsupported claim"),
        ("INC-0055", body_add("This was written up from chat."), "unsupported claim"),
        ("INC-0055", body_add("Nothing was lost."), "unsupported claim"),
        ("INC-0055", body_add("No event was lost anywhere."), "unsupported claim"),
        ("INC-0055", body_add("The hosting provider blamed a hypervisor."), "unsupported claim"),
        ("INC-0048", body_add("The processor said all was well."), "unsupported claim"),
        ("INC-0099", body_add("Sentinel logged +switch-master."), "unverified real-world string"),
        # batch 8 review: meta-talk, outside confirmations, unverified Redis text, future history
        ("INC-0055", body_add("This document does not say."), "unsupported claim"),
        ("INC-0055", body_add("The sources do not record it."), "unsupported claim"),
        ("INC-0055", body_add("This log does not guess."), "unsupported claim"),
        ("INC-0055", body_add("I will not explain it on its behalf."), "unsupported claim"),
        ("INC-0055", body_add("The notes from the night are thin."), "unsupported claim"),
        ("INC-0048", body_add("Their status page confirmed it."), "unsupported claim"),
        ("INC-0055", body_add("Redis said the dataset is being loaded."), "unverified real-world"),
        ("INC-0055", body_add("Like the August 2025 case."), "after the write-up date"),
        ("INC-0055", body_add("The row is filed as a pool problem."), "unsupported claim"),
        ("INC-0055", body_add("The row for this incident says sev2."), "unsupported claim"),
        ("INC-0099", body_add("sentinel: failover requested."), "unverified real-world"),
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
        ("INC-0185", log_mut("message", fn=lambda m: m + " +sdown"), "unverified real-world"),
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


def test_web_concurrency_only_ever_16(by_id):
    from scripts.corpus.incidents import anachronisms

    assert anachronisms("`WEB_CONCURRENCY=12`", by_id["INC-0189"])
    assert not anachronisms("`WEB_CONCURRENCY=16`", by_id["INC-0189"])


def test_inc_0024_fix_is_the_cpu_limit(docs):
    """NOTES binding: INC-0024 (before 2026-05-29) is fixed with the CPU-limit lever."""
    if "INC-0024" in docs:
        assert re.search(r"CPU limit", doc_text(docs["INC-0024"])), "INC-0024"


def test_journal_growth_write_ups_apply_the_cap(docs, side, rows):
    """SystemMaxUse=500M is applied per VM as RB-008's fix: each journal-growth write-up says
    it was applied, and once applied the VM can't fill /var with its journal again."""
    journal = [r for r in rows if side[r["id"]]["mechanism"] == "var_journal_growth"]
    assert len(journal) == 8
    hosts = [h for r in journal for h in r["hosts"]]
    assert len(hosts) == len(set(hosts)), hosts
    for r in journal:
        if r["id"] in docs:
            assert "SystemMaxUse=500M" in doc_text(docs[r["id"]]), r["id"]


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


def test_sidecar_facts_hold(docs, events, side):
    """Facts stated, false booleans not contradicted, discriminators present, absent codes
    absent from prose and logs (scripts/corpus/checks.py: fact_errors)."""
    for i in docs:
        logtext = "\n".join(e["message"] for e in events[i])
        assert not fact_errors(doc_text(docs[i]), side[i], logtext), i


def test_no_sidecar_keys_or_mechanism_tags_in_write_ups(docs, side, runbooks):
    """Labels that are also real vocabulary in a runbook (e.g. `s_client`) are exempt."""
    labels = sidecar_labels(side, runbooks)
    for i in docs:
        assert not label_errors(doc_text(docs[i]), labels), i


def test_no_write_up_or_log_leaks_the_demo(docs, events):
    for i in docs:
        assert not leaks_demo(doc_text(docs[i])), i
        assert not leaks_demo("\n".join(e["message"] for e in events[i])), i


# ----------------------------------------------------------- times and numbers


def test_stated_durations_match_the_times(docs, by_id):
    """Every "N minutes" / "N h" is the total, a gap between two stated times, or whitelisted."""
    for i in docs:
        assert not duration_errors(docs[i], by_id[i]), i


def at(clock, minutes=0.0):
    return hm(clock) + minutes


@pytest.mark.parametrize(
    ("key", "contradiction"),
    [
        ("last_retry_spin", "The retry workers spun again in December."),
        ("data_kafka_involved", "/data/kafka was full as well."),
        ("payments_inventory_extra_db_reads", "payments-api made extra DB reads."),
        ("intermittent", "Every lookup failed for 40 minutes."),
    ],
)
def test_batch7_boolean_facts_reject_contradictions(key, contradiction):
    val = key in ("last_retry_spin", "intermittent")
    assert fact_errors(contradiction, {"facts": {key: val}}), key
    assert not fact_errors("A neutral sentence.", {"facts": {key: val}}), key


@pytest.mark.parametrize(
    "contradiction",
    ["The edge rate limiter rejected the crawler.", "Then the local limit kicked in at 12:40."],
)
def test_batch8_limiter_fact_rejects_contradictions(contradiction):
    assert fact_errors(contradiction, {"facts": {"limiter": False}})
    assert not fact_errors("search-cluster-02 rejected searches.", {"facts": {"limiter": False}})


@pytest.mark.parametrize(
    "contradiction",
    ["The release went out on time at 20:40.", "The deploy was not delayed by the migration."],
)
def test_batch9_release_delayed_fact_rejects_contradictions(contradiction):
    assert fact_errors(contradiction, {"facts": {"release_delayed": True}})
    assert not fact_errors("The migration retried twice.", {"facts": {"release_delayed": True}})


def test_every_written_boolean_fact_has_a_pattern(side):
    for i in BATCH5 | BATCH6 | BATCH7 | BATCH8 | BATCH9:
        for k, v in side[i].get("facts", {}).items():
            assert not isinstance(v, bool) or k in FORBIDDEN_BY_FACT, (i, k)


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
    # batch 6
    ("INC-0197", 240 - 180, "60", 0),
    ("INC-0197", 2 * 180, "360", 0),
    ("INC-0197", at("20:23") - at("19:41"), "42", 0),
    ("INC-0031", (date(2025, 4, 8) - date(2025, 3, 9)).days, "30", 0),
    ("INC-0025", at("15:50") - at("15:15"), "35", 0),
    ("INC-0096", at("09:15") - at("09:05"), "10", 0),
    ("INC-0015", at("10:45") - at("10:05"), "40", 0),
    ("INC-0001", at("23:15") - at("21:25"), "110", 0),
    ("INC-0089", at("13:40") - at("11:55"), "105", 0),
    ("INC-0089", at("12:35") - at("11:55"), "40", 0),
    ("INC-0149", at("24:05") - at("22:55"), "70", 0),
    ("INC-0097", 16 * 150, "2,400", 0),
    ("INC-0097", 3_072 - 2_400, "672", 0),
    ("INC-0013", at("10:50") - at("08:50"), "120", 0),
    ("INC-0080", at("05:20") - at("05:02"), "18", 0),
    # batch 7
    ("INC-0042", 60 + 5, "65", 0),
    ("INC-0042", 2 * (10 + 5), "30", 0),
    ("INC-0042", 30 + 1 + 1, "32", 0),
    ("INC-0042", 100 - 3, "97", 0),
    ("INC-0124", 20 - 4, "16", 0),
    ("INC-0137", 420 - 300, "120", 0),
    ("INC-0137", 3 * 100, "300", 0),
    ("INC-0137", 100 * 120 / 420, "29", 0.5),
    ("INC-0137", 3 * 250, "750", 0),
    ("INC-0137", at("09:50") - at("09:05"), "45", 0),
    ("INC-0137", at("09:50") - at("09:42"), "8", 0),
    ("INC-0128", at("15:20") - at("09:00"), "380", 0),
    ("INC-0128", 380 / 50, "7.6", 0),
    ("INC-0059", at("15:30") - at("10:40"), "290", 0),
    ("INC-0059", 2 * 20, "40", 0),
    ("INC-0151", 100 - 3, "97", 0),
    ("INC-0151", 2 * 20, "40", 0),
    ("INC-0151", 2 * 80, "160", 0),
    ("INC-0151", 160 - 97, "63", 0),
    ("INC-0162", 3 * 3, "9", 0),
    ("INC-0177", 2 * 180, "360", 0),
    ("INC-0078", 32 * 0.8 + 0, "25.6", 0),
    ("INC-0078", 14 + 14, "28", 0),
    ("INC-0120", 2 * (168 / 24), "14", 0),
    ("INC-0120", 24 * 3_600 * 1_000, "86400000", 0),
    ("INC-0108", at("03:40") - at("03:12"), "28", 0),
    ("INC-0108", 40 - 30, "10", 0),
    ("INC-0108", hm("03:12") + 30 / 60, "03:12:30", 0),
    ("INC-0108", hm("03:12:30") + 10 / 60, "03:12:40", 0),
    # batch 8
    ("INC-0081", at("21:40") - at("20:25"), "75", 0),
    ("INC-0033", at("01:40") - at("01:15"), "25", 0),
    ("INC-0038", at("07:45") - at("06:30"), "75", 0),
    ("INC-0068", at("06:55") - at("04:10"), "165", 0),
    ("INC-0086", at("19:25") - at("18:20"), "65", 0),
    ("INC-0061", at("21:25") - at("17:20"), "245", 0),
    ("INC-0110", at("22:47") - at("21:37"), "70", 0),
    ("INC-0073", at("16:40") - at("16:05"), "35", 0),
    ("INC-0119", at("22:25") - at("21:25"), "60", 0),
    ("INC-0103", 3 * 25, "75", 0),
    ("INC-0103", 300 - 3, "297", 0),
    ("INC-0103", 222 + 75, "297", 0),
    ("INC-0103", at("22:45") - at("22:15"), "30", 0),
    ("INC-0134", 2 * 25, "50", 0),
    ("INC-0046", 2 * 30, "60", 0),
    ("INC-0073", 2 * 30, "60", 0),
    ("INC-0046", at("21:52") - at("21:32"), "20", 0),
    ("INC-0077", 2 * 20, "40", 0),
    ("INC-0077", 200 - 3, "197", 0),
    ("INC-0077", at("11:35") - at("10:05"), "90", 0),
    ("INC-0091", 2 * (10 + 5), "30", 0),
    ("INC-0091", at("22:10") - at("21:30"), "40", 0),
    ("INC-0091", 30 - 15, "15", 0),
    ("INC-0095", at("11:40") - at("11:05"), "35", 0),
    ("INC-0065", at("09:45") - at("06:50"), "175", 0),
    ("INC-0049", 2 * 20, "40", 0),
    ("INC-0049", 100 - 3, "97", 0),
    ("INC-0074", at("04:00") - at("03:35"), "25", 0),
    ("INC-0186", 2 * 180, "360", 0),
    ("INC-0186", at("22:07") - at("21:52"), "15", 0),
    ("INC-0167", 3 * 250, "750", 0),
    ("INC-0167", at("11:20") - at("10:55"), "25", 0),
    ("INC-0028", at("13:00") - at("12:30"), "30", 0),
    ("INC-0005", at("23:40") - at("22:30"), "70", 0),
    ("INC-0155", at("20:45") - at("19:25"), "80", 0),
    ("INC-0123", at("02:00") - at("01:50"), "10", 0),
    ("INC-0022", at("18:35") - at("18:15"), "20", 0),
    # batch 9
    ("INC-0035", 4 * 40, "160", 0),
    ("INC-0035", 2 * 40, "80", 0),
    ("INC-0035", at("02:20") - at("01:55"), "25", 0),
    ("INC-0182", 2 * 25, "50", 0),
    ("INC-0182", 3 * 25, "75", 0),
    ("INC-0083", 4 * 40, "160", 0),
    ("INC-0083", 2 * 40, "80", 0),
    ("INC-0182", at("10:40") - at("10:15"), "25", 0),
    ("INC-0136", 2 * 20, "40", 0),
    ("INC-0136", 0.9 * 20, "18", 0),
    ("INC-0088", at("04:25") - at("03:50"), "35", 0),
    ("INC-0166", at("13:30") - at("13:05"), "25", 0),
    ("INC-0112", at("21:42") - at("20:37"), "65", 0),
    ("INC-0039", at("21:25") - at("12:35"), "530", 0),
    ("INC-0043", at("16:32") - at("16:07"), "25", 0),
    ("INC-0082", at("15:05") - at("10:45"), "260", 0),
    ("INC-0126", at("09:55") - at("09:00"), "55", 0),
    ("INC-0076", at("11:35") - at("11:20"), "15", 0),
    ("INC-0196", at("22:40") - at("17:55"), "285", 0),
    ("INC-0179", at("06:35") - at("03:00"), "215", 0),
    ("INC-0117", at("12:25") - at("11:45"), "40", 0),
    ("INC-0054", at("02:40") - at("02:25"), "15", 0),
    ("INC-0008", at("19:15") - at("18:10"), "65", 0),
    ("INC-0146", at("11:45") - at("11:05"), "40", 0),
    ("INC-0014", at("07:45") - at("07:00"), "45", 0),
    ("INC-0187", at("10:32") - at("09:47"), "45", 0),
    ("INC-0169", at("13:20") - at("11:35"), "105", 0),
    ("INC-0169", 32 - 24, "8", 0),
    ("INC-0184", at("06:05") - at("04:10"), "115", 0),
    ("INC-0037", at("23:00") - at("21:55"), "65", 0),
    ("INC-0165", at("24:45") - at("23:35"), "70", 0),
    ("INC-0006", at("24:40") - at("23:40"), "60", 0),
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
    for batch, (ids, cap) in FORMAT_CAP.items():
        mine = [docs[i]["meta"]["format"] for i in ids if i in docs]
        assert not mine or max(mine.count(f) for f in mine) <= cap, batch
    for i, d in docs.items():
        assert not word_count_errors(d), i


def test_headings_never_repeat(docs, runbooks):
    for i, d in docs.items():
        others = {k: v for k, v in docs.items() if k != i}
        assert not heading_errors(d, others, runbooks), i


def test_write_ups_do_not_share_passages(docs, runbooks):
    texts = {r["meta"]["id"]: r["body"] for r in runbooks}
    scores = []
    for i, d in docs.items():
        others = {**texts, **{k: v["body"] for k, v in docs.items() if k > i}}
        scores += [(j, f"{i}/{n}") for j, n in jaccard_scores(d["body"], others)]
    top = sorted(scores, reverse=True)[:10]
    print("top trigram Jaccard:", ", ".join(f"{p} {j:.4f}" for j, p in top))
    assert top[0][0] < TRIGRAM_JACCARD_THRESHOLD, top[0]


def test_no_sentence_copied_from_a_runbook_or_another_write_up(docs, runbooks):
    texts = {r["meta"]["id"]: r["body"] for r in runbooks}
    for i, d in docs.items():
        others = {**texts, **{k: v["body"] for k, v in docs.items() if k != i}}
        assert not copied_runs(d["body"], others), i


# ----------------------------------------------------------------------- logs


def test_logs_are_deterministic_and_sized(events, by_id, docs, services_doc):
    for i in docs:
        evs = events[i]
        # a TLS failure the server never sees logs no errors at all, so one level is fine there
        assert not log_size_errors(evs, by_id[i]), i
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
        # batch 6
        ("INC-0025", r"waiting_clients=(\d+)", 1),
        ("INC-0031", r"open_conns=(\d+)", -1),
        ("INC-0001", r"/var usage (\d+)%", 1),
        ("INC-0001", r"holding (\d+) unsent", 1),
        ("INC-0001", r"backlog (\d+)% shipped", 1),
        ("INC-0011", r"/var usage (\d+)% \(alert", 1),
        ("INC-0079", r"/var usage (\d+)% \(alert", 1),
        ("INC-0089", r"\(/var\) usage (\d+)%", 1),
        ("INC-0109", r"lines (\d+)% shipped", 1),
        ("INC-0127", r"/var usage (\d+)%", 1),
        ("INC-0149", r"/var usage (\d+)% \(alert", 1),
        ("INC-0183", r"/var usage (\d+)% \(ticket", 1),
        ("INC-0013", r"working_set_mib=(\d+)", 1),
        ("INC-0063", r"feature_entries=(\d+)", 1),
        ("INC-0080", r"process memory rss_mib=(\d+)", 1),
        ("INC-0080", r"since restart rss_mib=(\d+)", 1),
        ("INC-0097", r"mem check rss_mib=(\d+)", 1),
        ("INC-0097", r"after restart rss_mib=(\d+)", 1),
        ("INC-0152", r"stale for (\d+) min", 1),
    ],
)
def test_log_counters_never_jump_back(events, inc, pattern, direction):
    vals = counters(events[inc], pattern)
    assert len(vals) >= 4, (inc, pattern)
    if direction:
        assert vals == sorted(vals, reverse=direction < 0), (inc, vals)
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


def test_loghub_windows_cover_the_documented_incidents(by_id, docs):
    windows = build_corpus.incident_windows()
    assert [w[0] for w in windows] == sorted(docs)
    for i, lo, hi in windows:
        assert lo == by_id[i]["started_at"] - timedelta(minutes=10)
        assert hi == by_id[i]["resolved_at"] + timedelta(minutes=10)
