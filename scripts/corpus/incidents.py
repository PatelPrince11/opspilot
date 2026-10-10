"""Load data/incidents/skeleton.toml (200 incident rows) and the incident write-ups
(data/incidents/INC-NNNN.md), and check both against services.toml and the runbooks.
Eval-only labels live in eval/corpus/ and are checked by the tests, never here."""

import re
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts.corpus.runbooks import (
    FAILURE_TYPES,
    HOST_RE,
    REF_LIKE_RE,
    REF_RE,
    parse_runbook,
    word_count,
)
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


# ------------------------------------------------------------ incident documents
# data/incidents/INC-NNNN.md: TOML front matter (+++) and a markdown body. Row fields stay
# in skeleton.toml; the document adds only what a tracker's write-up would.

DOC_FORMATS = {
    "postmortem",
    "table",
    "ticket",
    "handoff",
    "qa",
    "brief",
    "narrative",
    "timeline",
    "chat_digest",
}
DOC_REQUIRED = {
    "id": str,
    "title": str,
    "format": str,
    "synthetic": bool,
    "symptoms": str,
    "root_cause": str,
    "resolution": str,
}
# Eval-only vocabulary never belongs in a document (the sidecar keeps it out of data/).
LABEL_RE = re.compile(
    r"\b(?:mechanism|constraint|anchor|anchor_tokens|discriminators?|absent_codes|"
    r"cause_component|hard_negative(?:_pair)?|multi_doc)\b",
    re.IGNORECASE,
)
SEV_RE = re.compile(r"\bsev ?([0-9])\b", re.IGNORECASE)
TIME_RE = re.compile(r"(?<![\d:.])([01]\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?(?![\d:])")
MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December"
DATE_RE = re.compile(
    rf"\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{1,2}} (?:{MONTHS})\b|\b(?:{MONTHS}) \d{{1,2}}\b"
)
# A timeline entry: a line that opens with an optional ISO date, then a clock time.
TIMELINE_RE = re.compile(
    r"^\s*(?:[-*|]\s*)?(?:\*\*)?(?:(\d{4}-\d{2}-\d{2})[ T])?"
    r"((?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?)"
)
# Dated facts (NOTES Fictional facts / skeleton rules), as (pattern, date). A write-up is
# written within a week of resolution, so it may not mention anything later than that.
ANACHRONISMS = [
    (r"CPX-W430", "2026-07-09"),
    (r"CPX_LOCAL_RPS", "2026-07-09"),
    (r"ndots:2", "2026-06-18"),
    (r"\b4\.12\b", "2025-11-12"),
    (r"LEDGER_RECON_PARALLELISM=4", "2026-02-01"),
    (r"lock_timeout", "2025-05-16"),
    (r"probe table", "2026-08-20"),
    # fraud-scorer: WEB_CONCURRENCY unset until 2026-05-29, 16 since. Before that it may
    # appear only as unset.
    (
        r"WEB_CONCURRENCY`?\s*=\s*\d|\bset(?:s|ting)?\s+`?WEB_CONCURRENCY"
        r"|WEB_CONCURRENCY`?\s+(?:is|was)\s+(?:already\s+)?set\b",
        "2026-05-29",
    ),
    # edge /search local limit: 100 per pod until 2026-03-10, 250 since.
    (
        r"(?:limit|bucket|tokens?)[^.\n]{0,30}\b250\b"
        r"|\b250\b[^.\n]{0,30}(?:per pod|/pod|tokens?|limit)",
        "2026-03-10",
    ),
    # auth-service reads both session encodings only since 2026-07-22.
    (r"dual[- ]read|reads both (?:the )?old and", "2026-07-22"),
]
# Facts that stopped being true: (pattern, date). An incident that starts after the date may
# not state them.
SUPERSEDED = [
    (r"WEB_CONCURRENCY`?\s+(?:is|was)\s+(?:not set|unset)|WEB_CONCURRENCY`?\s+unset", "2026-05-29"),
]
# Never true for the incident being written: SystemMaxUse is applied per VM as RB-008's fix,
# so a write-up may say it was applied, never that the VM already had it.
NEVER = [
    r"SystemMaxUse[^.\n]*\balready\b|\balready\b[^.\n]*SystemMaxUse",
    r"SystemMaxUse[^.\n]{0,40}\b(?:was|were|is)\s+(?:set|in place|configured|present)\b"
    r"|\b(?:had|has)\s+(?:a\s+|the\s+)?`?SystemMaxUse",
    # WEB_CONCURRENCY was only ever set to 16 (2026-05-29); any other value is invented.
    r"WEB_CONCURRENCY`?\s*=\s*`?(?!16\b)\d+",
]
# Phrases that assert something outside the incident the write-up can't back (ticket ages,
# next-day confirmations, absolute no-loss claims, provenance). Checked in every write-up.
BLOCKED_PHRASES = [
    "confirmed the next morning",
    "sat in the queue",
    "written up from",
    "nothing was lost",
    "no event was lost",
    # batch 7: statements by outside parties nothing in the corpus records
    "hosting provider",
    "told us it saw",
    "the processor said",
    "the processor told",
]
# Real-world log formats seen in drafts but never checked against a source (batch 7 review).
# A write-up or synthetic log line may not carry them; describe the event in words instead.
UNVERIFIED_STRINGS = [
    "+sdown",  # Redis Sentinel event lines: NOTES verifies the events exist, not the format
    "+odown",
    "+switch-master",
    "Started coredns.service",  # systemd job.c prints the Description=, unknown for coredns
]
# Runbooks written as the story of one incident did not exist before it (skeleton test and
# write-up validator): a write-up may not cite one for an incident that started earlier.
NARRATIVE = {
    "RB-013": "2026-01-31",
    "RB-016": "2026-05-26",
    "RB-021": "2026-06-15",
    "RB-023": "2025-01-14",  # its samples and History are the 2025-01-14 night (INC-0004)
    "RB-028": "2025-05-15",
    "RB-030": "2026-03-10",
}
WRITE_UP_DAYS = 7


