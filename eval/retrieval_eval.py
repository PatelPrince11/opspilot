"""Retrieval eval: doc-level Recall@5, Recall@10, MRR and all-gold Recall@10 (`make eval`).

    python -m eval.retrieval_eval                        # dev, locked config from config.py
    python -m eval.retrieval_eval --mode dense --no-bge-prefix
    python -m eval.retrieval_eval --split test --final-test   # Phase 5, once

Each question retrieves DEPTH chunks; chunks are deduped to documents keeping each document's
best rank, and metrics are computed on that document ranking:
- Recall@k (mean fraction of gold docs found): per question |gold ∩ top k| / |gold|, averaged.
  Not the share of questions with any gold doc in the top k; that is Hit@k.
- Hit@k (at least one gold doc in top k): 1 if any gold doc is in the top k, else 0, averaged.
  It flatters multi-gold questions (one of two docs found counts as a full hit).
- MRR: 1 / rank of the first gold document (0 if none in the ranking).
- all-gold Recall@10: questions with 2+ gold documents only; 1 if every gold doc is in the top 10.
Latency is retrieve()'s wall time per question, models loaded beforehand.

Two diagnostics (not done-checks):
- token-doc Recall@5: exact-token questions only. Of the gold docs that contain one of the
  question's codes or hostnames (validator's Corpus.exact_tokens, whole-token match), the share
  in the top 5. Questions with no such gold doc are left out; their count is the n shown.
- agent view: the top 5 runbooks and the top 5 incidents, retrieved separately (doc_type
  filter, as Phase 3's search_runbooks and search_incidents will), scored as Recall@10 and
  all-gold Recall@10 on the union of the two.

Every run goes to eval_runs (commit, config, model revisions, split, timestamp) with one
eval_results row per question. A run of a named config (the locked one, or the dense baseline)
also writes eval/results/<split>_<name>.json: per-question scores, metrics, config, models,
a corpus snapshot and provenance, with no timestamps or run ids. scripts/results.py builds
docs/results.md from these committed files only. When the scores, metrics, config, models and
corpus are identical to the file's, the file is left untouched (its commit and latency stay
those of the run that first produced the numbers), so re-running changes nothing.

Refuses to run unless the DB was ingested with the current
chunk settings, so the stored config describes the chunks that were searched.
"""

import argparse
import json
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from statistics import mean, median, quantiles

import psycopg

from config import get_settings
from eval.validate_questions import QUESTIONS, SPLIT_DIMS, Corpus, has, load
from rag.ingestion import content_hash, documents
from rag.retrieval import MODES, Hit, RetrievalConfig, retrieve
from scripts.corpus.common import PROCESSED

DEPTH = 30  # chunks per question, before the doc dedupe
RESULTS = Path(__file__).parent / "results"
SCORE_KEYS = (
    "recall@5",
    "recall@10",
    "hit@5",
    "hit@10",
    "rr",
    "all_gold@10",
    "token_doc@5",
    "agent_recall@10",
    "agent_all_gold@10",
    "docs",
)
NAMES = {
    "recall@5": "Recall@5 (mean fraction of gold docs found)",
    "recall@10": "Recall@10 (mean fraction of gold docs found)",
    "hit@5": "Hit@5 (at least one gold doc in top 5)",
    "hit@10": "Hit@10 (at least one gold doc in top 10)",
    "rr": "MRR",
    "mrr": "MRR",
    "all_gold@10": "all-gold Recall@10 (2+ gold docs, all in top 10)",
    "token_doc@5": "token-doc Recall@5 (diagnostic)",
    "agent_recall@10": "agent-view Recall@10 (diagnostic, mean fraction of gold docs found)",
    "agent_all_gold@10": "agent-view all-gold Recall@10 (diagnostic)",
}
SLICES = ("difficulty", "author", "gold bucket", "false_premise")
TEST_WARNING = (
    "WARNING: running on the TEST split. Test is touched once, in Phase 5, with the locked"
    " config. Never tune on these numbers."
)


def check_split(split: str, final_test: bool) -> None:
    if split == "test" and not final_test:
        raise SystemExit(
            "refusing --split test without --final-test: test is touched once, in Phase 5"
        )
    if split == "test":
        print(TEST_WARNING, file=sys.stderr)


