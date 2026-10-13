"""Writer packet for one skeleton row: everything a writer may use, in one file.

    make packet ID=INC-NNNN   ->  scratchpad/packets/INC-NNNN.md (gitignored)

Contents:
- the skeleton row and its sidecar entry
- the NARRATIVE dates of its runbooks (post-dated ones are labels only, never named)
- Fictional-facts blocks for its service and symptom services. A block whose header names one
  goes in whole; otherwise only its matching bullets or table rows. Platform-wide facts always
  go in.
- the "Skeleton rules" sections in full
- the past-events table rows that mention its services
- for a VM service or a bad_deploy row: the "Rollout tool for VM config" block in full, plus
  every NOTES line about the rollout tool (e.g. F4, F5)
- every NOTES line that mentions the id (as INC-NNNN or bare NNNN)
- every NOTES line or table row that names one of its hosts, codes, services, or a systemd
  unit of its services
- scripts/corpus/review_checklist.md (judgement-only review rules)
- the linked runbooks in full
"""

import re
import sys
import tomllib

from scripts.corpus.checks import load_sidecar
from scripts.corpus.common import DATA, ROOT
from scripts.corpus.incidents import NARRATIVE, load_skeleton, postdated

PACKETS = ROOT / "scratchpad" / "packets"
CHECKLIST = ROOT / "scripts" / "corpus" / "review_checklist.md"
FACTS_HEADING = "## Fictional facts"
PAST_EVENTS = "**Past events mentioned in runbooks"
ROLLOUT_BLOCK = "**Rollout tool for VM config**"
ROLLOUT_RE = re.compile(r"rollout[- ]tool", re.I)
POSTGRES = ("payments-db", "ledger-db", "orders-db", "auth-db", "inventory-db", "catalog-db")
REDIS = ("redis-cache", "redis-sessions", "redis-features")
# systemd units of each service (NOTES Fictional facts and the RB-027 probe table)
UNITS = {
    "internal-dns": ["coredns"],
    "kafka": ["kafka"],
    "search-cluster": ["opensearch"],
    "payments-pgbouncer": ["pgbouncer"],
    **{s: ["postgresql@16-main"] for s in POSTGRES},
    **{s: ["redis-server", "redis-sentinel"] for s in REDIS},
}
# Datastores run on VMs, and NOTES (batch 2, Assumption A) puts these two apps on VMs too.
VM_APPS = {"payments-pgbouncer", "internal-dns"}


def fact_blocks(notes: str) -> list[list[str]]:
    """The Fictional-facts section split at its bold headers (lines starting with **)."""
    lines = notes.split("\n")
    start = next(i for i, ln in enumerate(lines) if ln.startswith(FACTS_HEADING))
    end = next(i for i in range(start + 1, len(lines)) if lines[i].startswith("## "))
    blocks: list[list[str]] = []
    for ln in lines[start + 1 : end]:
        if ln.startswith("**") or not blocks:
            blocks.append([])
        blocks[-1].append(ln)
    return blocks


def units(block: list[str]) -> list[list[str]]:
    """A top-level bullet with its indented continuation, a table row, or a plain line."""
    out: list[list[str]] = []
    for ln in block[1:]:
        if ln.startswith((" ", "\t")) and out:
            out[-1].append(ln)
        else:
            out.append([ln])
    return out


def term_re(terms: list[str], ignore_case: bool) -> re.Pattern | None:
    """Whole-token match: not part of a longer name, host or code."""
    if not terms:
        return None
    alts = "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True))
    return re.compile(rf"(?<![\w-])(?:{alts})(?![\w-])", re.I if ignore_case else 0)


def mentions(text: str, services: list[str]) -> bool:
    pat = term_re(services, ignore_case=True)
    return bool(pat and pat.search(text))


def facts_for(notes: str, services: list[str]) -> str:
    parts = []
    for block in fact_blocks(notes):
        head = block[0]
        if head.startswith(PAST_EVENTS) or head.startswith("**Skeleton rules"):
            continue  # sections of their own below
        if head.startswith("**Platform-wide") or mentions(head, services):
            parts.append("\n".join(block).strip())
            continue
        hits = ["\n".join(u) for u in units(block) if mentions("\n".join(u), services)]
        if hits:
            parts.append("\n".join([head, "(matching lines only)", *hits]).strip())
    return "\n\n".join(parts)


def blocks_starting(notes: str, prefix: str) -> list[str]:
    return ["\n".join(b).strip() for b in fact_blocks(notes) if b[0].startswith(prefix)]


