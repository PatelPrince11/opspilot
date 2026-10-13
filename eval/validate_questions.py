"""Checks eval/questions.jsonl against the corpus and docs/eval_prep.md.

    python eval/validate_questions.py              # partial: every line present must pass
    python eval/validate_questions.py --final      # also: 200 lines, the author mix, the split
    python eval/validate_questions.py --write-split  # fill `split` (fixed seed), then check --final
    python eval/validate_questions.py --import-human FILE --author prince|gemini  # 40 lines

Lines imported for hand labelling (empty relevant_doc_ids) skip the label checks in partial
mode and fail --final.
"""

import argparse
import json
import random
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.corpus.common import DATA, PROCESSED, ROOT, SEED  # noqa: E402
from scripts.corpus.incidents import code_finder  # noqa: E402
from scripts.corpus.services import load_services  # noqa: E402

QUESTIONS = ROOT / "eval" / "questions.jsonl"
FIELDS = {
    "id",
    "question",
    "relevant_doc_ids",
    "expected_key_facts",
    "difficulty",
    "author",
    "split",
}
# Optional: false_premise (bool) marks a question that assumes something the docs contradict; a
# key fact corrects it. unrecorded (list) names parts the corpus doesn't answer, each matched by a
# key fact "not recorded: <part>".
OPTIONAL = {"false_premise", "unrecorded"}
NOT_RECORDED = "not recorded: "
DIFFICULTIES = {"exact-token", "paraphrase", "multi-doc"}
AUTHORS = {"claude", "prince", "gemini"}
HUMAN = {"prince", "gemini"}

# docs/eval_prep.md (a): twin groups; every pair inside a group is a twin pair.
TWINS = [
    "0001 0011 0079 0089 0109 0127 0149 0183",
    "0025 0142",
    "0007 0023",
    "0125 0016",
    "0125 0025",
    "0125 0142",
    "0142 0016",
    "0162 0048",
    "0177 0048",
    "0162 0177",
    "0162 0197",
    "0177 0197",
    "0156 0055",
    "0059 0151",
    "0070 0089",
    "0070 0149",
    "0070 0120",
    "0070 0101",
    "0120 0089",
    "0120 0149",
    "0078 0152",
    "0084 0157 0174",
    "0087 0099",
    "0108 0094",
    "0128 0133",
    "0081 0033 0038 0025 0142",
    "0068 0086 0061 0007 0023",
    "0110 0004",
    "0073 0031",
    "0186 0195 0048 0162 0177 0197",
    "0167 0137",
    "0119 0060",
    "0155 0152 0078 0123",
    "0095 0055 0156",
    "0077 0124",
    "0022 0015",
    "0145 0107",
    "0043 0086",
    "0082 0007 0061",
    "0126 0073",
    "0035 0156 0055 0095",
    "0083 0035",
    "0136 0077 0124",
    "0182 0134",
    "0169 0184 0155 0152",
    "0146 0014 0028",
    "0187 0107",
    "0159 0048 0162 0177 0186 0195 0197",
    "0179 0196",
    "0076 0025 0033 0038 0081 0125 0142",
    "0054 0108 0094",
    "0165 0093",
    "0006 0133",
    "0037 0166",
    "0069 0039",
]

# (b): write-ups that retell a runbook's history; a gold write-up brings its runbook.
RETELLINGS = (
    "0185/RB-027 0168/RB-021 0055/RB-011 0023/RB-025 0016/RB-022 0024/RB-002 0031/RB-024 "
    "0178/RB-026 0197/RB-029 0004/RB-023 0042/RB-028 0059/RB-015 0070/RB-008 0078/RB-019 "
    "0084/RB-007 0157/RB-007 0174/RB-007 0087/RB-018 0093/RB-001 0108/RB-017 0120/RB-009 "
    "0124/RB-013 0128/RB-005 0133/RB-005 0137/RB-030 0142/RB-020 0151/RB-012 0162/RB-016 "
    "0101/RB-008"
)

# (d): no question may ask what fixed these.
NO_FIX = (
    "0060 0107 0125 0128 "
    "0005 0022 0028 0033 0038 0046 0049 0061 0065 0068 0073 0074 0077 0081 0086 0091 0095 0110 "
    "0119 0123 0145 0155 0167 0186 0195 "
    "0006 0008 0014 0035 0037 0039 0043 0054 0069 0076 0082 0083 0088 0112 0117 0126 0136 0146 "
    "0159 0165 0166 0169 0179 0182 0184 0187 0196"
)
FIX_WORDS = re.compile(
    r"\b(fix\w*|remediat\w*|mitigat\w*"
    r"|(?<!name )(?<!dns )(?:resolv\w*|resolution)(?! (?:failures?|errors?|issues?|problems?))"
    r"|what (?:stopped|ended|cleared)|how (?:did|do) (?:they|we|you) (?:stop|end|clear|get))\b",
    re.I,
)

