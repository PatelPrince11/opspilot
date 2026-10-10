"""Incident skeleton tests (Phase 1 step 3, batch 4). The skeleton is rows only; these tests
pin the past events the runbooks describe, the dated skeleton rules in NOTES, the
distribution bounds and the demo guard. Eval-only labels come from eval/corpus/."""

import copy
import tomllib
from collections import Counter
from datetime import UTC, datetime

import pytest

from scripts.corpus.common import DATA, SIDECAR
from scripts.corpus.incidents import NARRATIVE, load_skeleton, postdated, validate_skeleton
from scripts.corpus.runbooks import load_runbooks, runbooks_output
from scripts.corpus.services import CorpusError, load_services


@pytest.fixture(scope="module")
def services_doc():
    return load_services(DATA / "services.toml")


@pytest.fixture(scope="module")
def runbooks():
    return {r["id"]: r for r in runbooks_output(load_runbooks(DATA / "runbooks"))}


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
def demo():
    with (SIDECAR / "demo_answer.toml").open("rb") as f:
        return tomllib.load(f)


def d(s):
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def mech(side, mechanism):
    return sorted(i for i, s in side.items() if s["mechanism"] == mechanism)


def short(hosts):
    return [h.split(".")[0] for h in hosts]


# ------------------------------------------------------------------ schema


def test_skeleton_validates(rows, services_doc, runbooks):
    validate_skeleton(rows, services_doc, set(runbooks))
    assert len(rows) == 200


def row(rs, i):
    return next(r for r in rs if r["id"] == f"INC-{i:04d}")


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda rs: row(rs, 1)["hosts"].append("payments-api-09.prod.internal"), "unknown host"),
        (lambda rs: row(rs, 1)["codes"].append("CPX-W430"), "belongs to card-processor-proxy"),
        (lambda rs: rs.__setitem__(slice(0, 2), [rs[1], rs[0]]), "INC-0001..INC-0200"),
        (lambda rs: row(rs, 3).update(severity="sev0"), "severity"),
        (
            lambda rs: row(rs, 3).update(resolved_at=row(rs, 3)["started_at"].replace(year=2024)),
            "resolved_at",
        ),
        (lambda rs: row(rs, 3)["runbook_ids"].append("RB-031"), "runbook_ids"),
    ],
)
def test_validator_rejects_broken_skeleton(rows, services_doc, runbooks, mutate, message):
    rs = copy.deepcopy(rows)
    mutate(rs)
    with pytest.raises(CorpusError, match=message):
        validate_skeleton(rs, services_doc, set(runbooks))


def test_sidecar_covers_every_row_and_nothing_else(rows, side):
    assert sorted(side) == [r["id"] for r in rows]
    assert all(s.get("mechanism") for s in side.values())


def test_eval_labels_stay_out_of_data(rows):
    eval_only = {"constraint", "mechanism", "anchor_tokens", "multi_doc", "absent_codes", "facts"}
    assert not eval_only & {k for r in rows for k in r}


# ------------------------------------------------- constraint rows (NOTES table)

