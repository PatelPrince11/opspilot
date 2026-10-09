"""Writer packet for one skeleton row: everything a writer may use, in one file.

    make packet ID=INC-NNNN   ->  scratchpad/packets/INC-NNNN.md (gitignored)

Contents:
- the skeleton row and its sidecar entry
- the NARRATIVE dates of its runbooks (post-dated ones are labels only, never named)
- the NOTES Fictional-facts parts that mention the row's service or symptom services. A block
  whose header names one goes in whole. Otherwise only its matching units go in: a top-level
  bullet with its sub-bullets, or a table row. Platform-wide facts always go in.
- every NOTES line that mentions the id (as INC-NNNN or bare NNNN)
- the linked runbooks in full
"""

import re
import sys

from scripts.corpus.checks import load_sidecar
from scripts.corpus.common import DATA, ROOT
from scripts.corpus.incidents import NARRATIVE, load_skeleton, postdated

PACKETS = ROOT / "scratchpad" / "packets"
FACTS_HEADING = "## Fictional facts"


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


def mentions(text: str, services: list[str]) -> bool:
    return any(re.search(rf"(?<![\w-]){re.escape(s)}(?![\w-])", text, re.I) for s in services)


def facts_for(notes: str, services: list[str]) -> str:
    parts = []
    for block in fact_blocks(notes):
        head = block[0]
        if head.startswith("**Platform-wide") or mentions(head, services):
            parts.append("\n".join(block).strip())
            continue
        hits = ["\n".join(u) for u in units(block) if mentions("\n".join(u), services)]
        if hits:
            parts.append("\n".join([head, "(matching lines only)", *hits]).strip())
    return "\n\n".join(parts)


def notes_lines(notes: str, inc_id: str) -> list[str]:
    num = inc_id.split("-")[1]
    pat = re.compile(rf"(?<![\w-])(?:INC-)?{num}(?!\w)")
    return [f"NOTES.md:{n}: {ln}" for n, ln in enumerate(notes.split("\n"), 1) if pat.search(ln)]


def build(inc_id: str) -> str:
    rows = {r["id"]: r for r in load_skeleton(DATA / "incidents" / "skeleton.toml")}
    if inc_id not in rows:
        raise SystemExit(f"{inc_id}: not a skeleton row")
    row = rows[inc_id]
    side = load_sidecar()[inc_id]
    notes = (ROOT / "NOTES.md").read_text()
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
    out += ["", f"## NOTES lines that mention {inc_id}", ""]
    out += notes_lines(notes, inc_id) or ["(none)"]
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
