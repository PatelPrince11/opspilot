"""Batch progress file: scratchpad/batch-N/progress.toml (gitignored).

    batch = 7

    [rows."INC-0042"]
    state = "selfchecked"
    note = "optional free text"

States, in order: drafted, selfchecked, in_data, reviewed, fixed, gated.
- drafted: a draft exists in the scratchpad
- selfchecked: `make selfcheck` passes on the draft
- in_data: copied into data/incidents/, and `make corpus` / `make test` pass
- reviewed: reviewer findings recorded
- fixed: findings fixed, or recorded as open
- gated: the full gate passed and the row is committed

Resume rule: read this file before anything else. A row that isn't listed hasn't started. A
row at "drafted" may be redrafted from scratch. A row past "drafted" is never redone: it
continues from its state to the next one, and set_state refuses to move it backwards.
"""

import tomllib
from pathlib import Path

STATES = ("drafted", "selfchecked", "in_data", "reviewed", "fixed", "gated")


def load(path: Path) -> dict:
    """{"batch": N, "rows": {id: {"state": ..., "note": ...}}}, validated."""
    with path.open("rb") as f:
        doc = tomllib.load(f)
    if not isinstance(doc.get("batch"), int):
        raise ValueError(f"{path}: batch must be an integer")
    for inc_id, row in doc.get("rows", {}).items():
        if row.get("state") not in STATES:
            raise ValueError(f"{path}: {inc_id}: state {row.get('state')!r} not in {STATES}")
    doc.setdefault("rows", {})
    return doc


def next_step(progress: dict, inc_id: str) -> str | None:
    """The state this row should reach next; None once it is gated."""
    state = progress["rows"].get(inc_id, {}).get("state")
    if state is None:
        return "drafted"
    i = STATES.index(state)
    return STATES[i + 1] if i + 1 < len(STATES) else None


def may_redraft(progress: dict, inc_id: str) -> bool:
    return progress["rows"].get(inc_id, {}).get("state") in (None, "drafted")


def set_state(progress: dict, inc_id: str, state: str, note: str = "") -> None:
    if state not in STATES:
        raise ValueError(f"{state!r} not in {STATES}")
    old = progress["rows"].get(inc_id, {}).get("state")
    if old is not None and STATES.index(old) > 0 and STATES.index(state) < STATES.index(old):
        raise ValueError(f"{inc_id}: past 'drafted' at {old!r}; never moved back to {state!r}")
    progress["rows"][inc_id] = {"state": state, **({"note": note} if note else {})}


def dumps(progress: dict) -> str:
    out = [f"batch = {progress['batch']}"]
    for inc_id in sorted(progress["rows"]):
        row = progress["rows"][inc_id]
        out += ["", f'[rows."{inc_id}"]', f'state = "{row["state"]}"']
        if row.get("note"):
            note = row["note"].replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'note = "{note}"')
    return "\n".join(out) + "\n"


def save(path: Path, progress: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps(progress))