def past_event_rows(notes: str, services: list[str]) -> list[str]:
    rows = []
    for block in fact_blocks(notes):
        if block[0].startswith(PAST_EVENTS):
            rows += [ln for ln in block if ln.startswith("| ") and mentions(ln, services)]
    return rows


def numbered(notes: str, keep) -> list[str]:
    return [f"NOTES.md:{n}: {ln}" for n, ln in enumerate(notes.split("\n"), 1) if keep(ln)]


def id_lines(notes: str, inc_id: str) -> list[str]:
    num = inc_id.split("-")[1]
    pat = re.compile(rf"(?<![\w-])(?:INC-)?{num}(?!\w)")
    return numbered(notes, pat.search)


def term_lines(notes: str, row: dict, services: list[str]) -> list[str]:
    """Lines naming a row host (full or short), code, service, or a unit of its services."""
    hosts = row["hosts"] + [h.split(".")[0] for h in row["hosts"]]
    words = services + [u for s in services for u in UNITS.get(s, [])]
    loose = term_re(hosts + words, ignore_case=True)
    codes = term_re(row["codes"], ignore_case=False)  # catalog codes are case-sensitive
    return numbered(notes, lambda ln: loose.search(ln) or (codes and codes.search(ln)))


def is_vm_row(row: dict, kinds: dict) -> bool:
    services = [row["service"], *row["symptom_services"]]
    return any(kinds[s] == "datastore" or s in VM_APPS for s in services)


def build(inc_id: str) -> str:
    rows = {r["id"]: r for r in load_skeleton(DATA / "incidents" / "skeleton.toml")}
    if inc_id not in rows:
        raise SystemExit(f"{inc_id}: not a skeleton row")
    row = rows[inc_id]
    side = load_sidecar()[inc_id]
    notes = (ROOT / "NOTES.md").read_text()
    with (DATA / "services.toml").open("rb") as f:
        kinds = {s["name"]: s["kind"] for s in tomllib.load(f)["services"]}
    services = [row["service"], *row["symptom_services"]]
    late = postdated(row)

    out = [f"# Writer packet: {inc_id}", "", "## Skeleton row", "", "```"]
    out += [
        f"{k} = {v.isoformat() if hasattr(v, 'isoformat') else repr(v)}" for k, v in row.items()
    ]
    out += ["```", "", "## Sidecar entry (eval only: never name its labels)", "", "```"]
    out += [f"{k} = {v!r}" for k, v in side.items()]
    out += ["```", "", "## Runbook dates", ""]
    for rb in row["runbook_ids"]:
        if rb in late:
            out.append(
                f"- {rb}: written about this incident ({NARRATIVE[rb]}); label only, never name it"
            )
        elif rb in NARRATIVE:
            out.append(
                f"- {rb}: NARRATIVE date {NARRATIVE[rb]}, before this incident; may be named"
            )
        else:
            out.append(f"- {rb}: not in NARRATIVE (existed before any incident); may be named")
    if not row["runbook_ids"]:
        out.append("- none linked")
    out += ["", f"## Fictional facts for {', '.join(services)}", "", facts_for(notes, services)]
    out += ["", "## Skeleton rules (in full)", "", *blocks_starting(notes, "**Skeleton rules")]
    out += ["", f"## Past-events rows for {', '.join(services)}", ""]
    out += past_event_rows(notes, services) or ["(none)"]
    if is_vm_row(row, kinds) or "bad_deploy" in row["failure_types"]:
        out += ["", "## Rollout tool (VM service or bad_deploy row)", ""]
        out += blocks_starting(notes, ROLLOUT_BLOCK)
        out += ["", "Every NOTES line about the rollout tool:", ""]
        out += numbered(notes, ROLLOUT_RE.search)
    out += ["", f"## NOTES lines that mention {inc_id}", ""]
    out += id_lines(notes, inc_id) or ["(none)"]
    out += ["", "## NOTES lines naming the row's hosts, codes, services or their units", ""]
    out += term_lines(notes, row, services) or ["(none)"]
    out += [
        "",
        "## Review checklist (scripts/corpus/review_checklist.md)",
        "",
        CHECKLIST.read_text(),
    ]
    for rb in row["runbook_ids"]:
        out += ["", f"## Runbook {rb} (full)", "", (DATA / "runbooks" / f"{rb}.md").read_text()]
    return "\n".join(out) + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m scripts.corpus.packet INC-NNNN", file=sys.stderr)
        return 2
    PACKETS.mkdir(parents=True, exist_ok=True)
    path = PACKETS / f"{argv[0]}.md"
    path.write_text(build(argv[0]))
    print(path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