# id: (constraint, service, symptoms, start, end, types, hosts, codes, runbooks, severity)
# Copied from the approved batch 4 outline; each row is a past event a runbook describes.
CONSTRAINTS = {
    "INC-0004": (
        "RB-023/2025-01-14",
        "card-processor-proxy",
        ["payments-api"],
        "2025-01-14T00:00",
        "2025-01-14T02:40",
        ["tls_expired"],
        ["card-processor-proxy-01", "card-processor-proxy-02"],
        ["CPX-E600", "PAYAPI-E1107"],
        ["RB-023"],
        "sev1",
    ),  # noqa: E501
    "INC-0016": (
        "RB-022/2025-02-20",
        "internal-dns",
        ["auth-service", "catalog-api", "orders-api"],
        "2025-02-20T19:50",
        "2025-02-20T20:15",
        ["dns_failure"],
        [
            "auth-service-01",
            "auth-service-02",
            "catalog-api-01",
            "catalog-api-02",
            "orders-api-01",
            "orders-api-02",
            "orders-api-03",
        ],
        ["EAI_AGAIN"],
        ["RB-022", "RB-020"],
        "sev2",
    ),  # noqa: E501
    "INC-0024": (
        "RB-002/2025-03",
        "fraud-scorer",
        ["payments-api"],
        "2025-03-18T15:10",
        "2025-03-18T16:05",
        ["high_cpu", "api_latency"],
        ["fraud-scorer-01", "payments-api-03"],
        ["PAYAPI-W0310"],
        ["RB-002"],
        "sev3",
    ),  # noqa: E501
    "INC-0031": (
        "RB-024/2025-04-08",
        "auth-db",
        ["auth-service"],
        "2025-04-08T09:00",
        "2025-04-08T09:53",
        ["tls_expired"],
        ["auth-db-01", "auth-db-02", "auth-service-01", "auth-service-02"],
        ["x509: certificate has expired", "503"],
        ["RB-024"],
        "sev2",
    ),  # noqa: E501
    "INC-0042": (
        "RB-028/2025-05-15",
        "catalog-api",
        ["catalog-db", "edge-gateway"],
        "2025-05-15T14:02",
        "2025-05-15T14:09",
        ["bad_deploy", "api_latency"],
        ["catalog-api-01", "catalog-api-02", "catalog-db-01"],
        ["504"],
        ["RB-028", "RB-012"],
        "sev3",
    ),  # noqa: E501
    "INC-0055": (
        "RB-011/2025-06",
        "payments-api",
        ["payments-pgbouncer"],
        "2025-06-24T13:20",
        "2025-06-24T14:05",
        ["conn_pool_exhaustion", "api_latency"],
        ["payments-pgbouncer-01", "payments-pgbouncer-02"],
        ["PAYAPI-E1042", "503"],
        ["RB-011", "RB-010"],
        "sev2",
    ),  # noqa: E501
    "INC-0059": (
        "RB-015/2025-07",
        "inventory-service",
        ["inventory-db", "orders-api"],
        "2025-07-09T10:40",
        "2025-07-09T15:30",
        ["bad_deploy", "api_latency"],
        [
            "inventory-db-01",
            "inventory-service-01",
            "inventory-service-02",
            "orders-api-01",
            "orders-api-02",
            "orders-api-03",
        ],
        ["INV-E220", "ORD-5003"],
        ["RB-015"],
        "sev3",
    ),  # noqa: E501
    "INC-0063": (
        "RB-006/2025-01..09",
        "fraud-scorer",
        ["payments-api", "redis-features"],
        "2025-07-22T08:15",
        "2025-07-22T11:00",
        ["memory_leak"],
        ["fraud-scorer-02", "redis-features-01"],
        ["FRD-W118", "PAYAPI-W0310"],
        ["RB-006"],
        "sev3",
    ),  # noqa: E501
    "INC-0070": (
        "RB-008/2025-08",
        "kafka",
        [],
        "2025-08-12T02:30",
        "2025-08-12T03:45",
        ["disk_full"],
        ["kafka-02"],
        ["ENOSPC"],
        ["RB-008"],
        "sev3",
    ),  # noqa: E501
    "INC-0078": (
        "RB-019/2025-09",
        "redis-features",
        ["fraud-scorer"],
        "2025-09-09T11:20",
        "2025-09-09T13:05",
        ["redis_down"],
        ["fraud-scorer-01", "fraud-scorer-02", "redis-features-01"],
        ["MISCONF", "FRD-E140"],
        ["RB-019"],
        "sev3",
    ),  # noqa: E501
    "INC-0084": (
        "RB-007/WAL-1",
        "payments-db",
        [],
        "2025-09-25T04:10",
        "2025-09-25T07:30",
        ["disk_full"],
        ["payments-db-01"],
        [],
        ["RB-007"],
        "sev2",
    ),  # noqa: E501
    "INC-0087": (
        "RB-018/2025-10",
        "redis-sessions",
        ["auth-service"],
        "2025-10-07T06:10",
        "2025-10-07T06:20",
        ["redis_down"],
        ["auth-service-01", "redis-sessions-01", "redis-sessions-02"],
        ["READONLY", "AUTH-E401X"],
        ["RB-018"],
        "sev4",
    ),  # noqa: E501
    "INC-0093": (
        "RB-001/2025-10",
        "ledger-service",
        [],
        "2025-10-23T16:40",
        "2025-10-23T17:55",
        ["high_cpu"],
        ["ledger-service-01", "ledger-service-02"],
        ["LEDGER-E455"],
        ["RB-001"],
        "sev2",
    ),  # noqa: E501
    "INC-0101": (
        "RB-008/2025-11",
        "search-cluster",
        [],
        "2025-11-18T05:30",
        "2025-11-18T06:40",
        ["disk_full"],
        ["search-cluster-02"],
        ["ENOSPC"],
        ["RB-008"],
        "sev3",
    ),  # noqa: E501
    "INC-0108": (
        "RB-017/2025-12",
        "redis-cache",
        ["catalog-api", "catalog-db", "payments-api", "inventory-service"],
        "2025-12-09T03:12",
        "2025-12-09T03:40",
        ["redis_down"],
        ["catalog-db-01", "redis-cache-01", "redis-cache-02"],
        ["ETIMEDOUT", "CAT-E301"],
        ["RB-017"],
        "sev2",
    ),  # noqa: E501
    "INC-0120": (
        "RB-009/2025-04..2026-04",
        "kafka",
        [],
        "2026-01-19T21:00",
        "2026-01-19T23:30",
        ["disk_full"],
        ["kafka-03"],
        [],
        ["RB-009"],
        "sev3",
    ),  # noqa: E501
    "INC-0124": (
        "RB-013/2026-01-31",
        "ledger-service",
        ["payments-api", "ledger-db"],
        "2026-01-31T23:35",
        "2026-02-01T03:10",
        ["conn_pool_exhaustion"],
        ["ledger-db-01", "ledger-service-02"],
        ["LEDGER-W210", "503"],
        ["RB-013", "RB-001"],
        "sev2",
    ),  # noqa: E501
    "INC-0128": (
        "RB-005/2026-02-catalog",
        "catalog-api",
        [],
        "2026-02-10T09:00",
        "2026-02-10T15:20",
        ["memory_leak"],
        ["catalog-api-02"],
        ["OOMKilled"],
        ["RB-005"],
        "sev3",
    ),  # noqa: E501
    "INC-0133": (
        "RB-005/2026-02-notification",
        "notification-worker",
        [],
        "2026-02-23T07:00",
        "2026-02-23T08:10",
        ["memory_leak"],
        ["notification-worker-01"],
        ["OOMKilled"],
        ["RB-005"],
        "sev4",
    ),  # noqa: E501
    "INC-0137": (
        "RB-030/2026-03-10",
        "edge-gateway",
        [],
        "2026-03-10T09:05",
        "2026-03-10T09:50",
        ["rate_limiting"],
        ["edge-gateway-01", "edge-gateway-02", "edge-gateway-03"],
        ["429", "EDGE-RL01"],
        ["RB-030", "RB-014"],
        "sev3",
    ),  # noqa: E501
    "INC-0142": (
        "RB-020/2026-03",
        "internal-dns",
        ["inventory-service"],
        "2026-03-24T13:30",
        "2026-03-24T14:15",
        ["dns_failure", "bad_deploy"],
        ["internal-dns-01", "internal-dns-02", "inventory-service-01", "inventory-service-02"],
        ["NXDOMAIN"],
        ["RB-020"],
        "sev3",
    ),  # noqa: E501
    "INC-0151": (
        "RB-012/2026-04",
        "inventory-service",
        ["inventory-db"],
        "2026-04-21T10:05",
        "2026-04-21T10:40",
        ["bad_deploy", "conn_pool_exhaustion"],
        ["inventory-db-01", "inventory-service-01", "inventory-service-02"],
        ["53300"],
        ["RB-012"],
        "sev2",
    ),  # noqa: E501
    "INC-0157": (
        "RB-007/WAL-2",
        "payments-db",
        ["payments-api"],
        "2026-05-12T02:20",
        "2026-05-12T05:10",
        ["disk_full"],
        ["payments-db-01"],
        ["ENOSPC", "57P02"],
        ["RB-007"],
        "sev2",
    ),  # noqa: E501
    "INC-0162": (
        "RB-016/2026-05",
        "card-processor-proxy",
        ["payments-api", "orders-api", "edge-gateway"],
        "2026-05-26T17:05",
        "2026-05-26T17:50",
        ["rate_limiting", "api_latency"],
        ["card-processor-proxy-01", "card-processor-proxy-02"],
        ["CPX-W429", "PAYAPI-E1107", "ORD-5003", "504"],
        ["RB-016", "RB-014"],
        "sev1",
    ),  # noqa: E501
    "INC-0168": (
        "RB-021/2026-06",
        "orders-api",
        ["orders-db"],
        "2026-06-15T19:30",
        "2026-06-18T16:00",
        ["dns_failure", "api_latency"],
        ["orders-api-01", "orders-api-02", "orders-api-03", "orders-db-01"],
        ["EAI_AGAIN"],
        ["RB-021", "RB-020"],
        "sev3",
    ),  # noqa: E501
    "INC-0174": (
        "RB-007/WAL-3",
        "payments-db",
        [],
        "2026-07-07T09:00",
        "2026-07-07T12:40",
        ["disk_full"],
        ["payments-db-01", "payments-db-02"],
        [],
        ["RB-007"],
        "sev2",
    ),  # noqa: E501
    "INC-0178": (
        "RB-026/2026-07-21",
        "auth-service",
        [],
        "2026-07-21T14:05",
        "2026-07-21T14:30",
        ["bad_deploy"],
        ["auth-service-01", "auth-service-02"],
        ["AUTH-E401X"],
        ["RB-026", "RB-018"],
        "sev2",
    ),  # noqa: E501
    "INC-0185": (
        "RB-027/2026-08-13",
        "kafka",
        ["ledger-service", "orders-api"],
        "2026-08-13T10:12",
        "2026-08-13T10:36",
        ["bad_deploy"],
        ["kafka-01", "kafka-02"],
        ["LEDGER-E455"],
        ["RB-027"],
        "sev2",
    ),  # noqa: E501
    "INC-0197": (
        "RB-029/2026-09-16",
        "card-processor-proxy",
        ["payments-api"],
        "2026-09-16T19:41",
        "2026-09-16T20:23",
        ["rate_limiting"],
        ["card-processor-proxy-01", "card-processor-proxy-02"],
        ["CPX-W430", "PAYAPI-E1107"],
        ["RB-029", "RB-016"],
        "sev1",
    ),  # noqa: E501
}