# "name/DNS resolution" and "resolution failure" are lookups, not fixes.
# (g): narrative colour. Every row: owners, on-call, follow-ups, reporters. 0025: its page,
# check and diff times.
COLOUR = re.compile(
    r"\bteam-[a-z]+\b|\bon-?call\b|\bfollow-?ups?\b|\breported by\b|\bowner\b", re.I
)
COLOUR_BY_ID = {"INC-0025": re.compile(r"15:26|15:31|15:15|\bdiff\b", re.I)}

# (f): tokens a question may not use when the row is gold.
KEPT_OUT = {"INC-0142": re.compile(r"inventory-db-01", re.I)}

WORD = re.compile(r"[a-z0-9]+(?:['._/:-][a-z0-9]+)*")
MONTHS = "january february march april may june july august september october november december"


def inc(n: str) -> str:
    return f"INC-{n}"


def twin_partners() -> dict[str, set[str]]:
    partners: dict[str, set[str]] = {}
    for group in TWINS:
        ids = {inc(n) for n in group.split()}
        for i in ids:
            partners.setdefault(i, set()).update(ids - {i})
    return partners


def retelling_pairs() -> list[tuple[str, str]]:
    return [(inc(p.split("/")[0]), p.split("/")[1]) for p in RETELLINGS.split()]


def has(text: str, token: str) -> bool:
    """Whole-token match: `edge-gateway` is not found inside `edge-gateway-03`."""
    return re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", text, re.I) is not None


def date_tokens(started_at: str) -> set[str]:
    dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    month = MONTHS.split()[dt.month - 1]
    names = {month, month[:3]} | ({"sept"} if month == "september" else set())
    out = {dt.strftime("%Y-%m-%d"), dt.strftime("%Y-%m"), str(dt.year)}
    out |= names - {"may"}  # a bare month names a date; a bare "may" is just a word
    for m in names:
        out |= {f"{m} {dt.year}", f"{m} {dt.day}", f"{dt.day} {m}"}
        out |= {f"{m} {dt.day}{s}" for s in ("st", "nd", "rd", "th")}
    return out


def doc_tokens(doc: dict) -> set[str]:
    """What can tell one incident from its twin: hosts (full and short), services, codes,
    date forms."""
    hosts = set(doc["hosts"]) | {h.split(".")[0] for h in doc["hosts"]}
    return (
        hosts
        | {doc["service"], *doc["symptom_services"]}
        | set(doc["codes"])
        | date_tokens(doc["started_at"])
    )


# A service may be named loosely: "catalog API" for catalog-api, "notification processor"
# for notification-worker.
SUFFIX_ALIASES = {"worker": ("processor",)}


def spellings(token: str) -> set[str]:
    head, _, last = token.rpartition("-")
    forms = {token} | {f"{head}-{a}" for a in SUFFIX_ALIASES.get(last, ()) if head}
    return forms | {f.replace("-", " ") for f in forms}


def doc_text(doc: dict) -> str:
    fields = ("title", "symptoms", "root_cause", "resolution", "body")
    return "\n".join(doc.get(k) or "" for k in fields)


def shingles(text: str, n: int = 5) -> set[tuple[str, ...]]:
    words = WORD.findall(text.lower())
    return {tuple(words[i : i + n]) for i in range(len(words) - n + 1)}


class Corpus:
    def __init__(self):
        services = load_services(DATA / "services.toml")
        self.codes = code_finder(services)
        full = {h["name"] for s in services["services"] for h in s["hosts"]}
        self.hostnames = full | {h.split(".")[0] for h in full}
        self.docs = {}
        for name in ("incidents.jsonl", "runbooks.jsonl"):
            for line in (PROCESSED / name).read_text().splitlines():
                d = json.loads(line)
                self.docs[d["id"]] = d
        self.text = {k: doc_text(d) for k, d in self.docs.items()}
        self.grams = {k: shingles(t) for k, t in self.text.items()}
        self.partners = twin_partners()
        self.retell = retelling_pairs()
        self.no_fix = {inc(n) for n in NO_FIX.split()}

    def exact_tokens(self, question: str) -> set[str]:
        found = self.codes(question)[0]
        return found | {h for h in self.hostnames if has(question, h)}


def labelled(q: dict) -> bool:
    return bool(q.get("relevant_doc_ids"))


