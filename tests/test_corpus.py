"""Corpus build tests (Phase 1 step 3). No network: LogHub tests use a made-up fixture
with the same columns as the real file, and a fake fetch function."""

import copy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from scripts import build_corpus
from scripts.corpus import loghub
from scripts.corpus.common import DATA, PROCESSED, rng_for, sha256_bytes
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
    for token in ["ECONNREFUSED", "ETIMEDOUT", "53300", "503", "NXDOMAIN", "OOMKilled"]:
        assert codes[token] == "*"
    assert codes["PAYAPI-E1042"] == "payments-api"


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
    assert set(outputs) == {"services.json"}


def test_committed_outputs_are_up_to_date():
    """Same check as `make corpus-check`: rerun `make corpus` if a source changed."""
    outputs, counts = build_corpus.build_committed()
    expected = {**outputs, build_corpus.MANIFEST: build_corpus.manifest(outputs, counts)}
    for name, data in expected.items():
        path = PROCESSED / name
        assert path.exists(), f"{name} missing; run `make corpus`"
        assert path.read_bytes() == data, f"{name} is stale; run `make corpus`"
