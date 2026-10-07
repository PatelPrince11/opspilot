"""Load data/runbooks/RB-NNN.md (TOML front matter between +++ lines, markdown body)
and check each runbook against services.toml."""

import re
import tomllib
from datetime import date
from pathlib import Path

from scripts.corpus.services import CorpusError

FAILURE_TYPES = {
    "high_cpu",
    "memory_leak",
    "disk_full",
    "conn_pool_exhaustion",
    "api_latency",
    "redis_down",
    "dns_failure",
    "tls_expired",
    "bad_deploy",
    "rate_limiting",
}
FORMATS = {
    "long",
    "prose",
    "decision_tree",
    "checklist",
    "faq",
    "commands",
    "table",
    "procedure",
    "reference",
    "worked_example",
}
REQUIRED = {
    "id": str,
    "title": str,
    "services": list,
    "failure_types": list,
    "owner": str,
    "format": str,
    "last_reviewed": date,
    "synthetic": bool,
}
ID_RE = re.compile(r"^RB-\d{3}$")
HOST_RE = re.compile(r"\b[a-z][a-z0-9-]*\.prod\.internal\b")
# Documents may cite runbooks (RB-012) and past incidents (INC-0042). Anything else with
# those prefixes is malformed, including INC-LIVE-001: the open demo is never cited.
REF_RE = re.compile(r"\b(?:RB-\d{3}|INC-\d{4})\b")
REF_LIKE_RE = re.compile(r"\b(?:RB|INC)-[A-Za-z0-9-]*[A-Za-z0-9]")
FENCE = "+++"


def parse_runbook(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    if lines[0].strip() != FENCE:
        raise CorpusError(f"{path.name}: must start with a {FENCE} front matter line")
    try:
        end = lines.index(FENCE, 1)
    except ValueError:
        raise CorpusError(f"{path.name}: front matter has no closing {FENCE}") from None
    try:
        meta = tomllib.loads("\n".join(lines[1:end]))
    except tomllib.TOMLDecodeError as exc:
        raise CorpusError(f"{path.name}: bad front matter: {exc}") from None
    return {"file": path.name, "meta": meta, "body": "\n".join(lines[end + 1 :]).strip() + "\n"}


def load_runbooks(directory: Path) -> list[dict]:
    return sorted(
        (parse_runbook(p) for p in directory.glob("RB-*.md")), key=lambda r: r["meta"].get("id")
    )


def validate_runbooks(runbooks: list[dict], services_doc: dict) -> None:
    services = {s["name"]: s for s in services_doc["services"]}
    owners = {s["owner"] for s in services.values()}
    hosts = {h["name"] for s in services.values() for h in s["hosts"]}
    internal = {e["code"] for e in services_doc["error_codes"] if e["service"] != "*"}
    # Any token with an internal code's prefix (PAYAPI-, LEDGER-, ...) must be a catalog code.
    prefixes = sorted({c.split("-")[0] for c in internal})
    code_re = re.compile(rf"\b(?:{'|'.join(prefixes)})-[A-Z0-9]+\b")
    errors = []

    ids = [r["meta"].get("id") for r in runbooks]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        errors.append(f"duplicate runbook ids: {sorted(dupes)}")

    for r in runbooks:
        meta, where = r["meta"], r["file"]
        bad = [k for k, typ in REQUIRED.items() if not isinstance(meta.get(k), typ)]
        if bad:
            errors.append(f"{where}: missing or wrongly typed front matter keys: {bad}")
            continue
        if not ID_RE.match(meta["id"]) or f"{meta['id']}.md" != where:
            errors.append(f"{where}: id {meta['id']!r} must be RB-NNN and match the filename")
        if meta["synthetic"] is not True:
            errors.append(f"{where}: synthetic must be true")
        if not meta["services"]:
            errors.append(f"{where}: services is empty")
        for s in meta["services"]:
            if s not in services:
                errors.append(f"{where}: unknown service {s!r}")
        if meta["owner"] not in owners:
            errors.append(f"{where}: owner {meta['owner']!r} owns no service")
        ft = meta["failure_types"]
        if not 1 <= len(ft) <= 2 or len(set(ft)) != len(ft):
            errors.append(f"{where}: failure_types must hold 1 or 2 distinct values")
        for f in ft:
            if f not in FAILURE_TYPES:
                errors.append(f"{where}: unknown failure type {f!r}")
        if meta["format"] not in FORMATS:
            errors.append(f"{where}: format {meta['format']!r} not in {sorted(FORMATS)}")
        if not r["body"].strip():
            errors.append(f"{where}: empty body")
        for h in sorted(set(HOST_RE.findall(r["body"])) - hosts):
            errors.append(f"{where}: unknown host {h!r}")
        for c in sorted(set(code_re.findall(r["body"])) - internal):
            errors.append(f"{where}: unknown internal code {c!r}")
        for ref in sorted(set(REF_LIKE_RE.findall(r["body"]))):
            if not REF_RE.fullmatch(ref):
                errors.append(f"{where}: malformed reference {ref!r} (use RB-NNN or INC-NNNN)")

    if errors:
        raise CorpusError("runbooks:\n  " + "\n  ".join(errors))


def headings(body: str) -> tuple[str, ...]:
    """Markdown heading lines, skipping fenced code (shell comments start with # too)."""
    out, in_code = [], False
    for line in body.split("\n"):
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and re.match(r"^#{1,6} ", line):
            out.append(line.lstrip("#").strip().lower())
    return tuple(out)


def word_count(body: str) -> int:
    return len(body.split())


def runbooks_output(runbooks: list[dict]) -> list[dict]:
    """Rows for data/processed/runbooks.jsonl, sorted by id."""
    return [
        {
            "id": r["meta"]["id"],
            "doc_type": "runbook",
            "title": r["meta"]["title"],
            "services": r["meta"]["services"],
            "failure_types": r["meta"]["failure_types"],
            "owner": r["meta"]["owner"],
            "format": r["meta"]["format"],
            "last_reviewed": r["meta"]["last_reviewed"].isoformat(),
            "synthetic": True,
            "word_count": word_count(r["body"]),
            "body": r["body"],
        }
        for r in runbooks
    ]