def test_constraint_rows_match_exactly(by_id, side):
    assert {i for i, s in side.items() if "constraint" in s} == set(CONSTRAINTS)
    for i, (key, svc, sym, start, end, types, hosts, codes, rbs, sev) in CONSTRAINTS.items():
        r = by_id[i]
        assert side[i]["constraint"] == key, i
        assert (r["service"], r["symptom_services"], r["failure_types"]) == (svc, sym, types), i
        assert (r["started_at"], r["resolved_at"]) == (d(start), d(end)), i
        assert (short(r["hosts"]), r["codes"], r["runbook_ids"]) == (hosts, codes, rbs), i
        assert r["severity"] == sev, i


def test_constraint_rows_never_carry_their_absent_codes(by_id, side):
    for i, s in side.items():
        assert not set(s.get("absent_codes", [])) & set(by_id[i]["codes"]), i


def test_history_predates_the_runbook_review(by_id, side, runbooks):
    for i, s in side.items():
        if "constraint" in s:
            rb = s["constraint"].split("/")[0]
            assert by_id[i]["started_at"].date().isoformat() < runbooks[rb]["last_reviewed"], i


def test_no_filler_duplicates_a_constraint_row(rows, side):
    def key(r):
        return (
            r["service"],
            tuple(r["failure_types"]),
            f"{r['started_at']:%Y-%m}",
            frozenset(r["codes"]),
        )

    taken = {key(r) for r in rows if "constraint" in side[r["id"]]}
    dupes = [r["id"] for r in rows if "constraint" not in side[r["id"]] and key(r) in taken]
    assert not dupes


