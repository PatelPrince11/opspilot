"""Corpus build tests (Phase 1 step 3). No network: LogHub tests use a made-up fixture
with the same columns as the real file, and a fake fetch function."""

import copy
import re
import tomllib
from datetime import UTC, datetime, timedelta
from itertools import combinations
from pathlib import Path

import pytest

from scripts import build_corpus
from scripts.corpus import loghub
from scripts.corpus.common import DATA, PROCESSED, SIDECAR, rng_for, sha256_bytes
from scripts.corpus.runbooks import (
    headings,
    load_runbooks,
    runbooks_output,
    validate_runbooks,
    word_count,
)
from scripts.corpus.services import (
    CorpusError,
    load_services,
    services_output,
    validate_services,
)

FIXTURE = Path(__file__).parent / "fixtures" / "loghub_sample.csv"


@pytest.fixture(scope="module")
def services_doc():
    return load_services(DATA / "services.toml")


# ------------------------------------------------------------------- services


def test_exactly_24_services_13_apps_11_datastores(services_doc):
    kinds = [s["kind"] for s in services_doc["services"]]
    assert len(kinds) == 24
    assert kinds.count("app") == 13
    assert kinds.count("datastore") == 11


def test_services_toml_is_valid(services_doc):
    validate_services(services_doc)  # raises CorpusError listing every problem


def test_status_snapshot(services_doc):
    status = {s["name"]: s["status"] for s in services_doc["services"]}
    degraded = {n for n, st in status.items() if st != "healthy"}
    assert degraded == {"payments-api", "payments-pgbouncer", "search-api"}
    assert {status[n] for n in degraded} == {"degraded"}


def test_demo_path_exists_in_dependency_graph(services_doc):
    deps = {s["name"]: s["dependencies"] for s in services_doc["services"]}
    assert "payments-pgbouncer" in deps["payments-api"]
    assert deps["payments-pgbouncer"] == ["payments-db"]
    hosts = {h["name"] for s in services_doc["services"] for h in s["hosts"]}
    assert "payments-db-01.prod.internal" in hosts  # used by the step-2 keyword tests


def test_output_rows_are_marked_synthetic(services_doc):
    rows = services_output(services_doc)
    assert [r["name"] for r in rows] == sorted(r["name"] for r in rows)
    assert all(r["synthetic"] is True for r in rows)


def test_error_code_catalog_has_standard_and_internal_codes(services_doc):
    codes = {e["code"]: e["service"] for e in services_doc["error_codes"]}
    for token in [
        "ECONNREFUSED",
        "ETIMEDOUT",
        "53300",
        "57P01",
        "57P02",
        "503",
        "OOMKilled",
        "query_wait_timeout",
        "READONLY",
        "OOM command not allowed",
        "EAI_AGAIN",
    ]:
        assert codes[token] == "*"
    assert len(codes) == 38
    assert sum(s == "*" for s in codes.values()) == 21  # real-world tokens
    assert sum(s != "*" for s in codes.values()) == 17  # fictional internal codes
    assert codes["PAYAPI-E1042"] == "payments-api"
    assert codes["FRD-E140"] == "fraud-scorer"
    assert codes["CPX-W430"] == "card-processor-proxy"


def broken(doc, mutate):
    doc = copy.deepcopy(doc)
    mutate(doc)
    return doc


def svc(doc, name):
    return next(s for s in doc["services"] if s["name"] == name)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: svc(d, "payments-api")["dependencies"].append("billing-api"), "billing-api"),
        (
            lambda d: svc(d, "payments-api")["hosts"].append(
                {"name": "payments-db-09.prod.internal", "ip": "10.0.3.99"}
            ),
            "must look like payments-api-NN",
        ),
        (lambda d: svc(d, "payments-db")["dependencies"].append("payments-api"), "cycle"),
        (lambda d: svc(d, "kafka").update(status="on-fire"), "status"),
        (lambda d: svc(d, "kafka")["hosts"][1].update(ip="10.0.33.11"), "duplicate ip"),
        (lambda d: d["error_codes"].append({"code": "X1", "service": "nope", "meaning": ""}), "X1"),
    ],
)
def test_validator_rejects_broken_services(services_doc, mutate, message):
    with pytest.raises(CorpusError, match=message):
        validate_services(broken(services_doc, mutate))


# ------------------------------------------------------------------- runbooks

# Batch 1 measured a maximum pairwise word-trigram Jaccard of 0.0108 (RB-004 vs RB-005).
# The threshold leaves room for batches 2-3 and their hard-negative pairs; a pair above
# it means two runbooks share whole passages, i.e. a template crept back in.
MEASURED_MAX_TRIGRAM_JACCARD = 0.0233  # batch 2, RB-001 vs RB-013; unchanged after batch 3
TRIGRAM_JACCARD_THRESHOLD = 0.03


@pytest.fixture(scope="module")
def runbooks():
    return load_runbooks(DATA / "runbooks")


