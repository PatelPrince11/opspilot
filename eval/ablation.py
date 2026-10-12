"""Phase 2 retrieval ablation on dev -> eval/results/ablation.md.

    CHUNK_SIZE=400 CHUNK_OVERLAP=50 make ingest
    CHUNK_SIZE=400 CHUNK_OVERLAP=50 uv run python -m eval.ablation run   # 7 configs, stored
    CHUNK_SIZE=256 CHUNK_OVERLAP=32 make ingest
    CHUNK_SIZE=256 CHUNK_OVERLAP=32 uv run python -m eval.ablation run
    uv run python -m eval.ablation report   # latest dev run per config, from eval_runs

(Host-side, DATABASE_URL must point at localhost:5433, as in the Makefile.) Dev only: there is
no option to run on test.
"""

import argparse
import random
import sys
from collections import Counter
from pathlib import Path

import psycopg

from config import get_settings
from eval.retrieval_eval import (
    check_ingested,
    latency,
    metrics,
    run,
    store,
    summarize,
)
from eval.validate_questions import QUESTIONS, load
from rag.retrieval import RetrievalConfig
from scripts.corpus.common import SEED

OUT = Path(__file__).parent / "results" / "ablation.md"
FINDINGS = "## Findings (hand-written; kept when the tables are regenerated)"
RESAMPLES = 10_000
SMALL = 50  # tokens: the sub-50-token chunks
CONFIGS = [
    ("dense", True),
    ("dense", False),
    ("keyword", None),
    ("hybrid", True),
    ("hybrid", False),
    ("hybrid_rerank", True),
    ("hybrid_rerank", False),
]
METRICS = (("recall@5", "Recall@5"), ("recall@10", "Recall@10"), ("mrr", "MRR"))


def label(mode: str, prefix: bool | None, chunk: str) -> str:
    p = "" if prefix is None else (" +prefix" if prefix else " -prefix")
    return f"{mode}{p} @{chunk}"


def run_all() -> None:
    s = get_settings()
    questions = [q for q in load(QUESTIONS) if q["split"] == "dev"]
    with psycopg.connect(s.database_url) as conn:
        check_ingested(conn)
        for mode, prefix in CONFIGS:
            cfg = RetrievalConfig(mode=mode, bge_prefix=bool(prefix), candidates=30)
            rows = run(conn, questions, cfg)
            run_id = store(conn, "dev", cfg, rows)
            conn.commit()
            m = metrics(rows)
            print(
                f"run {run_id}: {label(mode, prefix, f'{s.chunk_size}/{s.chunk_overlap}')}"
                f" R@5 {m['recall@5']:.3f} p95 {latency(rows)['p95_ms']:.0f} ms"
            )


def latest_runs(conn) -> dict[str, list[dict]]:
    """{label: per-question rows} for the newest dev run of each config and chunk size."""
    runs = conn.execute(
        "SELECT id, config FROM eval_runs WHERE eval_type = 'retrieval' AND split = 'dev'"
        " AND (config->>'candidates')::int = 30 ORDER BY id"
    ).fetchall()
    newest = {}
    for run_id, c in runs:
        prefix = None if c["mode"] == "keyword" else c["bge_prefix"]
        newest[label(c["mode"], prefix, f"{c['chunk_size']}/{c['chunk_overlap']}")] = run_id
    out = {}
    for name, run_id in newest.items():
        res = conn.execute(
            "SELECT question_id, metrics FROM eval_results WHERE run_id = %s ORDER BY question_id",
            (run_id,),
        ).fetchall()
        out[name] = [{"id": qid, **m} for qid, m in res]
    return out


def bootstrap(a: list[float], b: list[float], seed: int = SEED) -> tuple[float, float, float]:
    """Mean of a - b (paired by question) and its 95% percentile-bootstrap CI."""
    d = [x - y for x, y in zip(a, b, strict=True)]
    rng, n = random.Random(seed), len(d)
    means = sorted(sum(rng.choices(d, k=n)) / n for _ in range(RESAMPLES))
    return sum(d) / n, means[int(0.025 * RESAMPLES)], means[int(0.975 * RESAMPLES) - 1]


