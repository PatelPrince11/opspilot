"""Why hybrid_rerank does not beat dense on exact-token Recall@5 (dev only, diagnostic).

    CHUNK_SIZE=256 CHUNK_OVERLAP=32 uv run python -m eval.diagnose_ablation   # as ingested

Reads the ablation runs from eval_runs (eval.ablation.latest_runs) and the current chunks:
1. Where dense's exact-token misses come from (no gold vs a missing runbook), rank-1 counts,
   and the MRR difference with its paired-bootstrap CI.
2. Keyword lexeme frequencies for two failing questions.
3. A keyword leg with lexemes found in over 20% of chunks removed (a crude IDF cut), re-fused
   and re-ranked. Diagnostic only: it confirms the cause and is not a locked config.
4. Exact scan vs forced HNSW index scan: the same dense top 30 for every dev question?
Steps 1-3 use the original (ts_rank_cd era) runs. Step 5 uses the BM25 rerun:
5. Exact-token questions whose token-bearing gold doc is in hybrid's top 5 but not
   hybrid_rerank's, and the reverse; and the same for keyword vs hybrid_rerank.
"""

from statistics import mean

import psycopg

from config import get_settings
from eval.ablation import bootstrap, latest_runs
from eval.retrieval_eval import check_ingested, dedupe, score
from eval.validate_questions import QUESTIONS, load
from rag.retrieval import COLS, Hit, RetrievalConfig, dense, rerank, rrf

COMMON = 0.2  # share of chunks above which a lexeme is cut in step 3


def misses(runs, qs) -> None:
    ex = [i for i, q in qs.items() if q["difficulty"] == "exact-token"]
    for chunk in ("400/50", "256/32"):
        d = {r["id"]: r for r in runs.get(f"dense +prefix @{chunk}", [])}
        r = {x["id"]: x for x in runs.get(f"hybrid_rerank +prefix @{chunk}", [])}
        if not d or not r:
            continue
        miss = [i for i in ex if d[i]["recall@5"] < 1]
        none = sum(not set(qs[i]["relevant_doc_ids"]) & set(d[i]["docs"][:5]) for i in miss)
        rb = sum(
            any(g.startswith("RB-") for g in set(qs[i]["relevant_doc_ids"]) - set(d[i]["docs"][:5]))
            for i in miss
        )
        print(
            f"\n[{chunk}] dense exact-token questions with Recall@5 < 1: {len(miss)} of {len(ex)}"
        )
        print(f"  no gold doc in top 5: {none}; a runbook among the missing gold: {rb}")
        print(
            f"  gold at rank 1: dense {sum(d[i]['rr'] == 1 for i in ex)},"
            f" hybrid_rerank {sum(r[i]['rr'] == 1 for i in ex)} (of {len(ex)})"
        )
        for name, ids in (("exact-token", ex), ("overall", list(qs))):
            diff, lo, hi = bootstrap([r[i]["rr"] for i in ids], [d[i]["rr"] for i in ids])
            print(f"  MRR hybrid_rerank - dense, {name}: {diff:+.3f} [{lo:+.3f}, {hi:+.3f}]")


def lexemes(conn, query: str) -> list[str]:
    q = conn.execute("SELECT opspilot_or_tsquery(%s)::text", (query,)).fetchone()[0]
    return [x.strip(" '") for x in q.split("|")] if q else []


def chunk_freq(conn, lexeme: str) -> int:
    sql = "SELECT count(*) FROM chunks WHERE tsv @@ to_tsquery('simple', quote_literal(%s))"
    return conn.execute(sql, (lexeme,)).fetchone()[0]


def frequencies(conn, qs) -> None:
    n = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    for qid in ("Q003", "Q027"):
        q = qs[qid]
        print(f"\n{qid}: {q['question']}\n  gold {q['relevant_doc_ids']}; chunks containing:")
        print("  " + ", ".join(f"{x} {chunk_freq(conn, x)}" for x in lexemes(conn, q["question"])))
        gold = conn.execute(
            "SELECT document_id, chunk_index, (SELECT count(*) + 1 FROM chunks c2"
            "  WHERE ts_rank_cd(c2.tsv, opspilot_or_tsquery(%s))"
            "  > ts_rank_cd(c.tsv, opspilot_or_tsquery(%s)))"
            " FROM chunks c WHERE document_id = ANY(%s)"
            " ORDER BY ts_rank_cd(c.tsv, opspilot_or_tsquery(%s)) DESC LIMIT 1",
            (q["question"], q["question"], q["relevant_doc_ids"], q["question"]),
        ).fetchone()
        print(f"  best gold chunk {gold[0]}#{gold[1]} is keyword rank {gold[2]} of {n} chunks")


