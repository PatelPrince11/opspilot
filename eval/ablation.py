"""Phase 2 retrieval ablation on dev -> eval/results/ablation.md.

The original ablation (keyword = ts_rank_cd, both chunk sizes) is reported from runs stored
before BM25; the BM25 rerun (locked 256/32 chunks) from runs whose config records
keyword_ranker = bm25. Today's `run` produces the latter:

    make ingest   # locked 256/32
    uv run python -m eval.ablation run
    uv run python -m eval.ablation report

The original runs were made with:

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
    token_gold,
)
from eval.validate_questions import QUESTIONS, Corpus, load
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
    ("keyword_tsrank", None),
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
        tg = token_gold(questions, Corpus())
        for mode, prefix in CONFIGS:
            cfg = RetrievalConfig(mode=mode, bge_prefix=bool(prefix), candidates=30)
            rows = run(conn, questions, cfg, tg)
            run_id = store(conn, "dev", cfg, rows)
            conn.commit()
            m = metrics(rows)
            print(
                f"run {run_id}: {label(mode, prefix, f'{s.chunk_size}/{s.chunk_overlap}')}"
                f" R@5 {m['recall@5']:.3f} p95 {latency(rows)['p95_ms']:.0f} ms"
            )


def latest_runs(conn, bm25: bool = True) -> dict[str, list[dict]]:
    """{label: per-question rows} for the newest dev run of each config and chunk size, from
    the BM25-era runs (bm25=True) or the ones before it, whose keyword leg was ts_rank_cd."""
    runs = conn.execute(
        "SELECT id, config FROM eval_runs WHERE eval_type = 'retrieval' AND split = 'dev'"
        " AND (config->>'candidates')::int = 30 AND (config ? 'keyword_ranker') = %s"
        " AND config->>'doc_type' IS NULL ORDER BY id",
        (bm25,),
    ).fetchall()
    newest = {}
    for run_id, c in runs:
        prefix = None if c["mode"].startswith("keyword") else c["bge_prefix"]
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


DIFFICULTIES = ("exact-token", "multi-doc", "paraphrase")


def ci_rows(
    runs, questions, a: str, b: str, key: str = "recall@5", slices=(None, *DIFFICULTIES)
) -> list[str]:
    """One table row per slice (None = overall): paired bootstrap of key, A minus B, over the
    questions where both runs have a value."""
    by_id = {q["id"]: q for q in questions}
    ra = {r["id"]: r.get(key) for r in runs[a]}
    rb = {r["id"]: r.get(key) for r in runs[b]}
    out = []
    for sl in slices:
        ids = sorted(
            i
            for i in ra
            if ra[i] is not None and rb.get(i) is not None and sl in (None, by_id[i]["difficulty"])
        )
        if not ids:
            raise ValueError(f"{a} vs {b}: no questions have {key!r} in both runs")
        diff, lo, hi = bootstrap([ra[i] for i in ids], [rb[i] for i in ids])
        claim = "yes" if lo > 0 or hi < 0 else "no"
        name = "overall" if sl is None else f"difficulty={sl}"
        metric = "MRR (rr)" if key == "rr" else key
        out.append(
            f"| {a} vs {b} | {metric} | {name} | {len(ids)} | {diff:+.3f}"
            f" | [{lo:+.3f}, {hi:+.3f}] | {claim} |"
        )
    return out


CI_HEAD = [
    "| A vs B | metric | slice | n | mean A-B | 95% CI | claim |",
    "|---|---|---|---|---|---|---|",
]


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


def per_tag(names, summaries, chunk: str, keys) -> list[str]:
    cols = [n for n in names if n.endswith(f"@{chunk}")]
    short = [c.removesuffix(f" @{chunk}") for c in cols]
    slices = [s for s in summaries[cols[0]] if s != "latency"]
    out = []
    for key, title, n_key in keys:
        out += [f"**{title}**", ""]
        out += ["| slice | n | " + " | ".join(short) + " |", "|---|---|" + "---|" * len(cols)]
        for sl in slices:
            n = summaries[cols[0]][sl][n_key]
            if not n:
                continue
            vals = [summaries[c][sl][key] for c in cols]
            out.append(f"| {sl} | {n} | " + " | ".join(f"{v:.3f}" for v in vals) + " |")
        out.append("")
    lat = [summaries[c]["latency"] for c in cols]
    out += ["**Latency (ms)**", "", "| | " + " | ".join(short) + " |"]
    out += ["|---|" + "---|" * len(cols)]
    out.append("| p50 | " + " | ".join(f"{x['p50_ms']:.0f}" for x in lat) + " |")
    out.append("| p95 | " + " | ".join(f"{x['p95_ms']:.0f}" for x in lat) + " |")
    return out


BASE_KEYS = [(k, t, "n") for k, t in METRICS] + [("all_gold@10", "all-gold Recall@10", "n_multi")]
DIAG_KEYS = [
    ("token_doc@5", "token-doc Recall@5 (diagnostic)", "n_token"),
    ("agent_recall@10", "agent view Recall@10 (diagnostic)", "n"),
    ("agent_all_gold@10", "agent view all-gold Recall@10 (diagnostic)", "n_multi"),
]


def original_section(runs, questions) -> list[str]:
    names = sorted(runs, key=lambda n: (n.split(" @")[1], n))
    summaries = {n: summarize(runs[n], questions) for n in names}
    out = [
        "## Original ablation (keyword = ts_rank_cd, before BM25)",
        "",
        "In this section `keyword`, `hybrid` and `hybrid_rerank` use the ts_rank_cd keyword"
        " ranker (now mode `keyword_tsrank`).",
        "",
        "### Headline (overall, n = 100)",
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
    out += ["", "### Key comparisons", "", *CI_HEAD]
    for chunk in ("400/50", "256/32"):
        for a, b in [
            ("hybrid_rerank +prefix", "dense +prefix"),
            ("hybrid +prefix", "dense +prefix"),
            ("hybrid_rerank +prefix", "hybrid +prefix"),
            ("keyword", "dense +prefix"),
            ("dense +prefix", "dense -prefix"),
            ("hybrid +prefix", "hybrid -prefix"),
            ("hybrid_rerank +prefix", "hybrid_rerank -prefix"),
        ]:
            a, b = f"{a} @{chunk}", f"{b} @{chunk}"
            if a in runs and b in runs:
                out += ci_rows(runs, questions, a, b)
    for mode in ("dense +prefix", "keyword", "hybrid +prefix", "hybrid_rerank +prefix"):
        a, b = f"{mode} @256/32", f"{mode} @400/50"
        if a in runs and b in runs:
            out += ci_rows(runs, questions, a, b)
    for chunk in ("400/50", "256/32"):
        out += ["", f"### Per tag, chunks {chunk}", ""]
        out += per_tag(names, summaries, chunk, BASE_KEYS)
    out += ["", "### Sub-50-token chunks as false hits", ""]
    out += small_chunks({n: runs[n] for n in names}, questions)
    return out


def bm25_section(runs, old, questions) -> list[str]:
    names = sorted(runs)
    summaries = {n: summarize(runs[n], questions) for n in names}
    c = "@256/32"
    out = [
        "## BM25 rerun (locked chunks 256/32)",
        "",
        "`keyword`, `hybrid` and `hybrid_rerank` now use BM25 (k1 1.2, b 0.75, fixed);"
        " `keyword_tsrank` is the old ranker. token-doc Recall@5 and the agent view are"
        " diagnostics (definitions in eval/retrieval_eval.py), not done-checks.",
        "",
        "### Headline (overall, n = 100)",
        "",
        "| config | Recall@5 | Recall@10 | MRR | all-gold R@10 (n) | token-doc R@5 (n)"
        " | agent R@10 | agent all-gold R@10 | p50 ms | p95 ms |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for n in names:
        m, lat = summaries[n]["overall"], summaries[n]["latency"]
        out.append(
            f"| {n} | {m['recall@5']:.3f} | {m['recall@10']:.3f} | {m['mrr']:.3f}"
            f" | {m['all_gold@10']:.3f} ({m['n_multi']}) | {m['token_doc@5']:.3f}"
            f" ({m['n_token']}) | {m['agent_recall@10']:.3f} | {m['agent_all_gold@10']:.3f}"
            f" | {lat['p50_ms']:.0f} | {lat['p95_ms']:.0f} |"
        )
    # The BM25-era runs plus the ts_rank-era hybrid runs at 256/32, for before/after rows.
    both = runs | {f"{k} [ts_rank era]": v for k, v in old.items() if k.endswith(c)}
    rows = [
        ("keyword", "keyword_tsrank", "recall@5", (None, *DIFFICULTIES)),
        ("keyword", "keyword_tsrank", "token_doc@5", (None,)),
        ("keyword", "keyword_tsrank", "rr", (None,)),
        ("keyword", "dense +prefix", "recall@5", (None, "exact-token")),
        ("keyword", "dense +prefix", "rr", (None, "exact-token")),
        ("keyword", "dense +prefix", "all_gold@10", (None,)),
        ("hybrid_rerank +prefix", "dense +prefix", "recall@5", (None, "exact-token")),
        ("hybrid_rerank +prefix", "dense +prefix", "token_doc@5", (None,)),
        ("hybrid_rerank +prefix", "dense +prefix", "rr", (None, "exact-token")),
        ("hybrid_rerank +prefix", "dense +prefix", "agent_recall@10", (None,)),
        ("hybrid_rerank +prefix", "dense +prefix", "agent_all_gold@10", (None,)),
        ("hybrid +prefix", "dense +prefix", "recall@5", (None, "exact-token", "paraphrase")),
        ("hybrid +prefix", "dense +prefix", "rr", (None,)),
        ("hybrid +prefix", "dense +prefix", "token_doc@5", (None,)),
        ("hybrid_rerank +prefix", "hybrid +prefix", "recall@5", (None,)),
        ("hybrid_rerank +prefix", "hybrid +prefix", "rr", (None,)),
        ("keyword", "hybrid_rerank +prefix", "recall@5", (None, *DIFFICULTIES)),
        ("keyword", "hybrid_rerank +prefix", "rr", (None, *DIFFICULTIES)),
        ("keyword", "hybrid +prefix", "recall@5", (None, *DIFFICULTIES)),
        ("keyword", "hybrid +prefix", "rr", (None, *DIFFICULTIES)),
        ("dense +prefix", "dense -prefix", "recall@5", (None,)),
        ("hybrid +prefix", "hybrid -prefix", "recall@5", (None,)),
        ("hybrid_rerank +prefix", "hybrid_rerank -prefix", "recall@5", (None,)),
        ("hybrid_rerank +prefix", "hybrid_rerank -prefix", "rr", (None,)),
    ]
    out += ["", "### Key comparisons", "", *CI_HEAD]
    for a, b, key, slices in rows:
        a, b = f"{a} {c}", f"{b} {c}"
        if a in both and b in both:
            out += ci_rows(both, questions, a, b, key, slices)
    for mode in ("hybrid +prefix", "hybrid_rerank +prefix"):
        a, b = f"{mode} {c}", f"{mode} {c} [ts_rank era]"
        if a in both and b in both:
            out += ci_rows(both, questions, a, b, "recall@5", (None, "exact-token"))
    out += ["", "### Per tag", ""]
    out += per_tag(names, summaries, "256/32", BASE_KEYS + DIAG_KEYS)
    return out


def report() -> str:
    questions = [q for q in load(QUESTIONS) if q["split"] == "dev"]
    with psycopg.connect(get_settings().database_url) as conn:
        old, new = latest_runs(conn, bm25=False), latest_runs(conn, bm25=True)
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
    ]
    if new:
        out += bm25_section(new, old, questions) + [""]
    if old:
        out += original_section(old, questions)
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
