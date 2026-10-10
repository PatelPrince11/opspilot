"""Write-up checks beyond the build validator: the ones that need eval-only labels
(eval/corpus/) or the whole corpus (leakage, diversity). tests/test_incident_docs.py and
`make selfcheck` both call these, so each rule lives in one place. The build never reads
eval/, so none of this runs in `make corpus`."""

import re
import tomllib

from scripts.corpus.common import SIDECAR
from scripts.corpus.incidents import TIME_RE, doc_text
from scripts.corpus.runbooks import headings, word_count

JACCARD_TARGET = 0.025  # working ceiling for write-ups
TRIGRAM_JACCARD_THRESHOLD = 0.03  # hard test threshold (runbooks and write-ups)
SHARED_RUN = 8  # no run of this many words shared with a runbook or another write-up
LOG_LINES = (40, 160)
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
# Boolean facts become text that must not appear.
FORBIDDEN_BY_FACT = {
    "wal_or_disk": [r"\bWAL\b", r"\bdisk\b"],
    "retry_storm": [r"retry storm"],
    "ledger_cpu_spin": [r"\bspin"],
    "after_rb018_review": [r"RB-018 (?:records|lists|describes this|mentions this)"],
    "internal_dns_healthy": [r"internal-dns (?:was|were) (?:down|unhealthy|broken|failing)"],
    "data_lost": [
        r"\b(?:data|sessions?) (?:was|were) (?:lost|deleted|wiped)",
        r"\blost (?:the )?data\b",
    ],
    # batch 7
    "last_retry_spin": [r"\b(?:spun|spin\w*) (?:again|once more)\b"],
    "data_kafka_involved": [
        r"/data/kafka (?:was|were|had|filled|is) (?:full|filling|involved|also)"
    ],
    "payments_inventory_extra_db_reads": [r"(?<!no )(?<!without )extra (?:DB|database) reads"],
    "intermittent": [r"\bevery (?:lookup|query|resolution) (?:failed|returned)"],
}
DUR_RE = re.compile(r"\b(\d+)[ -](?:minutes?|min|hours?|h)\b")
DURATION_WHITELIST = {"INC-0171": {23, 168}}


def load_sidecar() -> dict:
    with (SIDECAR / "incidents_sidecar.toml").open("rb") as f:
        return {s["id"]: s for s in tomllib.load(f)["incidents"]}


def has_token(text, token):
    return re.search(rf"(?<![\w.,]){re.escape(token)}(?!\w|[.,]\d)", text) is not None


def hm(s):
    """Minutes since midnight; seconds count as a fraction of a minute."""
    h, m, *sec = s.split(":")
    return int(h) * 60 + int(m) + (int(sec[0]) / 60 if sec else 0)


def trigrams(text):
    words = re.findall(r"[a-z0-9_]+", text.lower())
    return {tuple(words[i : i + 3]) for i in range(len(words) - 2)}


def jaccard(a, b):
    return len(a & b) / len(a | b)