def postdated(row: dict) -> set[str]:
    """Runbook ids of the row that were written about this incident (dated on or after its
    start): labels for eval, never named in its write-up."""
    start = row["started_at"].date().isoformat()
    return {rb for rb in row["runbook_ids"] if rb in NARRATIVE and start <= NARRATIVE[rb]}


def anachronisms(text: str, row: dict) -> list[str]:
    errors = []
    written = (row["resolved_at"].date() + timedelta(days=WRITE_UP_DAYS)).isoformat()
    for pat, since in ANACHRONISMS:
        if (m := re.search(pat, text)) and written < since:
            errors.append(f"{m.group(0)!r} exists only from {since}")
    for pat, until in SUPERSEDED:
        if (m := re.search(pat, text)) and row["started_at"].date().isoformat() > until:
            errors.append(f"{m.group(0)!r} stopped being true on {until}")
    for pat in NEVER:
        if m := re.search(pat, text):
            errors.append(f"{m.group(0)!r} is never true here")
    return errors


def load_documents(directory: Path) -> list[dict]:
    return sorted((parse_runbook(p) for p in directory.glob("INC-*.md")), key=lambda d: d["file"])


def doc_text(doc: dict) -> str:
    """Everything a reader sees: the body plus the one-line tracker fields."""
    m = doc["meta"]
    return "\n".join(
        [
            m.get("title", ""),
            m.get("symptoms", ""),
            m.get("root_cause", ""),
            m.get("resolution", ""),
            doc["body"],
        ]
    )


def code_finder(services_doc: dict):
    """Catalog tokens only (services.toml error_codes): exact strings, case-sensitive, not
    part of a longer token. Bare HTTP numbers (429/502/503/504) are catalog tokens, so they
    match only standing alone: `SRCH-E502` holds no `502`. Anything else is free text."""
    catalog = sorted((e["code"] for e in services_doc["error_codes"]), key=len, reverse=True)
    alts = "|".join(re.escape(c) for c in catalog)
    exact = re.compile(rf"(?<![\w-])(?:{alts})(?![\w-])")
    internal = {e["code"] for e in services_doc["error_codes"] if e["service"] != "*"}
    prefixes = sorted({c.split("-")[0] for c in internal})
    prefixed = re.compile(rf"\b(?:{'|'.join(prefixes)})-[A-Z0-9]+\b")

    def find(text: str) -> tuple[set[str], set[str]]:
        """(catalog codes found, unknown tokens with an internal prefix)."""
        return set(exact.findall(text)), set(prefixed.findall(text)) - internal

    return find


