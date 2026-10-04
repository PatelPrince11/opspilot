"""Load data/services.toml and check it is internally consistent."""

import ipaddress
import re
import tomllib
from pathlib import Path

STATUSES = {"healthy", "degraded", "down"}  # matches the CHECK on services.status
KINDS = {"app", "datastore"}
NAME_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class CorpusError(ValueError):
    """The corpus sources break a rule; the message says which and where."""


def load_services(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def validate_services(doc: dict) -> None:
    services = doc.get("services", [])
    names = [s["name"] for s in services]
    errors = []

    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        errors.append(f"duplicate service names: {sorted(dupes)}")

    domain = doc["meta"]["domain"]
    seen_hosts, seen_ips = set(), set()
    for s in services:
        name = s["name"]
        if not NAME_RE.match(name):
            errors.append(f"{name}: name must be lowercase kebab-case")
        if s.get("status") not in STATUSES:
            errors.append(f"{name}: status {s.get('status')!r} not in {sorted(STATUSES)}")
        if s.get("kind") not in KINDS:
            errors.append(f"{name}: kind {s.get('kind')!r} not in {sorted(KINDS)}")
        if not s.get("owner"):
            errors.append(f"{name}: missing owner")
        for dep in s.get("dependencies", []):
            if dep not in names:
                errors.append(f"{name}: dependency {dep!r} is not a defined service")
            if dep == name:
                errors.append(f"{name}: depends on itself")
        if not s.get("hosts"):
            errors.append(f"{name}: no hosts")
        host_re = re.compile(rf"^{re.escape(name)}-\d{{2}}\.{re.escape(domain)}$")
        for h in s.get("hosts", []):
            if not host_re.match(h["name"]):
                errors.append(f"{name}: host {h['name']!r} must look like {name}-NN.{domain}")
            if h["name"] in seen_hosts:
                errors.append(f"duplicate host {h['name']}")
            if h["ip"] in seen_ips:
                errors.append(f"duplicate ip {h['ip']}")
            if not ipaddress.ip_address(h["ip"]).is_private:
                errors.append(f"{name}: ip {h['ip']} is not private")
            seen_hosts.add(h["name"])
            seen_ips.add(h["ip"])

    cycle = _find_cycle({s["name"]: s.get("dependencies", []) for s in services})
    if cycle:
        errors.append(f"dependency cycle: {' -> '.join(cycle)}")

    for e in doc.get("error_codes", []):
        if e["service"] != "*" and e["service"] not in names:
            errors.append(f"error code {e['code']}: unknown service {e['service']!r}")
    codes = [e["code"] for e in doc.get("error_codes", [])]
    if len(codes) != len(set(codes)):
        errors.append("duplicate error codes")

    if errors:
        raise CorpusError("services.toml:\n  " + "\n  ".join(errors))


def _find_cycle(graph: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}  # 1 = on the current path, 2 = done
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for dep in graph.get(node, []):
            if state.get(dep) == 1:
                return path[path.index(dep) :] + [dep]
            if dep not in state and (found := visit(dep)):
                return found
        path.pop()
        state[node] = 2
        return None

    for node in graph:
        if node not in state and (found := visit(node)):
            return found
    return None


def services_output(doc: dict) -> list[dict]:
    """Rows for data/processed/services.json, sorted by name."""
    return sorted(
        (
            {
                "name": s["name"],
                "kind": s["kind"],
                "owner": s["owner"],
                "tech": s["tech"],
                "port": s["port"],
                "status": s["status"],
                "dependencies": s["dependencies"],
                "hosts": s["hosts"],
                "synthetic": True,
            }
            for s in doc["services"]
        ),
        key=lambda r: r["name"],
    )