def idf_cut(conn, qs, runs) -> None:
    n = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    cfg = RetrievalConfig(mode="dense", bge_prefix=True, candidates=30)
    cut: dict[str, dict[str, float]] = {"keyword": {}, "hybrid": {}, "hybrid_rerank": {}}
    for q in qs.values():
        keep = [x for x in lexemes(conn, q["question"]) if chunk_freq(conn, x) < COMMON * n]
        kw = []
        if keep:
            tsq = " | ".join(f"'{x}'" for x in keep)
            rows = conn.execute(
                f"SELECT {COLS}, ts_rank_cd(c.tsv, to_tsquery('simple', %s)) AS r FROM chunks c"
                " WHERE c.tsv @@ to_tsquery('simple', %s) ORDER BY r DESC, c.id LIMIT 30",
                (tsq, tsq),
            ).fetchall()
            kw = [Hit(*r) for r in rows]
        with conn.transaction():
            hy = rrf(dense(conn, q["question"], cfg, 30), kw)[:30]
        for mode, hits in (
            ("keyword", kw),
            ("hybrid", hy),
            ("hybrid_rerank", rerank(q["question"], hy)),
        ):
            cut[mode][q["id"]] = score(dedupe(hits), q["relevant_doc_ids"])["recall@5"]
    chunk = f"{get_settings().chunk_size}/{get_settings().chunk_overlap}"
    base = {
        name: {r["id"]: r["recall@5"] for r in runs[f"{name} @{chunk}"]}
        for name in ("dense +prefix", "keyword", "hybrid +prefix", "hybrid_rerank +prefix")
    }
    print(f"\n[{chunk}] Recall@5 with lexemes in over {COMMON:.0%} of {n} chunks cut (diagnostic):")
    for tag in (None, "exact-token", "multi-doc", "paraphrase"):
        ids = [i for i, q in qs.items() if tag in (None, q["difficulty"])]
        cols = {**base, **{f"{m} (cut)": v for m, v in cut.items()}}
        print(
            f"  {tag or 'overall'} (n={len(ids)}): "
            + ", ".join(f"{m} {mean(v[i] for i in ids):.3f}" for m, v in cols.items())
        )
    ex = [i for i, q in qs.items() if q["difficulty"] == "exact-token"]
    diff, lo, hi = bootstrap(
        [cut["hybrid_rerank"][i] for i in ex], [base["dense +prefix"][i] for i in ex]
    )
    print(f"  exact-token hybrid_rerank (cut) - dense: {diff:+.3f} [{lo:+.3f}, {hi:+.3f}]")


def hnsw_check(conn, qs) -> None:
    cfg = RetrievalConfig(mode="dense", bge_prefix=True, candidates=30)
    same = 0
    for q in qs.values():
        with conn.transaction():
            exact = [h.chunk_id for h in dense(conn, q["question"], cfg, 30)]
        with conn.transaction():
            conn.execute("SET LOCAL enable_seqscan = off")  # forces the HNSW index scan
            same += exact == [h.chunk_id for h in dense(conn, q["question"], cfg, 30)]
    print(f"\nexact scan vs forced HNSW: identical top 30 for {same} of {len(qs)} dev questions")


def displaced(runs, qs) -> None:
    c = "@256/32"
    tok = {
        name: {
            r["id"]: r["token_doc@5"] for r in runs[f"{name} {c}"] if r["token_doc@5"] is not None
        }
        for name in ("keyword", "hybrid +prefix", "hybrid_rerank +prefix", "dense +prefix")
    }
    print(f"\n[BM25 rerun, 256/32] token-doc Recall@5 per question (n = {len(tok['keyword'])}):")
    for a in ("keyword", "hybrid +prefix", "dense +prefix"):
        b = "hybrid_rerank +prefix"
        worse = sorted(i for i in tok[b] if tok[b][i] < tok[a][i])
        better = sorted(i for i in tok[b] if tok[b][i] > tok[a][i])
        print(f"  rerank lower than {a} on {len(worse)} {worse}; higher on {len(better)} {better}")
    d = {r["id"]: r for r in runs[f"dense +prefix {c}"]}
    ex = [i for i, q in qs.items() if q["difficulty"] == "exact-token"]
    miss = [i for i in ex if d[i]["recall@5"] < 1]
    rb = sum(
        any(g.startswith("RB-") for g in set(qs[i]["relevant_doc_ids"]) - set(d[i]["docs"][:5]))
        for i in miss
    )
    print(f"  dense exact-token misses: {len(miss)} of {len(ex)}; a runbook among them: {rb}")
    r = {x["id"]: x for x in runs[f"hybrid_rerank +prefix {c}"]}
    miss = [i for i in ex if r[i]["recall@5"] < 1]
    rb = sum(
        any(g.startswith("RB-") for g in set(qs[i]["relevant_doc_ids"]) - set(r[i]["docs"][:5]))
        for i in miss
    )
    print(
        f"  hybrid_rerank exact-token misses: {len(miss)} of {len(ex)}; a runbook among them: {rb}"
    )


def main() -> None:
    qs = {q["id"]: q for q in load(QUESTIONS) if q["split"] == "dev"}
    with psycopg.connect(get_settings().database_url) as conn:
        check_ingested(conn)  # steps 2-4 read the chunks: CHUNK_SIZE/OVERLAP must match them
        runs = latest_runs(conn, bm25=False)
        misses(runs, qs)
        frequencies(conn, qs)
        idf_cut(conn, qs, runs)
        hnsw_check(conn, qs)
        displaced(latest_runs(conn, bm25=True), qs)


if __name__ == "__main__":
    main()