def dedupe(hits: list[Hit]) -> list[str]:
    """Document ids in order of each document's best-ranked chunk."""
    return list(dict.fromkeys(h.document_id for h in hits))


def score(ranked: list[str], gold: list[str]) -> dict:
    g = set(gold)
    first = next((i for i, d in enumerate(ranked, 1) if d in g), None)
    return {
        "recall@5": len(g & set(ranked[:5])) / len(g),
        "recall@10": len(g & set(ranked[:10])) / len(g),
        "hit@5": float(bool(g & set(ranked[:5]))),
        "hit@10": float(bool(g & set(ranked[:10]))),
        "rr": 1 / first if first else 0.0,
        "all_gold@10": float(g <= set(ranked[:10])) if len(g) > 1 else None,
    }


def token_gold(questions: list[dict], corpus: Corpus) -> dict[str, list[str]]:
    """Exact-token question id -> its gold docs that contain one of its codes or hostnames."""
    out = {}
    for q in questions:
        if q["difficulty"] != "exact-token":
            continue
        toks = corpus.exact_tokens(q["question"])
        gold = [d for d in q["relevant_doc_ids"] if any(has(corpus.text[d], t) for t in toks)]
        if gold:
            out[q["id"]] = gold
    return out


def agent_view(conn, query: str, cfg: RetrievalConfig) -> list[str]:
    """Top 5 runbooks then top 5 incidents, each from its own filtered retrieval."""
    docs = []
    for doc_type in ("runbook", "incident"):
        docs += dedupe(retrieve(query, replace(cfg, doc_type=doc_type), conn))[:5]
    return docs


def run(
    conn, questions: list[dict], cfg: RetrievalConfig, tokens_gold: dict | None = None
) -> list[dict]:
    """One row per question: metrics, diagnostics, latency and the top 10 chunks (doc, index,
    tokens)."""
    cfg = replace(cfg, k=DEPTH)
    tokens_gold = tokens_gold or {}
    tokens = dict(conn.execute("SELECT id, token_count FROM chunks").fetchall())
    retrieve("warm-up query", cfg, conn)  # loads the models outside the timings
    rows = []
    for q in questions:
        t0 = time.perf_counter()
        hits = retrieve(q["question"], cfg, conn)
        ms = (time.perf_counter() - t0) * 1000
        top = [[h.document_id, h.chunk_index, tokens[h.chunk_id]] for h in hits[:10]]
        ranked = dedupe(hits)
        tg = tokens_gold.get(q["id"])
        agent = score(agent_view(conn, q["question"], cfg), q["relevant_doc_ids"])
        rows.append(
            {"id": q["id"], **score(ranked, q["relevant_doc_ids"]), "latency_ms": ms}
            | {"token_doc@5": score(ranked, tg)["recall@5"] if tg else None}
            | {"agent_recall@10": agent["recall@10"], "agent_all_gold@10": agent["all_gold@10"]}
            | {"docs": ranked[:10], "top_chunks": top}
        )
    return rows


def with_hits(rows: list[dict], gold: dict[str, list[str]]) -> list[dict]:
    """Rows stored before Hit@k existed get it from their stored top-10 docs (exact: `docs`
    is the deduped ranking's first 10)."""
    return [
        r
        if "hit@5" in r
        else r
        | {
            "hit@5": float(bool(set(gold[r["id"]]) & set(r["docs"][:5]))),
            "hit@10": float(bool(set(gold[r["id"]]) & set(r["docs"][:10]))),
        }
        for r in rows
    ]


def present(rows: list[dict], key: str) -> list[float]:
    return [r[key] for r in rows if r.get(key) is not None]


def metrics(rows: list[dict]) -> dict:
    multi = present(rows, "all_gold@10")
    tok = present(rows, "token_doc@5")
    agent_multi = present(rows, "agent_all_gold@10")
    return {
        "n": len(rows),
        "recall@5": mean(r["recall@5"] for r in rows),
        "recall@10": mean(r["recall@10"] for r in rows),
        "hit@5": mean(r["hit@5"] for r in rows),
        "hit@10": mean(r["hit@10"] for r in rows),
        "mrr": mean(r["rr"] for r in rows),
        "all_gold@10": mean(multi) if multi else None,
        "n_multi": len(multi),
        "token_doc@5": mean(tok) if tok else None,
        "n_token": len(tok),
        "agent_recall@10": mean(agent) if (agent := present(rows, "agent_recall@10")) else None,
        "agent_all_gold@10": mean(agent_multi) if agent_multi else None,
    }