def test_runbooks_load_and_validate(runbooks, services_doc):
    validate_runbooks(runbooks, services_doc)
    assert [r["meta"]["id"] for r in runbooks] == [f"RB-{n:03d}" for n in range(1, 31)]
    rows = runbooks_output(runbooks)
    assert all(r["synthetic"] is True and r["doc_type"] == "runbook" for r in rows)


def test_runbook_failure_types_hold_one_or_two_values(runbooks):
    by_id = {r["meta"]["id"]: r["meta"]["failure_types"] for r in runbooks}
    assert by_id["RB-002"] == ["high_cpu", "api_latency"]
    assert all(1 <= len(ft) <= 2 for ft in by_id.values())


def rb(runbooks, rb_id):
    return next(r for r in runbooks if r["meta"]["id"] == rb_id)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda rs: rb(rs, "RB-001")["meta"]["services"].append("billing-api"), "billing-api"),
        (
            lambda rs: rb(rs, "RB-002").update(body="see payments-api-09.prod.internal\n"),
            "unknown host 'payments-api-09.prod.internal'",
        ),
        (
            lambda rs: rb(rs, "RB-003").update(body="logs PAYAPI-E9999 often\n"),
            "unknown internal code 'PAYAPI-E9999'",
        ),
        (lambda rs: rb(rs, "RB-004").update(file="RB-044.md"), "match the filename"),
        (lambda rs: rb(rs, "RB-005")["meta"].update(synthetic=False), "synthetic must be true"),
        (
            lambda rs: rb(rs, "RB-006")["meta"].update(
                failure_types=["memory_leak", "high_cpu", "disk_full"]
            ),
            "1 or 2 distinct",
        ),
        (lambda rs: rb(rs, "RB-007")["meta"].update(failure_types=["cosmic_rays"]), "cosmic_rays"),
        (lambda rs: rb(rs, "RB-008")["meta"].pop("owner"), "owner"),
        (lambda rs: rb(rs, "RB-009").update(body="like INC-42\n"), "malformed reference 'INC-42'"),
        (lambda rs: rb(rs, "RB-009").update(body="see RB-1\n"), "malformed reference 'RB-1'"),
        (
            lambda rs: rb(rs, "RB-009").update(body="as in INC-LIVE-001\n"),
            "malformed reference 'INC-LIVE-001'",
        ),
    ],
)
def test_validator_rejects_broken_runbooks(runbooks, services_doc, mutate, message):
    rs = copy.deepcopy(runbooks)
    mutate(rs)
    with pytest.raises(CorpusError, match=re.escape(message)):
        validate_runbooks(rs, services_doc)


def test_validator_allows_runbook_and_incident_references(runbooks, services_doc):
    rs = copy.deepcopy(runbooks)
    rb(rs, "RB-010")["body"] += "\nSee also RB-012 and past incidents INC-0042, INC-0107.\n"
    validate_runbooks(rs, services_doc)


def test_headings_ignore_shell_comments_in_code_fences():
    body = "# Title\n\n```\n# not a heading\n```\n## Step\n"
    assert headings(body) == ("title", "step")


def test_no_two_runbooks_share_a_heading_sequence(runbooks):
    seqs = [headings(r["body"]) for r in runbooks]
    nonempty = [s for s in seqs if s]
    assert len(nonempty) == len(set(nonempty))
    # Section headings beyond the title differ too: no shared template.
    sections = [s[1:] for s in nonempty if len(s) > 1]
    assert len(sections) == len(set(sections))


def test_runbook_formats_and_lengths_vary(runbooks):
    formats = {r["meta"]["format"] for r in runbooks}
    lengths = [word_count(r["body"]) for r in runbooks]
    assert len(formats) >= 5, formats
    assert min(lengths) < 300 and max(lengths) > 1000, lengths


def trigrams(text):
    words = re.findall(r"[a-z0-9_]+", text.lower())
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def test_runbooks_do_not_share_passages(runbooks):
    grams = {r["meta"]["id"]: trigrams(r["body"]) for r in runbooks}
    scores = {
        (a, b): len(grams[a] & grams[b]) / len(grams[a] | grams[b])
        for a, b in combinations(sorted(grams), 2)
    }
    worst = max(scores, key=scores.get)
    print(
        f"max trigram Jaccard {scores[worst]:.4f} ({worst[0]} vs {worst[1]}); "
        f"threshold {TRIGRAM_JACCARD_THRESHOLD} (last measured {MEASURED_MAX_TRIGRAM_JACCARD})"
    )
    assert scores[worst] < TRIGRAM_JACCARD_THRESHOLD, (worst, scores[worst])


def leaks_demo(body):
    """Only the demo's specific host + error + port combination (from demo_answer.toml)
    counts, so generic config-push runbooks (RB-027) pass."""
    with (SIDECAR / "demo_answer.toml").open("rb") as f:
        combo = tomllib.load(f)["leak_combination"]
    return all(t in body for t in combo)


