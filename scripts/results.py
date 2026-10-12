"""Generate docs/results.md, the results ledger (`make results`; `make results-check` in the gate).

Every number comes from eval_runs/eval_results, the ingested DB, or repo files; nothing is typed
by hand. The output is deterministic (no wall-clock time; seeded bootstrap), so `--check`
regenerates it in memory and fails when the committed file differs, e.g. after a new eval run.

Retrieval rows use the newest run of the locked config (config.py) and of the dense baseline
(dense, bge prefix on, same chunks and candidates) per split. Test rows appear only once a test
run exists (Phase 5, `make eval ARGS="--split test --final-test"`).
"""

import argparse
import re
import sys
from collections import Counter

import psycopg

from config import get_settings
from eval.ablation import bootstrap
from eval.retrieval_eval import NAMES, latency, metrics, token_gold, with_hits
from eval.validate_questions import QUESTIONS, SPLIT_DIMS, Corpus, load
from scripts.corpus.common import PROCESSED, ROOT, SEED

OUT = ROOT / "docs" / "results.md"
AUDIT = ROOT / "eval" / "label_audit.md"
GUIDE = ROOT / "docs" / "BUILD_GUIDE.md"

DEFINITIONS = {
    "recall@5": "per question, gold docs in the top 5 / gold docs, averaged",
    "recall@10": "per question, gold docs in the top 10 / gold docs, averaged",
    "hit@5": "share of questions with at least one gold doc in the top 5",
    "hit@10": "share of questions with at least one gold doc in the top 10",
    "mrr": "mean of 1 / rank of the first gold doc (0 if none in the 30-chunk ranking)",
    "all_gold@10": "questions with 2+ gold docs: share with every gold doc in the top 10",
    "token_doc@5": "exact-token questions: share of the gold docs holding the question's code or"
    " hostname that are in the top 5 (diagnostic; equals Recall@5 on most questions)",
    "agent_recall@10": "top 5 runbooks + top 5 incidents retrieved separately; mean fraction of"
    " gold docs found in the union (diagnostic)",
    "agent_all_gold@10": "same union; questions with 2+ gold docs, share with every gold doc"
    " (diagnostic)",
    "p50_ms": "median retrieve() wall time per question, models warm, Mac CPU",
    "p95_ms": "95th percentile retrieve() wall time per question, models warm, Mac CPU",
}
LEVEL_KEYS = [
    "recall@5",
    "recall@10",
    "hit@5",
    "hit@10",
    "mrr",
    "all_gold@10",
    "token_doc@5",
    "agent_recall@10",
    "agent_all_gold@10",
]
# (metric, per-question key, slice): the locked-vs-baseline comparisons
COMPARISONS = [
    ("recall@5", "recall@5", None),
    ("recall@5", "recall@5", "exact-token"),
    ("recall@5", "recall@5", "paraphrase"),
    ("recall@10", "recall@10", None),
    ("hit@5", "hit@5", None),
    ("hit@10", "hit@10", None),
    ("mrr", "rr", None),
    ("all_gold@10", "all_gold@10", None),
    ("token_doc@5", "token_doc@5", None),
    ("agent_recall@10", "agent_recall@10", None),
]
PLACEHOLDERS = [
    ("Citation accuracy (a): cited id was in the retrieved evidence", 3),
    ("Citation accuracy (b): cited evidence supports the claim (judged)", 3),
    ("Faithfulness (judge)", 3),
    ("Completeness (judge, expected key facts)", 3),
    ("Tool-call success rate", 3),
    ("Verify-loop rate (share of investigations that loop back)", 3),
    ("Attack success before defenses, tune half (15)", 4),
    ("Attack success before defenses, held-out half (15)", 4),
    ("Attack success after defenses, tune half (15)", 4),
    ("Attack success after defenses, held-out half (15)", 4),
    ("Judge agreement with Prince's 30 hand labels", 3),
    ("p50 / p95 latency, end to end per investigation", 4),
    ("p50 / p95 latency, retrieval on EC2", 5),
    ("p50 / p95 latency, LLM calls", 3),
    ("Tokens per investigation (input / output)", 4),
    ("Estimated cost per investigation", 4),
]
# Bullet template (by its opening words) -> the ledger rows that fill it.
BULLET_ROWS = {
    "Built hybrid": "Retrieval, test split: hybrid BM25 + dense vs dense, Recall@5 (mean fraction"
    " of gold docs found) levels for [X] (dense) and [Y] (locked hybrid), with the comparison"
    " row's CI; [N] documents from section (a); [N] questions = test n.",
    "Developed a LangGraph": "Section (c): citation accuracy (a)/(b) for [X], tool-call success"
    " for [Y] (Phase 3, then test in Phase 5).",
    "Cut prompt-injection": "Section (c): attack success before/after, held-out half (Phase 4);"
    " [N] = 30 cases (15 tune, 15 held-out, both reported).",
    "Built an automated eval": "Section (c): judge agreement for [X] (Phase 3); the retrieval"
    " section shows the retrieval half of the pipeline.",
    "Deployed FastAPI": "Section (c): p95 end-to-end latency on EC2 (Phase 5).",
}