def short_host_re(services_doc: dict) -> re.Pattern:
    names = sorted((s["name"] for s in services_doc["services"]), key=len, reverse=True)
    return re.compile(rf"(?<![\w-])((?:{'|'.join(map(re.escape, names))})-\d{{2}})(?![\w-]|\.\w)")


def _minutes(t: re.Match) -> float:
    return int(t.group(1)) * 60 + int(t.group(2)) + int(t.group(3) or 0) / 60


def check_times(where: str, text: str, row: dict) -> list[str]:
    errors = []
    start, end = row["started_at"], row["resolved_at"]
    for ts in (start, end):
        if f"{ts:%H:%M}" not in text:
            errors.append(f"{where}: clock time {ts:%H:%M} (start/end) not stated")
    lo, hi = start - timedelta(hours=2), end + timedelta(hours=2)
    days = [
        start.date() + timedelta(days=i) for i in range(-1, (end.date() - start.date()).days + 2)
    ]
    for line in text.split("\n"):
        if DATE_RE.search(line):
            continue  # an explicitly dated line may refer to another day
        for t in TIME_RE.finditer(line):
            m = _minutes(t)
            cands = [
                datetime(d.year, d.month, d.day, tzinfo=UTC) + timedelta(minutes=m) for d in days
            ]
            if not any(lo <= c <= hi for c in cands):
                errors.append(f"{where}: time {t.group(0)} outside start-2h..end+2h with no date")
    # Timeline entries never go backwards; a drop of more than 12 h is the next day.
    prev, day = None, timedelta(0)
    for line in text.split("\n"):
        m = TIMELINE_RE.match(line)
        if not m:
            continue
        hh, mm, *ss = m.group(2).split(":")
        clock = timedelta(hours=int(hh), minutes=int(mm), seconds=int(ss[0]) if ss else 0)
        if m.group(1):
            cur = datetime.fromisoformat(m.group(1)).replace(tzinfo=UTC) + clock
            day = cur - datetime(start.year, start.month, start.day, tzinfo=UTC) - clock
        else:
            cur = datetime(start.year, start.month, start.day, tzinfo=UTC) + day + clock
            if prev and cur < prev - timedelta(hours=12):
                day += timedelta(days=1)
                cur += timedelta(days=1)
        if prev and cur < prev:
            errors.append(f"{where}: timeline goes backwards at {m.group(2)}")
        prev = cur
    return errors


# The groups document_errors reports under, in the order `make selfcheck` prints them.
DOC_CHECKS = (
    "front matter",
    "hosts",
    "codes",
    "runbook refs",
    "references",
    "severity",
    "labels",
    "anachronisms",
    "blocklist",
    "demo guard",
    "times",
)