# ---------------------------------------------------------- skeleton rules


def test_r1_no_ledger_retry_spin_after_release_4_12(rows, side):
    for r in rows:
        spin = side[r["id"]]["mechanism"] == "ledger_retry_spin" or (
            r["service"] == "ledger-service"
            and r["failure_types"][0] == "high_cpu"
            and "LEDGER-E455" in r["codes"]
        )
        if spin:
            assert r["started_at"] < d("2025-11-12T00:00"), r["id"]


def test_r2_payments_db_disk_is_always_wal_slot_or_archive(rows, side):
    disk = [
        r for r in rows if r["service"] == "payments-db" and r["failure_types"][0] == "disk_full"
    ]
    mechs = {side[r["id"]]["mechanism"] for r in disk}
    assert mechs == {"wal_inactive_slot", "wal_archive_failure"}
    for r in disk:
        assert d("2025-01-01T00:00") <= r["started_at"] < d("2026-09-01T00:00"), r["id"]
    print("payments-db disk_full:", [(r["id"], side[r["id"]]["mechanism"]) for r in disk])


def test_r3_no_orders_api_search_suffix_latency_after_ndots_fix(rows):
    for r in rows:
        if r["service"] == "orders-api" and r["failure_types"] == ["dns_failure", "api_latency"]:
            assert r["started_at"] < d("2026-06-18T00:00"), r["id"]