def latest_run(conn, split: str, mode: str) -> tuple[int, str, list[dict]] | None:
    s = get_settings()
    row = conn.execute(
        "SELECT id, git_commit FROM eval_runs WHERE eval_type = 'retrieval' AND split = %s"
        " AND config->>'mode' = %s AND (config->>'bge_prefix')::boolean = %s"
        " AND (config->>'candidates')::int = %s AND (config->>'chunk_size')::int = %s"
        " AND (config->>'chunk_overlap')::int = %s AND config ? 'keyword_ranker'"
        " AND config->>'doc_type' IS NULL ORDER BY id DESC LIMIT 1",
        (split, mode, s.bge_prefix, s.retrieval_candidates, s.chunk_size, s.chunk_overlap),
    ).fetchone()
    if not row:
        return None
    res = conn.execute(
        "SELECT question_id, metrics FROM eval_results WHERE run_id = %s ORDER BY question_id",
        (row[0],),
    ).fetchall()
    gold = {q["id"]: q["relevant_doc_ids"] for q in load(QUESTIONS)}
    return row[0], row[1], with_hits([{"id": qid, **m} for qid, m in res], gold)


def corpus_section(conn, questions: list[dict]) -> list[str]:
    def lines(name: str) -> int:
        return len((PROCESSED / name).read_text().splitlines())

    s = get_settings()
    chunks = dict(
        conn.execute("SELECT metadata->>'doc_type', count(*) FROM chunks GROUP BY 1").fetchall()
    )
    logs = conn.execute(
        "SELECT CASE WHEN source LIKE 'loghub%%' THEN 'LogHub background'"
        " WHEN incident_id IS NOT NULL THEN 'synthetic, per incident'"
        " ELSE 'synthetic, demo (live)' END, count(*) FROM log_events GROUP BY 1 ORDER BY 1"
    ).fetchall()
    out = [
        "## (a) Corpus and eval set",
        "",
        "Everything is synthetic except the LogHub background log lines.",
        "",
        "| item | count | source |",
        "|---|---|---|",
        f"| runbooks | {lines('runbooks.jsonl')} | data/processed/runbooks.jsonl |",
        f"| incident write-ups | {lines('incidents.jsonl')} | data/processed/incidents.jsonl |",
        f"| chunks, runbook | {chunks.get('runbook', 0)} | DB, chunks {s.chunk_size}/"
        f"{s.chunk_overlap} tokens |",
        f"| chunks, incident | {chunks.get('incident', 0)} | DB |",
    ]
    out += [f"| log lines, {k} | {n} | DB, log_events |" for k, n in logs]
    out += ["", f"Questions: {len(questions)} (eval/questions.jsonl).", ""]
    out += ["| dimension | value | dev | test | all |", "|---|---|---|---|---|"]
    for dim, f in SPLIT_DIMS.items():
        c = Counter((f(q), q["split"]) for q in questions)
        for v in sorted({v for v, _ in c}, key=str):
            dev, test = c[(v, "dev")], c[(v, "test")]
            out.append(f"| {dim} | {v} | {dev} | {test} | {dev + test} |")

    text = AUDIT.read_text()
    head = re.search(
        r"Total questions: (\d+)\. Gemini lines: (\d+)\. Flagged: (\d+)\. Sampled OK: (\d+)", text
    )
    verdicts = re.findall(r"^\*\*Prince:\*\*.*?\b(AGREE|DISAGREE)\s*$", text, re.M)
    v = Counter(verdicts)
    out += [
        "",
        "Gemini label audit (eval/label_audit.md, pasted by hand; the Claude-drafted questions):",
        "",
        "| item | count |",
        "|---|---|",
        f"| questions audited | {head[1]} |",
        f"| Gemini verdict lines | {head[2]} |",
        f"| flagged by Gemini | {head[3]} |",
        f"| sampled Gemini-OK rows | {head[4]} |",
        f"| adjudicated by Prince: AGREE | {v['AGREE']} |",
        f"| adjudicated by Prince: DISAGREE | {v['DISAGREE']} |",
        f"| adjudicated, total | {len(verdicts)} |",
    ]
    return out


def fmt(x) -> str:
    return "-" if x is None else f"{x:.3f}"


