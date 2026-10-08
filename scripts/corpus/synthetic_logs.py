"""Synthetic log events for documented incidents, from hand-written specs.

data/incidents/logs/INC-NNNN.toml lists fixed lines ([[line]]: one time) and bursts
([[burst]]: a count spread over a time range). Times are UTC clock times on the incident's
start date unless an entry gives `date` (and a burst `to_date`). The generator only places
and fills: timestamps, host picks and {hex8} / {int:a-b} / {choice:a|b} placeholders come
from rng_for(incident id), so one incident's logs never depend on another's. {ramp:a-b}
(bursts only) is a counter that moves from a to b over the burst in time order, so a queue,
backlog or lag never jumps back; a > b counts down.

Logs are a sample of what the services wrote. Counts in a write-up come from metrics.
"""

import ipaddress
import re
import tomllib
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from scripts.corpus.common import rng_for
from scripts.corpus.incidents import code_finder
from scripts.corpus.runbooks import HOST_RE
from scripts.corpus.services import CorpusError

LEVELS = {"DEBUG", "INFO", "WARN", "ERROR", "FATAL"}
WINDOW = timedelta(minutes=10)  # logs sit in [start - 10 min, end + 10 min]
IP_PORT_RE = re.compile(r"\b(10\.\d+\.\d+\.\d+)(?::(\d+))?\b")
PLACEHOLDER_RE = re.compile(r"\{(hex8|int:(\d+)-(\d+)|choice:([^}]*)|ramp:(\d+)-(\d+))\}")


def load_spec(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def _at(day: date, clock: str) -> datetime:
    return datetime.combine(day, datetime.strptime(clock, "%H:%M:%S").time(), tzinfo=UTC)


def _fill(template: str, rng, frac: float | None = None) -> str:
    """frac is the event's position in its burst (0 at `from`, 1 at `to`); ramps need it."""

    def sub(m):
        if m.group(1) == "hex8":
            return f"{rng.getrandbits(32):08x}"
        if m.group(2):
            return str(rng.randint(int(m.group(2)), int(m.group(3))))
        if m.group(5):
            if frac is None:
                raise ValueError("{ramp:a-b} is only allowed in a [[burst]]")
            a, b = int(m.group(5)), int(m.group(6))
            return str(round(a + (b - a) * frac))
        return rng.choice(m.group(4).split("|"))

    return PLACEHOLDER_RE.sub(sub, template)


def generate(incident_id: str, spec: dict, start_day: date) -> list[dict]:
    rng = rng_for(f"logs:{incident_id}")
    events = []

    def add(ts, entry, host, frac=None):
        events.append(
            {
                "ts": ts,
                "service": entry["service"],
                "level": entry["level"],
                "message": f"{host} {_fill(entry['message'], rng, frac)}",
                "source": "synthetic",
                "incident_id": incident_id,
                "corpus": "main",
            }
        )

    for e in spec.get("line", []):
        day = date.fromisoformat(e["date"]) if "date" in e else start_day
        ts = _at(day, e["at"]) + timedelta(milliseconds=rng.randrange(1000))
        add(ts, e, e["host"])
    for b in spec.get("burst", []):
        day = date.fromisoformat(b["date"]) if "date" in b else start_day
        lo = _at(day, b["from"])
        hi = _at(date.fromisoformat(b["to_date"]) if "to_date" in b else day, b["to"])
        span = int((hi - lo).total_seconds() * 1000)
        offsets = sorted(rng.randrange(span + 1) for _ in range(b["count"]))
        for ms in offsets:
            add(
                lo + timedelta(milliseconds=ms), b, rng.choice(b["hosts"]), ms / span if span else 0
            )
    return sorted(events, key=lambda e: (e["ts"], e["service"], e["message"]))


def validate_logs(events: list[dict], row: dict, services_doc: dict) -> None:
    services = {s["name"]: s for s in services_doc["services"]}
    involved = {row["service"], *row["symptom_services"]}
    host_owner = {h["name"]: s["name"] for s in services.values() for h in s["hosts"]}
    ip_owner = {h["ip"]: s["name"] for s in services.values() for h in s["hosts"]}
    find_codes = code_finder(services_doc)
    lo, hi = row["started_at"] - WINDOW, row["resolved_at"] + WINDOW
    where, errors, seen = row["id"], [], set()
    pgb = any("pgbouncer" in h for h in row["hosts"]) or row["service"] == "payments-pgbouncer"
    for e in events:
        msg = e["message"]
        host = msg.split(" ", 1)[0]
        if e["service"] not in involved:
            errors.append(f"{where}: service {e['service']!r} not involved")
        if host_owner.get(host) != e["service"]:
            errors.append(f"{where}: line must start with a host of {e['service']}: {msg[:60]!r}")
        for h in HOST_RE.findall(msg):
            if host_owner.get(h) not in involved:
                errors.append(f"{where}: host {h!r} not of an involved service")
        for ip, port in IP_PORT_RE.findall(msg):
            owner = ip_owner.get(ip)
            if owner not in involved or not ipaddress.ip_address(ip).is_private:
                errors.append(f"{where}: ip {ip} is not a host of an involved service")
            elif port and int(port) != services[owner]["port"]:
                errors.append(f"{where}: {ip}:{port} is not {owner}'s port")
        found, unknown = find_codes(msg)
        seen |= found
        for c in sorted(found - set(row["codes"])) + sorted(unknown):
            errors.append(f"{where}: code {c!r} is not one of the row's codes")
        if e["level"] not in LEVELS:
            errors.append(f"{where}: level {e['level']!r}")
        if not lo <= e["ts"] <= hi:
            errors.append(f"{where}: {e['ts']} outside start-10min..end+10min")
        if e["source"] != "synthetic" or e["incident_id"] != row["id"]:
            errors.append(f"{where}: source must be synthetic and incident_id set")
        if "INC-LIVE" in msg or (pgb and ("ECONNREFUSED" in msg or "6432" in msg)):
            errors.append(f"{where}: demo guard: {msg[:60]!r}")
    for c in sorted(set(row["codes"]) - seen):
        errors.append(f"{where}: row code {c!r} never logged")
    if errors:
        raise CorpusError("synthetic logs:\n  " + "\n  ".join(dict.fromkeys(errors)))