def prose_ngrams(text, n=SHARED_RUN):
    """Word n-grams of the prose; inline code and code blocks don't count."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"`[^`]*`", " ", text)
    words = re.findall(r"[a-z0-9_]+", text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


def leaks_demo(body):
    """Only the demo's specific host + error + port combination (from demo_answer.toml)
    counts, so generic config-push runbooks (RB-027) pass."""
    with (SIDECAR / "demo_answer.toml").open("rb") as f:
        combo = tomllib.load(f)["leak_combination"]
    return all(t in body for t in combo)


def fact_errors(text: str, entry: dict, logtext: str = "") -> list[str]:
    """Sidecar facts present (numbers, times, values), false booleans not contradicted,
    discriminators present, absent codes absent from prose and logs."""
    errors = []
    for c in entry.get("absent_codes", []):
        if c in text or c in logtext:
            errors.append(f"absent code {c!r} appears")
    for key, val in entry.get("facts", {}).items():
        if isinstance(val, bool):
            for pat in FORBIDDEN_BY_FACT[key]:  # every boolean fact needs an entry
                if re.search(pat, text, re.IGNORECASE):
                    errors.append(f"fact {key}={val} contradicted ({pat!r})")
        elif isinstance(val, int | float):
            if not (has_token(text, f"{val:g}") or has_token(text, f"{val:,}")):
                errors.append(f"fact {key}={val} not stated")
        elif isinstance(val, str) and TIME_RE.search(val):
            for t in TIME_RE.findall(val):
                if ":".join(x for x in t if x) not in text:
                    errors.append(f"fact {key}={val} not stated")
        elif isinstance(val, str) and ("/" in val or "=" in val) and val not in text:
            errors.append(f"fact {key}={val!r} not stated")
    for tok in entry.get("discriminators", []):
        if tok not in text:
            errors.append(f"discriminator {tok!r} missing")
    return errors


def sidecar_labels(side: dict, runbooks: list[dict]) -> set[str]:
    """Mechanism tags and snake_case sidecar keys; those that are also real runbook
    vocabulary (e.g. `s_client`) are exempt."""
    labels = {s["mechanism"] for s in side.values()}
    labels |= {k for s in side.values() for k in s if "_" in k}
    labels |= {k for s in side.values() for k in s.get("facts", {}) if "_" in k}
    return labels - {w for r in runbooks for w in re.findall(r"[a-z0-9_]+", r["body"])}


def label_errors(text: str, labels: set[str]) -> list[str]:
    hits = labels & set(re.findall(r"[a-z0-9_]+", text))
    return [f"sidecar label words {sorted(hits)}"] if hits else []


def word_count_errors(doc: dict) -> list[str]:
    fmt = doc["meta"]["format"]
    lo, hi = WORDS[fmt]
    n = word_count(doc["body"])
    return [] if lo <= n <= hi else [f"{n} words; {fmt} needs {lo}-{hi}"]


def duration_errors(doc: dict, row: dict) -> list[str]:
    """Every "N minutes" / "N h" is the total, a gap between two stated times, or whitelisted."""
    text = doc_text(doc)
    total = int((row["resolved_at"] - row["started_at"]).total_seconds() // 60)
    clocks = {hm(m.group(0)) for m in TIME_RE.finditer(text)}
    gaps = {(b - a) % 1440 for a in clocks for b in clocks}
    allowed = {total} | gaps | DURATION_WHITELIST.get(row["id"], set())
    found = DUR_RE.findall(text)
    return [f"duration {n} is not the total or a gap" for n in found if int(n) not in allowed]


def log_size_errors(events: list[dict], row: dict) -> list[str]:
    lo, hi = LOG_LINES
    errors = [] if lo <= len(events) <= hi else [f"{len(events)} log lines; need {lo}-{hi}"]
    if row["codes"] and len({e["level"] for e in events}) < 2:
        errors.append("logs need at least two levels when the row has codes")
    return errors


def heading_errors(doc: dict, others: dict[str, dict], runbooks: list[dict]) -> list[str]:
    """Title, heading sequence and section-heading sequence unique against every other
    write-up and every runbook."""
    errors = []
    seq, title = headings(doc["body"]), doc["meta"]["title"].lower()
    for name, other in others.items():
        oseq = headings(other["body"])
        if oseq == seq:
            errors.append(f"heading sequence equals {name}'s")
        if len(seq) > 1 and oseq[1:] == seq[1:]:
            errors.append(f"section headings equal {name}'s")
        if other["meta"]["title"].lower() == title:
            errors.append(f"title equals {name}'s")
    for r in runbooks:
        if headings(r["body"]) == seq or r["meta"]["title"].lower() == title:
            errors.append(f"heading sequence or title equals {r['meta']['id']}'s")
    return errors


def copied_runs(body: str, others: dict[str, str]) -> list[str]:
    """Each other text that shares a run of SHARED_RUN words with body, with one example."""
    mine = prose_ngrams(body)
    out = []
    for name, text in others.items():
        if shared := mine & prose_ngrams(text):
            out.append(f"{name}: {' '.join(sorted(shared)[0])!r}")
    return out


def jaccard_scores(body: str, others: dict[str, str]) -> list[tuple[float, str]]:
    """Word-trigram Jaccard of body against each other text, highest first."""
    mine = trigrams(body)
    return sorted(((jaccard(mine, trigrams(t)), n) for n, t in others.items()), reverse=True)