def ci_rows(runs, questions, a: str, b: str) -> list[str]:
    by_id = {q["id"]: q for q in questions}
    out = []
    for name, keep in [("overall", lambda q: True)] + [
        (f"difficulty={v}", lambda q, v=v: q["difficulty"] == v)
        for v in ("exact-token", "multi-doc", "paraphrase")
    ]:
        ra = {r["id"]: r["recall@5"] for r in runs[a]}
        rb = {r["id"]: r["recall@5"] for r in runs[b]}
        ids = sorted(i for i in ra if keep(by_id[i]))
        diff, lo, hi = bootstrap([ra[i] for i in ids], [rb[i] for i in ids])
        claim = "yes" if lo > 0 or hi < 0 else "no"
        out.append(
            f"| {a} vs {b} | {name} | {len(ids)} | {diff:+.3f} | [{lo:+.3f}, {hi:+.3f}] | {claim} |"
        )
    return out


def small_chunks(runs, questions) -> list[str]:
    """Sub-50-token chunks in the top 5 chunk positions: how often, and how often off-gold."""
    gold = {q["id"]: set(q["relevant_doc_ids"]) for q in questions}
    lines = [
        "| config | top-5 chunk slots | sub-50 slots | sub-50 off-gold | other off-gold rate"
        " | sub-50 off-gold rate |",
        "|---|---|---|---|---|---|",
    ]
    culprits: Counter = Counter()
    for name, rows in runs.items():
        small = small_off = other = other_off = 0
        for r in rows:
            for doc, idx, tokens in r["top_chunks"][:5]:
                off = doc not in gold[r["id"]]
                if tokens < SMALL:
                    small, small_off = small + 1, small_off + off
                    if off:
                        culprits[(name.split(" @")[1], doc, idx, tokens)] += 1
                else:
                    other, other_off = other + 1, other_off + off
        rate = f"{small_off / small:.2f}" if small else "-"
        lines.append(
            f"| {name} | {small + other} | {small} | {small_off} | {other_off / other:.2f}"
            f" | {rate} |"
        )
    lines += ["", "Most frequent sub-50 off-gold chunks (chunking, doc, chunk index, tokens):", ""]
    lines += [f"- {k}: {n} times" for k, n in culprits.most_common(8)] or ["- none"]
    return lines