def test_r4_no_cpx_w430_before_the_local_limiter(rows):
    assert all(r["started_at"] >= d("2026-07-09T00:00") for r in rows if "CPX-W430" in r["codes"])


ENROLLED = {
    "kafka",
    "search-cluster",
    "redis-cache",
    "redis-sessions",
    "redis-features",
    "internal-dns",
    "payments-db",
    "auth-db",
    "ledger-db",
    "orders-db",
    "inventory-db",
    "catalog-db",
}


def test_r5_gate_stops_pushes_on_enrolled_units_after_2026_08_20(rows, side):
    for i in mech(side, "vm_config_push"):
        r = next(x for x in rows if x["id"] == i)
        assert "failed_hosts" in side[i]["facts"], i
        if r["started_at"] >= d("2026-08-20T00:00") and r["service"] in ENROLLED:
            assert side[i]["facts"]["failed_hosts"] <= 1, i


def first(by_id, side, mechanism):
    return [by_id[i]["started_at"] for i in mech(side, mechanism)]


def test_r6_no_long_migration_lock_queue_after_lock_timeout(by_id, side):
    assert mech(side, "migration_lock_queue") == ["INC-0042"]
    assert all(t < d("2025-05-16T00:00") for t in first(by_id, side, "migration_lock_queue"))


def test_r7_no_unloaded_auth_db_cert_after_reload_fix(by_id, side):
    assert all(t < d("2025-04-09T00:00") for t in first(by_id, side, "cert_not_reloaded"))


def test_r8_no_cpx_client_cert_expiry_after_alerts(by_id, side):
    assert all(t < d("2025-01-15T00:00") for t in first(by_id, side, "client_cert_expired"))


def test_r9_cluster_dns_capacity_only_the_2025_02_20_incident(side):
    assert mech(side, "cluster_dns_capacity") == ["INC-0016"]


def test_r10_r14_edge_rl01_only_from_2026_03_10_and_at_250_after(rows, side):
    for r in rows:
        if "EDGE-RL01" in r["codes"]:
            assert r["started_at"] >= d("2026-03-10T00:00"), r["id"]
            if r["id"] != "INC-0137":
                assert side[r["id"]]["facts"]["edge_limit_per_pod"] == 250, r["id"]


def test_r11_no_session_encoding_wave_after_dual_read(by_id, side):
    assert all(t < d("2026-07-22T00:00") for t in first(by_id, side, "session_encoding"))


def test_r12_recon_parallelism_only_the_january_2026_incident(side):
    assert mech(side, "recon_parallelism") == ["INC-0124"]


@pytest.mark.parametrize(
    ("rule", "mechanism", "keep", "lo", "hi"),
    [
        ("R15", "redis_cache_primary_loss", "INC-0108", "2025-12-09", "2026-01-12"),
        ("R16", "inventory_missing_index", "INC-0059", "2025-07-09", "2025-08-19"),
        ("R17", "sessions_planned_failover", "INC-0087", "2025-01-01", "2025-11-10"),
        ("R17", "sessions_failover", "INC-0087", "2025-01-01", "2025-11-10"),
        ("R18", "var_unrotated_log", "INC-0101", "2025-11-18", "2025-12-04"),
    ],
)
def test_r15_to_r18_last_time_windows(by_id, side, rule, mechanism, keep, lo, hi):
    """A runbook says "last time" / "last seen": no other such incident between that event
    and the runbook's review."""
    for i in mech(side, mechanism):
        if i != keep:
            t = by_id[i]["started_at"]
            assert not d(f"{lo}T00:00") <= t < d(f"{hi}T00:00"), (rule, i)