def document_errors(doc: dict, row: dict, find_codes, short_re) -> dict[str, list[str]]:
    """Every validator rule for one write-up against its row, grouped by check name (the
    build flattens the groups; `make selfcheck` prints one line per group)."""
    meta, where = doc["meta"], doc["file"]
    groups: dict[str, list[str]] = {}

    def err(group, msg):
        groups.setdefault(group, []).append(f"{where}: {msg}")

    if meta["format"] not in DOC_FORMATS:
        err("front matter", f"format {meta['format']!r}")
    if meta["synthetic"] is not True:
        err("front matter", "synthetic must be true")
    text = doc_text(doc)
    row_hosts = set(row["hosts"])
    for h in sorted(set(HOST_RE.findall(text)) - row_hosts):
        err("hosts", f"host {h!r} is not one of the row's hosts")
    short = {h.split(".")[0] for h in row_hosts}
    for h in sorted(set(short_re.findall(text)) - short):
        err("hosts", f"host {h!r} is not one of the row's hosts")
    found, unknown = find_codes(text)
    for c in sorted(found - set(row["codes"])):
        err("codes", f"code {c!r} is not one of the row's codes")
    for c in sorted(set(row["codes"]) - found):
        err("codes", f"row code {c!r} never stated")
    for c in sorted(unknown):
        err("codes", f"unknown internal code {c!r}")
    refs = set(REF_RE.findall(text))
    # A runbook written about this very incident (NARRATIVE date on or after its start)
    # stays in runbook_ids as a relevance label but is never named in the write-up.
    named = set(row["runbook_ids"]) - postdated(row)
    if {r for r in refs if r.startswith("RB-")} != named:
        err("runbook refs", f"runbook references must equal {sorted(named)}")
    for rb in sorted(refs & NARRATIVE.keys()):
        if row["started_at"].date().isoformat() <= NARRATIVE[rb]:
            err("runbook refs", f"{rb} did not exist before {NARRATIVE[rb]}")
    if {r for r in refs if r.startswith("INC-")}:
        err("references", "no incident references in write-ups")
    for ref in sorted(set(REF_LIKE_RE.findall(text)) - refs):
        err("references", f"malformed reference {ref!r}")
    sevs = {f"sev{n}" for n in SEV_RE.findall(text)}
    if sevs != {row["severity"]}:
        err("severity", f"severity stated as {sorted(sevs)}, row says {row['severity']}")
    if m := LABEL_RE.search(text):
        err("labels", f"eval label word {m.group(0)!r}")
    for e in anachronisms(text, row):
        err("anachronisms", e)
    low = text.lower()
    for phrase in BLOCKED_PHRASES:
        if phrase in low:
            err("blocklist", f"unsupported claim {phrase!r}")
    for string in UNVERIFIED_STRINGS:
        if string in text:
            err("blocklist", f"unverified real-world string {string!r}")
    if row["service"] == "payments-pgbouncer" or any("pgbouncer" in h for h in row_hosts):
        if "ECONNREFUSED" in text or "6432" in text:
            err("demo guard", "demo guard: pgbouncer with ECONNREFUSED or 6432")
    times = check_times(where, text, row)
    if times:
        groups.setdefault("times", []).extend(times)
    return groups


def front_matter_errors(doc: dict, by_id: dict) -> list[str]:
    """Checks that must pass before a write-up can be matched to its row."""
    meta, where = doc["meta"], doc["file"]
    bad = [k for k, typ in DOC_REQUIRED.items() if not isinstance(meta.get(k), typ)]
    if bad:
        return [f"{where}: missing or wrongly typed front matter keys: {bad}"]
    if meta["id"] not in by_id or f"{meta['id']}.md" != where:
        return [f"{where}: id must be a skeleton row and match the filename"]
    return []


def validate_documents(
    docs: list[dict], rows: list[dict], services_doc: dict, runbook_ids: set[str]
) -> None:
    by_id = {r["id"]: r for r in rows}
    find_codes = code_finder(services_doc)
    short_re = short_host_re(services_doc)
    errors = []
    for doc in docs:
        if fm := front_matter_errors(doc, by_id):
            errors += fm
            continue
        row = by_id[doc["meta"]["id"]]
        for group in document_errors(doc, row, find_codes, short_re).values():
            errors += group
    if errors:
        raise CorpusError("incident documents:\n  " + "\n  ".join(errors))


def incidents_output(docs: list[dict], rows: list[dict]) -> list[dict]:
    """Rows for data/processed/incidents.jsonl: skeleton fields plus the write-up."""
    by_id = {r["id"]: r for r in rows}
    out = []
    for doc in sorted(docs, key=lambda d: d["meta"]["id"]):
        m, r = doc["meta"], by_id[doc["meta"]["id"]]
        out.append(
            {
                "id": m["id"],
                "doc_type": "incident",
                "title": m["title"],
                "format": m["format"],
                "service": r["service"],
                "symptom_services": r["symptom_services"],
                "severity": r["severity"],
                "started_at": r["started_at"].isoformat().replace("+00:00", "Z"),
                "resolved_at": r["resolved_at"].isoformat().replace("+00:00", "Z"),
                "failure_types": r["failure_types"],
                "codes": r["codes"],
                "hosts": r["hosts"],
                "runbook_ids": r["runbook_ids"],
                "symptoms": m["symptoms"],
                "root_cause": m["root_cause"],
                "resolution": m["resolution"],
                "synthetic": True,
                "word_count": word_count(doc["body"]),
                "body": doc["body"],
            }
        )
    return out
