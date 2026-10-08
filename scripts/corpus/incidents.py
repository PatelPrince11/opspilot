"""Load data/incidents/skeleton.toml (200 incident rows, no prose yet) and check each row
against services.toml and the runbooks. Eval-only labels live in eval/corpus/ and are
checked by tests/test_incidents.py, never here."""

import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from scripts.corpus.runbooks import FAILURE_TYPES
from scripts.corpus.services import CorpusError

SEVERITIES = {"sev1", "sev2", "sev3", "sev4"}  # matches the CHECK on incidents.severity
REQUIRED = {
    "id": str,
    "service": str,
    "symptom_services": list,
    "severity": str,
    "started_at": datetime,
    "resolved_at": datetime,
    "failure_types": list,
    "codes": list,
    "hosts": list,
    "runbook_ids": list,
    "synthetic": bool,
}
FIRST = datetime(2025, 1, 1, tzinfo=UTC)
END = datetime(2026, 10, 1, tzinfo=UTC)  # every row starts before October 2026
DEMO = datetime(2026, 10, 6, tzinfo=UTC)  # INC-LIVE-001 day: every row is resolved before it
COUNT = 200


def load_skeleton(path: Path) -> list[dict]:
    with path.open("rb") as f:
        return tomllib.load(f)["incidents"]


def validate_skeleton(rows: list[dict], services_doc: dict, runbook_ids: set[str]) -> None:
    services = {s["name"]: s for s in services_doc["services"]}
    host_owner = {h["name"]: s["name"] for s in services.values() for h in s["hosts"]}
    codes = {e["code"]: e["service"] for e in services_doc["error_codes"]}
    errors = []

    ids = [r.get("id") for r in rows]
    if ids != [f"INC-{n:04d}" for n in range(1, COUNT + 1)]:
        errors.append(f"ids must be INC-0001..INC-{COUNT:04d} in order")

    prev = None
    for r in rows:
        where = r.get("id", "?")
        bad = [k for k, typ in REQUIRED.items() if not isinstance(r.get(k), typ)]
        if bad:
            errors.append(f"{where}: missing or wrongly typed keys: {bad}")
            continue
        start, end = r["started_at"], r["resolved_at"]
        if start.utcoffset() is None or start.utcoffset().total_seconds() != 0:
            errors.append(f"{where}: started_at must be UTC")
        if not FIRST <= start < END:
            errors.append(f"{where}: started_at {start} outside 2025-01..2026-09")
        if not start <= end < DEMO:
            errors.append(f"{where}: resolved_at must be >= started_at and before the demo")
        if prev and start <= prev:
            errors.append(f"{where}: ids must be chronological by started_at")
        prev = start
        if r["severity"] not in SEVERITIES:
            errors.append(f"{where}: severity {r['severity']!r}")
        ft = r["failure_types"]
        if not 1 <= len(ft) <= 2 or len(set(ft)) != len(ft) or not set(ft) <= FAILURE_TYPES:
            errors.append(f"{where}: failure_types must be 1-2 distinct known types: {ft}")
        involved = {r["service"], *r["symptom_services"]}
        unknown = involved - services.keys()
        if unknown:
            errors.append(f"{where}: unknown services {sorted(unknown)}")
        if r["service"] in r["symptom_services"]:
            errors.append(f"{where}: service is also listed as a symptom service")
        for h in r["hosts"]:
            if h not in host_owner:
                errors.append(f"{where}: unknown host {h!r}")
            elif host_owner[h] not in involved:
                errors.append(f"{where}: host {h!r} belongs to {host_owner[h]}, not involved")
        for c in r["codes"]:
            if c not in codes:
                errors.append(f"{where}: code {c!r} not in the catalog")
            elif codes[c] != "*" and codes[c] not in involved:
                errors.append(f"{where}: code {c!r} belongs to {codes[c]}, not involved")
        rbs = r["runbook_ids"]
        if len(rbs) > 3 or len(set(rbs)) != len(rbs) or not set(rbs) <= runbook_ids:
            errors.append(f"{where}: runbook_ids must be 0-3 distinct existing runbooks: {rbs}")
        if r["synthetic"] is not True:
            errors.append(f"{where}: synthetic must be true")
        if any(re.search(r"INC-LIVE", str(v)) for v in r.values()):
            errors.append(f"{where}: the demo incident is never referenced")

    if errors:
        raise CorpusError("incident skeleton:\n  " + "\n  ".join(errors))