def check_question(q: dict, c: Corpus) -> list[str]:
    errs = []
    if not FIELDS <= set(q) <= FIELDS | OPTIONAL:
        return [f"fields {sorted((set(q) ^ FIELDS) - OPTIONAL)} wrong"]
    if q["author"] not in AUTHORS:
        errs.append(f"author {q['author']!r}")
    if q["split"] not in ("dev", "test", None):
        errs.append(f"split {q['split']!r}")
    if not q["question"].strip():
        errs.append("empty question")
    if not labelled(q):
        return errs
    text, gold = q["question"], q["relevant_doc_ids"]
    if q["difficulty"] not in DIFFICULTIES:
        errs.append(f"difficulty {q['difficulty']!r}")
    if not q["expected_key_facts"]:
        errs.append("no key facts")
    if len(set(gold)) != len(gold):
        errs.append("duplicate gold id")
    unknown = [g for g in gold if g not in c.docs]
    if unknown:
        return errs + [f"not a written incident or runbook: {unknown}"]

    if q["difficulty"] == "exact-token":
        toks = c.exact_tokens(text)
        if not any(has(c.text[g], t) for t in toks for g in gold):
            errs.append(f"exact-token but no code/host from a gold doc (found {sorted(toks)})")
    if q["difficulty"] == "multi-doc" and len(gold) < 2:
        errs.append("multi-doc with fewer than 2 gold docs")

    mine = shingles(text)
    for g in gold:
        shared = mine & c.grams[g]
        if shared:
            errs.append(f"shares a 5-word run with {g}: {' '.join(sorted(shared)[0])!r}")

    for g in gold:
        for p in sorted(c.partners.get(g, set()) - set(gold)):
            dist = doc_tokens(c.docs[g]) - doc_tokens(c.docs[p])
            if not any(has(text, f) for t in dist for f in spellings(t)):
                errs.append(f"{g} is a twin of {p}: add both or name a host, date or service")

    # One way: the runbook tells the write-up's events, so it answers whatever the write-up
    # answers. A runbook-only question (its procedure) need not list the write-up.
    for i, rb in c.retell:
        if i in gold and rb not in gold:
            errs.append(f"{i} retells {rb}: list both")

    unrecorded = q.get("unrecorded", [])
    if not isinstance(unrecorded, list) or not all(isinstance(u, str) and u for u in unrecorded):
        errs.append("unrecorded must be a list of non-empty strings")
        unrecorded = []
    facts = q["expected_key_facts"]
    for u in unrecorded:
        if f"{NOT_RECORDED}{u}" not in facts:
            errs.append(f"unrecorded {u!r} needs the key fact {NOT_RECORDED + u!r}")
    if "false_premise" in q:
        if not isinstance(q["false_premise"], bool):
            errs.append("false_premise must be true or false")
        elif q["false_premise"] and all(f.startswith(NOT_RECORDED) for f in facts):
            errs.append("false_premise needs a key fact that corrects the premise")

    # (d) passes for a no-fix row only when the fix is marked unrecorded.
    fix_unrecorded = any(FIX_WORDS.search(u) for u in unrecorded)
    if FIX_WORDS.search(text) and set(gold) & c.no_fix and not fix_unrecorded:
        errs.append(f"asks for a fix of {sorted(set(gold) & c.no_fix)} (eval_prep (d))")

    for fact in q["expected_key_facts"]:
        if COLOUR.search(fact) or any(
            COLOUR_BY_ID[g].search(fact) for g in gold if g in COLOUR_BY_ID
        ):
            errs.append(f"key fact is narrative colour: {fact!r}")
    for g in gold:
        if g in KEPT_OUT and KEPT_OUT[g].search(text):
            errs.append(f"uses a token kept out of {g} (eval_prep (f))")
    return errs


SPLIT_DIMS = {
    "difficulty": lambda q: q["difficulty"],
    "author": lambda q: q["author"],
    "gold bucket": lambda q: "2+" if len(q["relevant_doc_ids"]) > 1 else "1",
    "false_premise": lambda q: bool(q.get("false_premise")),
    "unrecorded": lambda q: bool(q.get("unrecorded")),
}


def split_counts(questions: list[dict], out: dict[str, str]) -> dict[str, Counter]:
    """{dimension: Counter((value, split))} for the five balanced dimensions."""
    return {
        name: Counter((f(q), out[q["id"]]) for q in questions) for name, f in SPLIT_DIMS.items()
    }


def split(questions: list[dict]) -> dict[str, str]:
    """100/100 with the fixed seed, stratified by all five SPLIT_DIMS together. Each stratum
    alternates dev/test, so it is within one of even. An odd stratum's extra question goes to
    a seeded random side, redrawn until the totals are exact and every value of every
    dimension is within one between dev and test."""
    strata: dict[tuple, list[str]] = {}
    for q in sorted(questions, key=lambda q: q["id"]):
        key = tuple(str(f(q)) for f in SPLIT_DIMS.values())
        strata.setdefault(key, []).append(q["id"])
    rng = random.Random(SEED)
    for key in sorted(strata):
        rng.shuffle(strata[key])
    half = len(questions) // 2
    for _ in range(100_000):
        out = {}
        for key in sorted(strata):
            first = rng.randrange(2)
            for n, i in enumerate(strata[key]):
                out[i] = ("dev", "test")[(first + n) % 2]
        if sum(v == "dev" for v in out.values()) != half:
            continue
        counts = split_counts(questions, out)
        values = {(name, v) for name, c in counts.items() for v, _ in c}
        if all(abs(counts[n][(v, "dev")] - counts[n][(v, "test")]) <= 1 for n, v in values):
            return out
    raise SystemExit("no balanced split found")