def test_demo_leak_guard_is_scoped():
    assert leaks_demo("payments-pgbouncer-01 refused :6432 with ECONNREFUSED")
    assert not leaks_demo("Roll back the config push, then flip the kill switch.")
    assert not leaks_demo("ECONNREFUSED from payments-pgbouncer-02 on 6432")


def test_runbooks_do_not_leak_the_demo_combination(runbooks):
    assert not [r["meta"]["id"] for r in runbooks if leaks_demo(r["body"])]


# --------------------------------------------------------------------- LogHub


def test_parse_maps_levels_services_and_marks_noise():
    events = loghub.parse(FIXTURE.read_bytes())
    assert len(events) == 5
    assert {e["level"] for e in events} == {"INFO", "WARN"}  # WARNING -> WARN
    assert {e["service"] for e in events} == {
        "openstack-nova-api",
        "openstack-nova-compute",
        "openstack-nova-scheduler",
    }
    assert all(e["incident_id"] is None for e in events)
    assert all(e["source"] == "loghub:OpenStack" for e in events)
    assert all(e["corpus"] == "main" for e in events)
    assert events[0]["ts"] == datetime(2017, 5, 16, 0, 0, 0, 8000, tzinfo=UTC)


def test_ensure_file_downloads_and_verifies(tmp_path):
    data = b"hello"
    dest = tmp_path / "f.csv"
    assert loghub.ensure_file(dest, "u", sha256_bytes(data), fetch=lambda u: data) == data
    assert dest.read_bytes() == data


def test_ensure_file_hash_mismatch_raises_and_writes_nothing(tmp_path):
    dest = tmp_path / "f.csv"
    with pytest.raises(loghub.LogHubIntegrityError, match="sha256"):
        loghub.ensure_file(dest, "u", sha256_bytes(b"pinned"), fetch=lambda u: b"changed upstream")
    assert not dest.exists()


def test_ensure_file_reuses_valid_cache_and_replaces_corrupt_cache(tmp_path):
    data = b"pinned"
    dest = tmp_path / "f.csv"
    dest.write_bytes(data)

    def no_network(url):
        raise AssertionError("should not download when the cache is valid")

    assert loghub.ensure_file(dest, "u", sha256_bytes(data), fetch=no_network) == data

    dest.write_bytes(b"corrupt")
    assert loghub.ensure_file(dest, "u", sha256_bytes(data), fetch=lambda u: data) == data
    assert dest.read_bytes() == data


def test_pinned_urls_use_a_commit_not_a_branch():
    assert len(loghub.COMMIT) == 40
    assert loghub.COMMIT in loghub.raw_url(loghub.CSV_PATH)
    assert "/master/" not in loghub.raw_url(loghub.CSV_PATH)


def test_place_in_windows_keeps_noise_inside_windows_and_unlinked():
    events = loghub.parse(FIXTURE.read_bytes())
    start = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    windows = [
        ("INC-A", start, start + timedelta(hours=1)),
        ("INC-B", start, start + timedelta(minutes=5)),
    ]
    placed = loghub.place_in_windows(events, windows, rng_for, per_window=3)

    assert len(placed) == 6
    for (_, w_start, w_end), chunk in zip(windows, (placed[:3], placed[3:]), strict=True):
        assert all(w_start <= e["ts"] <= w_end for e in chunk)
        assert [e["ts"] for e in chunk] == sorted(e["ts"] for e in chunk)
    assert all(e["incident_id"] is None for e in placed)
    assert placed == loghub.place_in_windows(events, windows, rng_for, per_window=3)


# --------------------------------------------------------- seed and manifest


def test_rng_for_is_deterministic_and_independent_per_key():
    assert rng_for("INC-0001").random() == rng_for("INC-0001").random()
    assert rng_for("INC-0001").random() != rng_for("INC-0002").random()


def test_build_is_byte_identical_across_runs():
    first, counts1 = build_corpus.build_committed()
    second, counts2 = build_corpus.build_committed()
    assert first == second and counts1 == counts2
    assert build_corpus.manifest(first, counts1) == build_corpus.manifest(second, counts2)


def test_manifest_hashes_committed_outputs_only():
    outputs, counts = build_corpus.build_committed()
    manifest = build_corpus.manifest(outputs, counts).decode()
    assert build_corpus.LOGHUB_OUT not in manifest
    assert counts["services"] == 24
    assert counts["runbooks"] == 30
    assert set(outputs) == {"services.json", "runbooks.jsonl"}


def test_committed_outputs_are_up_to_date():
    """Same check as `make corpus-check`: rerun `make corpus` if a source changed."""
    outputs, counts = build_corpus.build_committed()
    expected = {**outputs, build_corpus.MANIFEST: build_corpus.manifest(outputs, counts)}
    for name, data in expected.items():
        path = PROCESSED / name
        assert path.exists(), f"{name} missing; run `make corpus`"
        assert path.read_bytes() == data, f"{name} is stale; run `make corpus`"