def latency(rows: list[dict]) -> dict:
    ms = [r["latency_ms"] for r in rows]
    return {"p50_ms": median(ms), "p95_ms": quantiles(ms, n=100)[94] if len(ms) > 1 else ms[0]}


def summarize(rows: list[dict], questions: list[dict]) -> dict:
    """{"overall": metrics, "<dim>=<value>": metrics, ...}, plus latency."""
    by_id = {q["id"]: q for q in questions}
    out = {"overall": metrics(rows)}
    for dim in SLICES:
        f = SPLIT_DIMS[dim]
        for v in sorted({f(by_id[r["id"]]) for r in rows}, key=str):
            out[f"{dim}={v}"] = metrics([r for r in rows if f(by_id[r["id"]]) == v])
    return out | {"latency": latency(rows)}


def fmt(x: float | None) -> str:
    return "-" if x is None else f"{x:.3f}"


def table(summary: dict) -> str:
    lines = [
        "| slice | n | "
        + " | ".join(NAMES[k] for k in ("recall@5", "recall@10", "hit@5"))
        + f" | {NAMES['hit@10']} | MRR | {NAMES['all_gold@10']} (n) | {NAMES['token_doc@5']} (n)"
        f" | {NAMES['agent_recall@10']} | {NAMES['agent_all_gold@10']} (n) |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, m in summary.items():
        if name == "latency":
            continue
        ag = "-" if m["all_gold@10"] is None else f"{m['all_gold@10']:.3f} ({m['n_multi']})"
        tok = "-" if m["token_doc@5"] is None else f"{m['token_doc@5']:.3f} ({m['n_token']})"
        aag = m["agent_all_gold@10"]
        aag = "-" if aag is None else f"{aag:.3f} ({m['n_multi']})"
        lines.append(
            f"| {name} | {m['n']} | {m['recall@5']:.3f} | {m['recall@10']:.3f}"
            f" | {m['hit@5']:.3f} | {m['hit@10']:.3f} | {m['mrr']:.3f} | {ag} | {tok}"
            f" | {fmt(m['agent_recall@10'])} | {aag} |"
        )
    lat = summary["latency"]
    lines.append(f"\nlatency p50 {lat['p50_ms']:.1f} ms, p95 {lat['p95_ms']:.1f} ms")
    return "\n".join(lines)


def git_commit() -> str:
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    dirty = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"], capture_output=True, text=True
    ).stdout.strip()
    return head.stdout.strip() + ("-dirty" if dirty else "")


def run_config(cfg: RetrievalConfig) -> dict:
    s = get_settings()
    return asdict(cfg) | {
        "k": DEPTH,
        "chunk_size": s.chunk_size,
        "chunk_overlap": s.chunk_overlap,
        "keyword_ranker": "bm25",  # runs without it predate BM25 (keyword was ts_rank_cd)
    }


def models() -> dict:
    s = get_settings()
    return {
        "embed": f"{s.embed_model}@{s.embed_revision}",
        "rerank": f"{s.rerank_model}@{s.rerank_revision}",
    }


def store(conn, split: str, cfg: RetrievalConfig, rows: list[dict]) -> int:
    run_id = conn.execute(
        "INSERT INTO eval_runs (git_commit, eval_type, split, config, models)"
        " VALUES (%s, 'retrieval', %s, %s, %s) RETURNING id",
        (git_commit(), split, json.dumps(run_config(cfg), default=str), json.dumps(models())),
    ).fetchone()[0]
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO eval_results (run_id, question_id, metrics) VALUES (%s, %s, %s)",
            [(run_id, r.pop("id"), json.dumps(r)) for r in map(dict, rows)],
        )
    return run_id


def named(cfg: RetrievalConfig) -> str | None:
    """The committed-results name of a config, or None for ablation-only configs."""
    if cfg == RetrievalConfig():
        return "locked"
    if cfg == RetrievalConfig(mode="dense"):
        return "dense_baseline"
    return None