def retrieval_section(conn, questions: list[dict]) -> list[str]:
    s = get_settings()
    locked = f"{s.retrieval_mode} (bge prefix {'on' if s.bge_prefix else 'off'})"
    out = [
        "## (b) Retrieval",
        "",
        f"Locked config: `{s.retrieval_mode}`, bge prefix {'on' if s.bge_prefix else 'off'},"
        f" {s.retrieval_candidates} candidates, chunks {s.chunk_size}/{s.chunk_overlap}."
        " Baseline: `dense`, same settings. Doc-level: 30 chunks retrieved, deduped to docs by"
        " best rank.",
        "",
        "- **Levels** are measured values, not claims (claimable: n/a).",
        "- **Comparisons** are locked minus baseline, paired by question, percentile bootstrap"
        f" 95% CI, 10,000 resamples, seed {SEED}. Claimable = yes only when the CI excludes 0.",
        "- Resume numbers come from **test** rows only (Phase 5). Dev rows are for development.",
        "",
        "| metric | definition | split | config | n | value | 95% CI | claimable"
        " | eval_run | commit |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for split in ("dev", "test"):
        qs = [q for q in questions if q["split"] == split]
        a = latest_run(conn, split, s.retrieval_mode)
        b = latest_run(conn, split, "dense")
        if not a or not b:
            out.append(
                f"| all | - | {split} | - | - | not measured yet"
                f" ({'Phase 5' if split == 'test' else 'run make eval'}) | - | - | - | - |"
            )
            continue
        for (run_id, commit, rows), name in ((a, locked), (b, "dense (bge prefix on)")):
            m, lat = metrics(rows), latency(rows)
            for k in LEVEL_KEYS:
                n = {"all_gold@10": m["n_multi"], "agent_all_gold@10": m["n_multi"]}.get(
                    k, m["n_token"] if k == "token_doc@5" else m["n"]
                )
                out.append(
                    f"| {NAMES[k]} | {DEFINITIONS[k]} | {split} | {name} | {n} | {fmt(m[k])}"
                    f" | - | n/a | {run_id} | {commit[:12]} |"
                )
            for k in ("p50_ms", "p95_ms"):
                out.append(
                    f"| retrieval latency {k[:3]} | {DEFINITIONS[k]} | {split} | {name}"
                    f" | {m['n']} | {lat[k]:.1f} ms | - | n/a | {run_id} | {commit[:12]} |"
                )
        by_id = {q["id"]: q for q in qs}
        ra, rb = {r["id"]: r for r in a[2]}, {r["id"]: r for r in b[2]}
        for metric, key, sl in COMPARISONS:
            ids = sorted(
                i
                for i in ra
                if ra[i].get(key) is not None
                and rb[i].get(key) is not None
                and sl in (None, by_id[i]["difficulty"])
            )
            diff, lo, hi = bootstrap([ra[i][key] for i in ids], [rb[i][key] for i in ids])
            claim = "yes" if lo > 0 or hi < 0 else "no"
            where = "" if sl is None else f", {sl} only"
            out.append(
                f"| {NAMES[metric]}: locked - dense{where} | difference of the levels above"
                f" | {split} | {locked} vs dense | {len(ids)} | {diff:+.3f}"
                f" | [{lo:+.3f}, {hi:+.3f}] | {claim} | {a[0]} vs {b[0]}"
                f" | {a[1][:12]} / {b[1][:12]} |"
            )
    tg = token_gold([q for q in questions if q["split"] == "dev"], Corpus())
    ex = [q for q in questions if q["split"] == "dev" and q["difficulty"] == "exact-token"]
    same = sum(sorted(tg.get(q["id"], [])) == sorted(q["relevant_doc_ids"]) for q in ex)
    out += [
        "",
        f"token-doc Recall@5 is not independent of Recall@5: on dev, {same} of {len(ex)}"
        " exact-token questions have every gold doc holding one of their tokens.",
    ]
    return out


def placeholder_section() -> list[str]:
    out = ["## (c) Not measured yet", "", "| metric | status |", "|---|---|"]
    out += [f"| {name} | not measured yet (Phase {p}) |" for name, p in PLACEHOLDERS]
    return out


def bullets_section() -> list[str]:
    text = GUIDE.read_text()
    block = text[text.index("**Bullet templates**") : text.index("**Skills line:**")]
    templates = [ln[2:].strip() for ln in block.splitlines() if ln.startswith("- ")]
    out = [
        "## (d) Resume bullet templates (docs/BUILD_GUIDE.md) and the rows that fill them",
        "",
        "Only test-split rows fill the brackets. A bullet whose number comes out weak is dropped.",
        "",
    ]
    for t in templates:
        rows = next((v for k, v in BULLET_ROWS.items() if t.startswith(k)), None)
        if rows is None:
            raise SystemExit(f"no ledger mapping for bullet template: {t[:60]!r}")
        out += [f"- {t}", f"  - Filled by: {rows}"]
    return out


def render() -> str:
    questions = load(QUESTIONS)
    with psycopg.connect(get_settings().database_url) as conn:
        a = corpus_section(conn, questions)
        b = retrieval_section(conn, questions)
    out = [
        "# OpsPilot results ledger",
        "",
        "Generated by `make results` (scripts/results.py) from eval_runs, the ingested DB and repo"
        " files. Do not edit by hand: `make gate` fails when this file is stale.",
        "",
        *a,
        "",
        *b,
        "",
        *placeholder_section(),
        "",
        *bullets_section(),
    ]
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    text = render()
    if args.check:
        if not OUT.exists() or OUT.read_text() != text:
            print("docs/results.md is stale: run `make results` and commit it")
            return 1
        print("docs/results.md is current")
        return 0
    OUT.write_text(text)
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
