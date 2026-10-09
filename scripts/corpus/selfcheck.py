"""Every deterministic check on one draft write-up, one PASS/FAIL line per check.

    make selfcheck ID=INC-NNNN [FILE=path/to/INC-NNNN.md]

FILE defaults to data/incidents/INC-NNNN.md; the log spec is read from logs/INC-NNNN.toml
next to it. The draft is compared with every runbook and every committed write-up except
its own id. All rules come from the build (scripts/corpus/incidents.py, synthetic_logs.py)
and scripts/corpus/checks.py; nothing is reimplemented here. Exit status 1 on any FAIL.
"""

import sys
from pathlib import Path

from scripts.corpus import checks, synthetic_logs
from scripts.corpus.common import DATA
from scripts.corpus.incidents import (
    DOC_CHECKS,
    code_finder,
    doc_text,
    document_errors,
    front_matter_errors,
    load_documents,
    load_skeleton,
    short_host_re,
)
from scripts.corpus.runbooks import load_runbooks, parse_runbook
from scripts.corpus.services import CorpusError, load_services


def run(inc_id: str, path: Path | None = None) -> tuple[list[tuple[str, list[str]]], list[str]]:
    """([(check name, errors)], info lines) for the draft at path; no errors means PASS."""
    path = path or DATA / "incidents" / f"{inc_id}.md"
    services = load_services(DATA / "services.toml")
    rows = {r["id"]: r for r in load_skeleton(DATA / "incidents" / "skeleton.toml")}
    runbooks = load_runbooks(DATA / "runbooks")
    others = {d["meta"]["id"]: d for d in load_documents(DATA / "incidents")}
    others.pop(inc_id, None)
    side = checks.load_sidecar()

    doc = parse_runbook(path)
    fm = front_matter_errors(doc, rows)
    if not fm and doc["meta"]["id"] != inc_id:
        fm = [f"front matter id {doc['meta']['id']} is not {inc_id}"]
    if fm:
        return [("front matter", fm)], []
    row, text = rows[inc_id], doc_text(doc)

    groups = document_errors(doc, row, code_finder(services), short_host_re(services))
    results = [(name, groups.get(name, [])) for name in DOC_CHECKS]

    spec_path = path.parent / "logs" / f"{inc_id}.toml"
    log_errors, logtext = [], ""
    try:
        spec = synthetic_logs.load_spec(spec_path)
        events = synthetic_logs.generate(inc_id, spec, row["started_at"].date())
        synthetic_logs.validate_logs(events, row, services)
        log_errors = checks.log_size_errors(events, row)
        logtext = "\n".join(e["message"] for e in events)
    except (OSError, ValueError, KeyError, CorpusError) as e:
        log_errors = [f"{spec_path}: {e}"]
    results.append(("log spec", log_errors))

    results.append(("sidecar facts", checks.fact_errors(text, side[inc_id], logtext)))
    results.append(
        ("sidecar labels", checks.label_errors(text, checks.sidecar_labels(side, runbooks)))
    )
    leak = checks.leaks_demo(text) or (logtext and checks.leaks_demo(logtext))
    results.append(("demo leak", ["leaks the demo combination"] if leak else []))
    results.append(("word count", checks.word_count_errors(doc)))
    results.append(("durations", checks.duration_errors(doc, row)))
    results.append(("headings", checks.heading_errors(doc, others, runbooks)))

    texts = {r["meta"]["id"]: r["body"] for r in runbooks}
    texts |= {k: v["body"] for k, v in others.items()}
    results.append(("8-word runs", checks.copied_runs(doc["body"], texts)))
    top = checks.jaccard_scores(doc["body"], texts)[:3]
    over = [f"{n} {j:.4f}" for j, n in top if j >= checks.JACCARD_TARGET]
    results.append(
        ("jaccard", [f"at or over {checks.JACCARD_TARGET}: {', '.join(over)}"] if over else [])
    )
    return results, [f"jaccard top 3: {', '.join(f'{n} {j:.4f}' for j, n in top)}"]


def main(argv: list[str]) -> int:
    if not 1 <= len(argv) <= 2:
        print("usage: python -m scripts.corpus.selfcheck INC-NNNN [path]", file=sys.stderr)
        return 2
    inc_id = argv[0]
    path = Path(argv[1]) if len(argv) == 2 and argv[1] else None
    results, info = run(inc_id, path)
    for name, errors in results:
        print(
            f"{'FAIL' if errors else 'PASS'}  {name}" + (f": {'; '.join(errors)}" if errors else "")
        )
    for line in info:
        print(f"INFO  {line}")
    return 1 if any(errors for _, errors in results) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