def check_all(questions: list[dict], c: Corpus, final: bool = False) -> list[str]:
    errs = []
    for q in questions:
        errs += [f"{q.get('id')}: {e}" for e in check_question(q, c)]
    ids = Counter(q.get("id") for q in questions)
    errs += [f"{i}: duplicate id" for i, n in ids.items() if n > 1]
    texts = Counter(q.get("question", "").strip().lower() for q in questions)
    errs += [f"duplicate question: {t!r}" for t, n in texts.items() if n > 1]
    if not final:
        return errs

    human = [q for q in questions if q.get("author") in HUMAN]
    exact = [q for q in questions if q.get("difficulty") == "exact-token"]
    if len(questions) != 200:
        errs.append(f"final: {len(questions)} questions, need 200")
    if len(human) != 40:
        errs.append(f"final: {len(human)} by prince or gemini, need 40")
    if len(exact) < 0.3 * len(questions):
        errs.append(f"final: {len(exact)} exact-token, need 30% ({len(questions)} questions)")
    if sum(q.get("difficulty") == "paraphrase" for q in human) < 20:
        errs.append("final: fewer than 20 paraphrase among the 40 human questions")
    unlabelled = [q["id"] for q in questions if not labelled(q)]
    if unlabelled:
        errs.append(f"final: unlabelled {unlabelled}")
    elif not errs:
        want = split(questions)
        wrong = [q["id"] for q in questions if q["split"] != want[q["id"]]]
        if wrong:
            errs.append(f"final: split differs from the seeded split for {len(wrong)} questions")
    return errs


def load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def import_human(path: Path, author: str, into: Path) -> list[dict]:
    """Append 40 "N<tab>question" lines as unlabelled questions H001..H040 (labels later)."""
    if author not in HUMAN:
        raise SystemExit(f"AUTHOR must be prince or gemini, not {author!r}")
    existing = load(into) if into.exists() else []
    if any(q["author"] in HUMAN for q in existing):
        raise SystemExit(f"{into} already has human questions; not importing twice")
    rows = [line.split("\t", 1) for line in path.read_text().splitlines() if line.strip()]
    if len(rows) != 40 or [r[0].strip() for r in rows] != [str(n) for n in range(1, 41)]:
        raise SystemExit(f"{path}: need exactly 40 lines numbered 1..40 as N<tab>question")
    if any(len(r) != 2 or not r[1].strip() for r in rows):
        raise SystemExit(f"{path}: every line needs N<tab>question")
    new = [
        {
            "id": f"H{int(n):03}",
            "question": text.strip(),
            "relevant_doc_ids": [],
            "expected_key_facts": [],
            "difficulty": None,
            "author": author,
            "split": None,
        }
        for n, text in rows
    ]
    with into.open("a") as f:
        f.writelines(json.dumps(q, ensure_ascii=False) + "\n" for q in new)
    return new


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", type=Path, default=QUESTIONS)
    ap.add_argument("--final", action="store_true")
    ap.add_argument("--write-split", action="store_true")
    ap.add_argument("--import-human", type=Path, metavar="FILE")
    ap.add_argument("--author")
    args = ap.parse_args()
    if args.import_human:
        print(f"imported {len(import_human(args.import_human, args.author, args.file))} questions")
    questions, corpus = load(args.file), Corpus()
    errs = check_all(questions, corpus, final=args.final or args.write_split)
    if args.write_split and all("split differs" in e for e in errs):
        want = split(questions)
        for q in questions:
            q["split"] = want[q["id"]]
        args.file.write_text("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in questions))
        errs = check_all(questions, corpus, final=True)
    for e in errs:
        print(e)
    by = Counter(q["difficulty"] for q in questions if labelled(q))
    blank = sum(not labelled(q) for q in questions)
    print(f"{len(questions)} questions ({blank} unlabelled); {dict(by)}")
    if (args.final or args.write_split) and not blank:
        stored = {q["id"]: q["split"] for q in questions}
        for name, c in split_counts(questions, stored).items():
            vals = sorted({v for v, _ in c}, key=str)
            print(f"  {name}: " + ", ".join(f"{v} {c[(v, 'dev')]}/{c[(v, 'test')]}" for v in vals))
        print("  (dev/test)")
    print("FAIL" if errs else "OK")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
