"""Retrieval eval: doc-level Recall@5, Recall@10, MRR and all-gold Recall@10 (`make eval`).

    python -m eval.retrieval_eval                        # dev, locked config from config.py
    python -m eval.retrieval_eval --mode dense --no-bge-prefix
    python -m eval.retrieval_eval --split test --final-test   # Phase 5, once

Each question retrieves DEPTH chunks; chunks are deduped to documents keeping each document's
best rank, and metrics are computed on that document ranking:
- Recall@k: share of the question's gold documents in the top k.
- MRR: 1 / rank of the first gold document (0 if none in the ranking).
- all-gold Recall@10: questions with 2+ gold documents only; 1 if every gold doc is in the top 10.
Latency is retrieve()'s wall time per question, models loaded beforehand.

Every run goes to eval_runs (commit, config, model revisions, split, timestamp) with one
eval_results row per question. Refuses to run unless the DB was ingested with the current
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
from eval.validate_questions import QUESTIONS, SPLIT_DIMS, load
from rag.ingestion import content_hash, documents
from rag.retrieval import MODES, Hit, RetrievalConfig, retrieve
from scripts.corpus.common import PROCESSED

DEPTH = 30  # chunks per question, before the doc dedupe
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
        "rr": 1 / first if first else 0.0,
        "all_gold@10": float(g <= set(ranked[:10])) if len(g) > 1 else None,
    }


def run(conn, questions: list[dict], cfg: RetrievalConfig) -> list[dict]:
    """One row per question: metrics, latency and the top 10 chunks (doc, index, tokens)."""
    cfg = replace(cfg, k=DEPTH)
    tokens = dict(conn.execute("SELECT id, token_count FROM chunks").fetchall())
    retrieve("warm-up query", cfg, conn)  # loads the models outside the timings
    rows = []
    for q in questions:
        t0 = time.perf_counter()
        hits = retrieve(q["question"], cfg, conn)
        ms = (time.perf_counter() - t0) * 1000
        top = [[h.document_id, h.chunk_index, tokens[h.chunk_id]] for h in hits[:10]]
        ranked = dedupe(hits)
        rows.append(
            {"id": q["id"], **score(ranked, q["relevant_doc_ids"]), "latency_ms": ms}
            | {"docs": ranked[:10], "top_chunks": top}
        )
    return rows


def metrics(rows: list[dict]) -> dict:
    multi = [r["all_gold@10"] for r in rows if r["all_gold@10"] is not None]
    return {
        "n": len(rows),
        "recall@5": mean(r["recall@5"] for r in rows),
        "recall@10": mean(r["recall@10"] for r in rows),
        "mrr": mean(r["rr"] for r in rows),
        "all_gold@10": mean(multi) if multi else None,
        "n_multi": len(multi),
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


def table(summary: dict) -> str:
    lines = [
        "| slice | n | Recall@5 | Recall@10 | MRR | all-gold R@10 (n) |",
        "|---|---|---|---|---|---|",
    ]
    for name, m in summary.items():
        if name == "latency":
            continue
        ag = "-" if m["all_gold@10"] is None else f"{m['all_gold@10']:.3f} ({m['n_multi']})"
        lines.append(
            f"| {name} | {m['n']} | {m['recall@5']:.3f} | {m['recall@10']:.3f}"
            f" | {m['mrr']:.3f} | {ag} |"
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
        rows = run(conn, questions, cfg)
        run_id = store(conn, args.split, cfg, rows)
        conn.commit()
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