def test_no_link_to_a_runbook_written_after_the_incident(rows):
    for r in rows:
        for rb in r["runbook_ids"]:
            if rb in NARRATIVE:
                assert r["started_at"] >= d(f"{NARRATIVE[rb]}T00:00"), (r["id"], rb)


def test_rb023_is_postdated_for_inc_0004_only(by_id):
    """RB-023's samples and History are the 2025-01-14 night, so it is INC-0004's label only;
    later RB-023 rows may still name it."""
    assert postdated(by_id["INC-0004"]) == {"RB-023"}
    assert postdated(by_id["INC-0110"]) == set()
    assert postdated(by_id["INC-0177"]) == set()


def test_later_sessions_failovers_postdate_rb018_review(by_id, side, runbooks):
    """RB-018 (reviewed 2025-11-10) answers "Has this happened before?" with October 2025
    only. Later failovers are fine but must postdate the review; the prose batch must not
    have RB-018 cite them."""
    later = mech(side, "sessions_failover")
    assert later
    for i in later:
        assert by_id[i]["started_at"].date().isoformat() > runbooks["RB-018"]["last_reviewed"], i
        assert side[i]["facts"]["after_rb018_review"] is True, i


def test_r19_overcommit_fork_only_september_2025(by_id, side):
    assert mech(side, "overcommit_fork") == ["INC-0078"]
    assert f"{by_id['INC-0078']['started_at']:%Y-%m}" == "2025-09"


def test_r20_kafka_broker_disk_top_topics_in_rb009_year(by_id, side):
    window = [
        i
        for i in mech(side, "kafka_broker_disk")
        if d("2025-04-11T00:00") <= by_id[i]["started_at"] < d("2026-04-11T00:00")
    ]
    assert len(window) >= 2
    for i in window:
        assert side[i]["facts"]["top_topics"] == ["ledger.entries", "notifications.outbound"], i


def test_r21_no_cpx_w429_retry_storm_before_may_2026(rows):
    for r in rows:
        if {"CPX-W429", "ORD-5003"} <= set(r["codes"]):
            assert r["started_at"] >= d("2026-05-26T00:00"), r["id"]


def test_c3_cpx_w429_blips_are_not_retry_storms(by_id, side):
    blips = mech(side, "processor_429_blip")
    assert len(blips) == 7
    for i in blips:
        assert "ORD-5003" not in by_id[i]["codes"] and side[i]["facts"]["retry_storm"] is False


def test_c5_c6_mechanism_tags(side):
    assert side["INC-0142"]["mechanism"] == "vm_config_push"
    assert side["INC-0133"]["mechanism"] == "limit_below_batch_peak"
    srch = mech(side, "search_pool_rejection")
    assert len(srch) == 3 and all(side[i]["facts"]["limiter"] is False for i in srch)


# ------------------------------------------------------------- distribution

TYPE_TARGETS = {
    "high_cpu": 20,
    "memory_leak": 20,
    "disk_full": 20,
    "conn_pool_exhaustion": 22,
    "api_latency": 23,
    "redis_down": 20,
    "dns_failure": 20,
    "tls_expired": 14,
    "bad_deploy": 25,
    "rate_limiting": 16,
}
SEV_TARGETS = {"sev1": 16, "sev2": 54, "sev3": 90, "sev4": 40}


def test_failure_type_distribution(rows):
    assert Counter(r["failure_types"][0] for r in rows) == TYPE_TARGETS


def test_severity_distribution_within_5(rows):
    got = Counter(r["severity"] for r in rows)
    assert all(abs(got[s] - n) <= 5 for s, n in SEV_TARGETS.items()), got


