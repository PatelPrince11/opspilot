"""Gemini label audit, by hand: no LLM calls from code.

    python eval/label_audit.py pack           # scratchpad/gemini_audit/corpus.md + batch_01..08.md
    python eval/label_audit.py import FILE    # Gemini's pasted lines -> eval/label_audit.md

Prince pastes corpus.md, then each batch, into Google AI Studio and saves the replies to FILE.
"""

import argparse
import random
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.validate_questions import QUESTIONS, SEED, Corpus, load  # noqa: E402
from scripts.corpus.common import ROOT  # noqa: E402

PACK = ROOT / "scratchpad" / "gemini_audit"
REPORT = ROOT / "eval" / "label_audit.md"
BATCH = 20
SAMPLE = 10

INSTRUCTIONS = """You are auditing an evaluation set. The corpus was pasted in an earlier message. For each question: (1) Do the listed gold docs together answer it? (2) Is each key fact stated in a gold doc? Quote the doc id. (3) Does any doc NOT listed also clearly answer it? List those ids. Use only the corpus, never outside knowledge. Output one line per question, exactly:
<qid> | OK or WRONG | missing_gold: <ids or none> | unsupported_facts: <fact numbers or none> | reason: <one short sentence>
Output nothing else."""  # noqa: E501

LINE = re.compile(
    r"^(?P<qid>Q\d{3}) \| (?P<verdict>OK|WRONG) \| missing_gold: (?P<missing>[^|]+?) \| "
    r"unsupported_facts: (?P<facts>[^|]+?) \| reason: (?P<reason>.+)$"
)
DOC_ID = re.compile(r"^(?:INC-\d{4}|RB-\d{3})$")


def pack(questions: list[dict], out: Path = PACK) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    text = Corpus().text  # the 117 written incidents and 30 runbooks, nothing else
    corpus = out / "corpus.md"
    corpus.write_text("".join(f"=== {i} ===\n{text[i].strip()}\n\n" for i in sorted(text)))
    paths = [corpus]
    for n in range(0, len(questions), BATCH):
        blocks = [INSTRUCTIONS, ""]
        for q in questions[n : n + BATCH]:
            facts = "\n".join(f"  {k}. {f}" for k, f in enumerate(q["expected_key_facts"], 1))
            blocks.append(
                f"{q['id']}\nQuestion: {q['question']}\n"
                f"Gold: {', '.join(q['relevant_doc_ids'])}\nKey facts:\n{facts}\n"
            )
        path = out / f"batch_{n // BATCH + 1:02}.md"
        path.write_text("\n".join(blocks))
        paths.append(path)
    return paths


def parse(text: str, questions: list[dict]) -> tuple[dict[str, dict], list[str], list[str]]:
    """(rows by qid, malformed lines with the reason, question ids with no line)."""
    known = {q["id"]: q for q in questions}
    rows, bad = {}, []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = LINE.match(line)
        if not m:
            bad.append(f"not in the format: {line!r}")
            continue
        row = m.groupdict()
        qid = row["qid"]
        missing = [] if row["missing"].strip().lower() == "none" else row["missing"].split(",")
        missing = [x.strip() for x in missing]
        facts = [] if row["facts"].strip().lower() == "none" else row["facts"].split(",")
        facts = [x.strip() for x in facts]
        n_facts = len(known[qid]["expected_key_facts"]) if qid in known else 0
        if qid not in known:
            bad.append(f"unknown question id: {line!r}")
        elif qid in rows:
            bad.append(f"duplicate line for {qid}: {line!r}")
        elif not all(DOC_ID.match(x) for x in missing):
            bad.append(f"missing_gold is not 'none' or doc ids: {line!r}")
        elif not all(x.isdigit() and 1 <= int(x) <= n_facts for x in facts):
            bad.append(f"unsupported_facts is not 'none' or fact numbers 1..{n_facts}: {line!r}")
        else:
            rows[qid] = row | {"missing": missing, "facts": [int(x) for x in facts]}
    absent = [q["id"] for q in questions if q["id"] not in rows]
    return rows, bad, absent


def flagged(row: dict) -> bool:
    return row["verdict"] == "WRONG" or bool(row["missing"]) or bool(row["facts"])


def report(questions: list[dict], rows: dict[str, dict], absent: list[str]) -> str:
    by = {q["id"]: q for q in questions}
    flags = [i for i in rows if flagged(rows[i])]
    ok = sorted(i for i in rows if not flagged(rows[i]))
    sample = sorted(random.Random(SEED).sample(ok, min(SAMPLE, len(ok))))

    def block(qid: str) -> str:
        q, r = by[qid], rows[qid]
        facts = "\n".join(f"{k}. {f}" for k, f in enumerate(q["expected_key_facts"], 1))
        return (
            f"## {qid} ({q['difficulty']}): Gemini {r['verdict']}\n\n"
            f"**Question:** {q['question']}\n\n**Gold:** {', '.join(q['relevant_doc_ids'])}\n\n"
            f"**Key facts:**\n{facts}\n\n"
            f"**Gemini:** missing_gold: {', '.join(r['missing']) or 'none'} | "
            f"unsupported_facts: {', '.join(map(str, r['facts'])) or 'none'} | "
            f"reason: {r['reason']}\n\n**Prince:** AGREE/DISAGREE, note:\n"
        )

    parts = [
        "# Label audit (Gemini, pasted by hand)\n",
        f"Total questions: {len(questions)}. Gemini lines: {len(rows)}. "
        f"Flagged: {len(flags)}. Sampled OK: {len(sample)} (seed {SEED}).\n",
    ]
    if absent:
        parts.append(f"No Gemini line for: {', '.join(absent)}\n")
    parts.append("# Flagged by Gemini\n")
    parts += [block(i) for i in sorted(flags)] or ["None.\n"]
    parts.append("# Random sample of questions Gemini marked OK\n")
    parts += [block(i) for i in sample] or ["None.\n"]
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("pack")
    imp = sub.add_parser("import")
    imp.add_argument("file", type=Path)
    args = ap.parse_args()
    questions = [q for q in load(QUESTIONS) if q["relevant_doc_ids"]]
    if args.cmd == "pack":
        for p in pack(questions):
            print(p.relative_to(ROOT))
        return 0
    rows, bad, absent = parse(args.file.read_text(), questions)
    for b in bad:
        print(f"malformed: {b}")
    if bad:
        print("FAIL: fix or re-run those lines; nothing written")
        return 1
    if absent:
        print(f"missing question ids ({len(absent)}): {', '.join(absent)}")
    REPORT.write_text(report(questions, rows, absent))
    print(f"wrote {REPORT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