def provenance() -> dict:
    """HEAD plus the tracked files that differed from it: the same for the same tree."""
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True)
    changed = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"], capture_output=True, text=True, check=True
    )
    return {"commit": head.stdout.strip(), "uncommitted_changes": changed.stdout.split()}


def corpus_snapshot(conn) -> dict:
    """What was searched, for a clean clone that has no DB (and no LogHub file)."""
    q = {
        "documents": "SELECT doc_type, count(*) FROM documents GROUP BY 1",
        "chunks": "SELECT metadata->>'doc_type', count(*) FROM chunks GROUP BY 1",
        "log_lines": "SELECT CASE WHEN source LIKE 'loghub%%' THEN 'LogHub background'"
        " WHEN incident_id IS NOT NULL THEN 'synthetic, per incident'"
        " ELSE 'synthetic, demo (live)' END, count(*) FROM log_events GROUP BY 1",
    }
    return {k: dict(sorted(conn.execute(v).fetchall())) for k, v in q.items()}


def result_content(split, cfg, rows, questions, corpus) -> dict:
    """The deterministic part of a named result file."""
    summary = summarize(rows, questions)
    summary.pop("latency")
    content = {
        "split": split,
        "config": run_config(cfg),
        "models": models(),
        "corpus": corpus,
        "metrics": summary,
        "questions": [{"id": r["id"], **{k: r[k] for k in SCORE_KEYS}} for r in rows],
    }
    return json.loads(json.dumps(content, default=str))  # as it will read back


def write_named(path: Path, content: dict, extra: dict) -> bool:
    """Write content + extra (provenance, latency) unless the file already holds identical
    content. Returns whether it wrote."""
    if path.exists():
        old = json.loads(path.read_text())
        if {k: old.get(k) for k in content} == content:
            return False
    path.write_text(json.dumps(content | extra, indent=1, sort_keys=True) + "\n")
    return True


def check_ingested(conn, processed: Path = PROCESSED) -> None:
    s = get_settings()
    want = {
        d["id"]: content_hash(d, s.chunk_size, s.chunk_overlap, s.embed_model, s.embed_revision)
        for d in documents(processed)
    }
    have = dict(conn.execute("SELECT id, content_hash FROM documents WHERE corpus = 'main'"))
    if have != want:
        stale = sorted(k for k in want.keys() | have.keys() if want.get(k) != have.get(k))
        raise SystemExit(
            f"{len(stale)} documents (e.g. {stale[:3]}) were not ingested with the current"
            f" settings (chunk {s.chunk_size}/{s.chunk_overlap}): run `make ingest` first"
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--split", choices=("dev", "test"), default="dev")
    ap.add_argument("--final-test", action="store_true")
    ap.add_argument("--mode", choices=MODES)
    ap.add_argument("--candidates", type=int)
    ap.add_argument("--bge-prefix", action=argparse.BooleanOptionalAction, default=None)
    args = ap.parse_args(argv)
    check_split(args.split, args.final_test)

    overrides = {
        k: v
        for k, v in {
            "mode": args.mode,
            "candidates": args.candidates,
            "bge_prefix": args.bge_prefix,
        }.items()
        if v is not None
    }
    cfg = RetrievalConfig(**overrides)
    questions = [q for q in load(QUESTIONS) if q["split"] == args.split]
    with psycopg.connect(get_settings().database_url) as conn:
        check_ingested(conn)
        rows = run(conn, questions, cfg, token_gold(questions, Corpus()))
        run_id = store(conn, args.split, cfg, rows)
        conn.commit()
        corpus = corpus_snapshot(conn)
    name = named(cfg)
    if name:
        path = RESULTS / f"{args.split}_{name}.json"
        content = result_content(args.split, cfg, rows, questions, corpus)
        wrote = write_named(path, content, provenance() | {"latency": latency(rows)})
        print(f"{path.relative_to(RESULTS.parent.parent)}: {'written' if wrote else 'unchanged'}")
    s = get_settings()
    print(
        f"split {args.split}, {len(rows)} questions; mode {cfg.mode},"
        f" bge_prefix {cfg.bge_prefix}, candidates {cfg.candidates},"
        f" chunk {s.chunk_size}/{s.chunk_overlap}; eval_runs id {run_id}"
    )
    print(table(summarize(rows, questions)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