def report() -> str:
    questions = [q for q in load(QUESTIONS) if q["split"] == "dev"]
    with psycopg.connect(get_settings().database_url) as conn:
        runs = latest_runs(conn)
        sizes = dict(
            conn.execute(
                "SELECT 'n', count(*) FILTER (WHERE token_count < %s) || ' of ' || count(*)"
                " FROM chunks",
                (SMALL,),
            ).fetchall()
        )
    names = sorted(runs, key=lambda n: (n.split(" @")[1], n))
    summaries = {n: summarize(runs[n], questions) for n in names}
    out = [
        "# Phase 2 retrieval ablation (dev split, 100 questions)",
        "",
        "**Rule: a difference is claimed only when its paired-bootstrap 95% CI excludes 0.**"
        f" Paired bootstrap over dev questions, {RESAMPLES:,} resamples, seed {SEED}. Everything"
        " else is reported but not claimed. Many comparisons are made, so expect about one in"
        " twenty null comparisons to exclude 0 by chance; a lone borderline CI is weak evidence.",
        "",
        "Doc-level metrics: retrieve 30 chunks, dedupe to documents by best rank. all-gold"
        " R@10 counts questions with 2+ gold docs only (its n in brackets). Latency is"
        " retrieve() wall time on the Mac's CPU (M-series, models warm), one query at a time.",
        "Generated by `python -m eval.ablation report` from the newest dev run of each config in"
        " eval_runs.",
        "",
        "## Headline (overall, n = 100)",
        "",
        "| config | Recall@5 | Recall@10 | MRR | all-gold R@10 (n) | p50 ms | p95 ms |",
        "|---|---|---|---|---|---|---|",
    ]
    for n in names:
        m, lat = summaries[n]["overall"], summaries[n]["latency"]
        out.append(
            f"| {n} | {m['recall@5']:.3f} | {m['recall@10']:.3f} | {m['mrr']:.3f}"
            f" | {m['all_gold@10']:.3f} ({m['n_multi']}) | {lat['p50_ms']:.0f}"
            f" | {lat['p95_ms']:.0f} |"
        )

    out += ["", "## Key comparisons: Recall@5 difference, paired bootstrap 95% CI", ""]
    out += ["| A vs B | slice | n | mean A-B | 95% CI | claim |", "|---|---|---|---|---|---|"]
    comparisons = []
    for chunk in ("400/50", "256/32"):
        comparisons += [
            ("hybrid_rerank +prefix", "dense +prefix", chunk),
            ("hybrid +prefix", "dense +prefix", chunk),
            ("hybrid_rerank +prefix", "hybrid +prefix", chunk),
            ("keyword", "dense +prefix", chunk),
            ("dense +prefix", "dense -prefix", chunk),
            ("hybrid +prefix", "hybrid -prefix", chunk),
            ("hybrid_rerank +prefix", "hybrid_rerank -prefix", chunk),
        ]
    for a, b, chunk in comparisons:
        a, b = f"{a} @{chunk}", f"{b} @{chunk}"
        if a in runs and b in runs:
            out += ci_rows(runs, questions, a, b)
    for mode in ("dense +prefix", "keyword", "hybrid +prefix", "hybrid_rerank +prefix"):
        a, b = f"{mode} @256/32", f"{mode} @400/50"
        if a in runs and b in runs:
            out += ci_rows(runs, questions, a, b)

    slices = [s for s in summaries[names[0]] if s != "latency"]
    for chunk in sorted({n.split(" @")[1] for n in names}, reverse=True):
        cols = [n for n in names if n.endswith(f"@{chunk}")]
        short = [c.removesuffix(f" @{chunk}") for c in cols]
        out += ["", f"## Per tag, chunks {chunk}", ""]
        for key, title in METRICS + (("all_gold@10", "all-gold Recall@10"),):
            out += [f"**{title}**", ""]
            out += ["| slice | n | " + " | ".join(short) + " |", "|---|---|" + "---|" * len(cols)]
            for sl in slices:
                m0 = summaries[cols[0]][sl]
                n = m0["n_multi"] if key == "all_gold@10" else m0["n"]
                if key == "all_gold@10" and not n:
                    continue
                vals = [summaries[c][sl][key] for c in cols]
                out.append(f"| {sl} | {n} | " + " | ".join(f"{v:.3f}" for v in vals) + " |")
            out.append("")
        lat = [summaries[c]["latency"] for c in cols]
        out += ["**Latency (ms)**", "", "| | " + " | ".join(short) + " |"]
        out += ["|---|" + "---|" * len(cols)]
        out.append("| p50 | " + " | ".join(f"{x['p50_ms']:.0f}" for x in lat) + " |")
        out.append("| p95 | " + " | ".join(f"{x['p95_ms']:.0f}" for x in lat) + " |")

    out += ["", "## Sub-50-token chunks as false hits", ""]
    out += [f"Chunks currently ingested under {SMALL} tokens: {sizes['n']}.", ""]
    out += small_chunks({n: runs[n] for n in names}, questions)
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=("run", "report"))
    args = ap.parse_args()
    if args.action == "run":
        run_all()
    else:
        OUT.parent.mkdir(exist_ok=True)
        old = OUT.read_text() if OUT.exists() else ""
        kept = old[old.index(FINDINGS) :] if FINDINGS in old else ""
        OUT.write_text(report() + ("\n" + kept if kept else ""))
        print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