def test_services_and_months_within_bounds(rows, services_doc):
    cause = Counter(r["service"] for r in rows)
    involved = Counter(s for r in rows for s in {r["service"], *r["symptom_services"]})
    for s in (x["name"] for x in services_doc["services"]):
        assert 3 <= cause[s] <= 20, (s, cause[s])
        assert involved[s] >= 5, (s, involved[s])
    months = Counter(f"{r['started_at']:%Y-%m}" for r in rows)
    assert len(months) == 21 and all(8 <= n <= 11 for n in months.values()), months


def test_every_runbook_used_by_at_least_3_incidents(rows, runbooks):
    used = Counter(rb for r in rows for rb in r["runbook_ids"])
    assert {rb: used[rb] for rb in runbooks if not 3 <= used[rb] <= 30} == {}
    # 16 rows link nothing: processor 429s before RB-016 existed (2026-05-26), EMFILE (no
    # runbook covers file descriptors), and a few one-offs. Real incidents often have no runbook.
    assert sum(not r["runbook_ids"] for r in rows) <= 20
    assert used["RB-014"] <= 15


def test_question_support(rows, side):
    freq = Counter(t for r in rows for t in {*r["codes"], *short(r["hosts"])})
    anchored = [i for i, s in side.items() if s.get("anchor_tokens")]
    assert len(anchored) >= 90
    for i in anchored:
        for tok in side[i]["anchor_tokens"]:
            assert freq[tok] <= 8 and tok not in {"503", "504", "429", "502"}, (i, tok)
    assert sum(len(r["runbook_ids"]) >= 2 for r in rows) >= 60


HARD_NEGATIVES = {
    tuple(p.split("/"))
    for p in (
        "010/011 010/012 011/012 010/013 001/013 010/015 017/018 017/019 006/019 014/016 002/014 "
        "003/014 004/016 002/016 020/021 004/021 014/021 020/022 021/022 002/022 016/023 023/024 "
        "023/029 012/024 010/024 014/025 023/025 018/026 009/027 001/027 020/027 015/028 017/028 "
        "012/028 016/029 016/030 003/030 014/030 029/030"
    ).split()
}


def test_hard_negative_links_are_declared_and_distinguishable(rows, side, runbooks):
    linked = 0
    for r in rows:
        nums = sorted(x[3:] for x in r["runbook_ids"])
        pairs = [
            (a, b) for k, a in enumerate(nums) for b in nums[k + 1 :] if (a, b) in HARD_NEGATIVES
        ]
        s = side[r["id"]]
        assert len(pairs) <= 1, r["id"]
        if not pairs:
            assert "hard_negative_pair" not in s, r["id"]
            continue
        linked += 1
        a, b = (f"RB-{x}" for x in pairs[0])
        assert s["hard_negative_pair"] == [a, b], r["id"]
        for tok in s["discriminators"]:
            assert (tok in runbooks[a]["body"]) != (tok in runbooks[b]["body"]), (r["id"], tok)
    assert linked <= 15


# --------------------------------------------------------------- demo guard


def test_r22_demo_guard(rows, demo):
    assert all(r["started_at"] < d("2026-10-01T00:00") for r in rows)
    pgb = {f"payments-pgbouncer-0{n}.prod.internal" for n in (1, 2)}
    for r in rows:
        text = " ".join([*r["codes"], *r["hosts"], r["service"]])
        assert not all(t in text for t in demo["leak_combination"]), r["id"]
        if "ECONNREFUSED" in r["codes"]:
            assert r["service"] != "payments-pgbouncer" and not pgb & set(r["hosts"]), r["id"]
        if r["service"] == "payments-pgbouncer":
            assert "bad_deploy" not in r["failure_types"], r["id"]


def test_demo_spec_is_consistent(demo, rows, runbooks):
    assert demo["id"] == "INC-LIVE-001"
    assert demo["timeline"]["date"].isoformat() > max(r["last_reviewed"] for r in runbooks.values())
    assert demo["binding_constraint"]["gate_live_since"].isoformat() == "2026-08-20"
    assert demo["binding_constraint"]["pgbouncer_enrolled"] is False
    assert "pgbouncer" not in runbooks["RB-027"]["body"]  # not in the gate's probe table
    assert demo["cause"]["mechanism_status"].startswith("placeholder")
    assert set(demo["evidence"]["runbooks"]) <= set(runbooks)
