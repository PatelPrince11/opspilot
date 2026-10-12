"""eval/retrieval_eval.py: metric maths, the test-split guard, a stored run on the fixture DB."""

import pytest

from eval.ablation import bootstrap
from eval.retrieval_eval import check_split, dedupe, run, score, store, summarize
from rag.retrieval import Hit, RetrievalConfig
from tests.test_retrieval import conn  # noqa: F401  (module fixture: the tiny DB)


def hit(doc: str) -> Hit:
    return Hit(0, doc, 0, "", {}, 0.0)


def test_dedupe_keeps_each_docs_best_rank():
    assert dedupe([hit("A"), hit("B"), hit("A"), hit("C"), hit("B")]) == ["A", "B", "C"]


def test_score():
    s = score(["B", "A", "X", "Y", "Z", "C"], ["A", "C"])
    assert s == {"recall@5": 0.5, "recall@10": 1.0, "rr": 0.5, "all_gold@10": 1.0}
    s = score(["X", "Y"], ["A"])
    assert s == {"recall@5": 0.0, "recall@10": 0.0, "rr": 0.0, "all_gold@10": None}
    assert score(["A"] + ["X"] * 9 + ["C"], ["A", "C"])["all_gold@10"] == 0.0


def q(i, difficulty, gold, author="claude", fp=None):
    return {
        "id": f"Q{i}",
        "difficulty": difficulty,
        "author": author,
        "relevant_doc_ids": gold,
        "false_premise": fp,
    }


def test_summarize_slices_carry_their_sample_size():
    questions = [
        q(1, "exact-token", ["A"]),
        q(2, "exact-token", ["A", "B"]),
        q(3, "paraphrase", ["A", "B"], author="gemini", fp=True),
    ]
    rows = [
        {"id": "Q1", "recall@5": 1.0, "recall@10": 1.0, "rr": 1.0, "all_gold@10": None},
        {"id": "Q2", "recall@5": 0.5, "recall@10": 1.0, "rr": 0.5, "all_gold@10": 1.0},
        {"id": "Q3", "recall@5": 0.0, "recall@10": 0.5, "rr": 0.0, "all_gold@10": 0.0},
    ]
    for i, r in enumerate(rows):
        r["latency_ms"] = 10.0 * (i + 1)
    s = summarize(rows, questions)
    assert s["overall"]["n"] == 3 and s["overall"]["recall@5"] == pytest.approx(0.5)
    assert s["overall"]["all_gold@10"] == 0.5 and s["overall"]["n_multi"] == 2
    assert s["difficulty=exact-token"]["n"] == 2
    assert s["difficulty=exact-token"]["mrr"] == pytest.approx(0.75)
    assert s["author=gemini"]["n"] == 1 and s["false_premise=True"]["n"] == 1
    assert s["gold bucket=1"]["all_gold@10"] is None
    assert s["latency"]["p50_ms"] == 20.0


def test_test_split_needs_final_test(capsys):
    check_split("dev", False)
    with pytest.raises(SystemExit, match="final-test"):
        check_split("test", False)
    check_split("test", True)
    assert "touched once, in Phase 5" in capsys.readouterr().err


def test_run_and_store_on_the_fixture_db(conn):  # noqa: F811
    questions = [
        q(1, "exact-token", ["INC-9001"]),
        q(2, "paraphrase", ["RB-901", "INC-9002"]),
    ]
    questions[0]["question"] = "what does PAYAPI-E1042 mean for checkout"
    questions[1]["question"] = "cache keys evicted and a full disk on logs-01"
    cfg = RetrievalConfig(mode="hybrid", bge_prefix=True, candidates=30)
    rows = run(conn, questions, cfg)
    assert rows[0]["rr"] == 1.0 and rows[0]["docs"][0] == "INC-9001"
    assert rows[1]["recall@10"] == 1.0  # the fixture has only 3 docs
    assert all(r["latency_ms"] > 0 and r["top_chunks"] for r in rows)

    with conn.transaction():
        run_id = store(conn, "dev", cfg, rows)
        stored = conn.execute(
            "SELECT git_commit, eval_type, split, config, models, created_at FROM eval_runs"
            " WHERE id = %s",
            (run_id,),
        ).fetchone()
        commit, eval_type, split, config, models, created = stored
        assert len(commit.removesuffix("-dirty")) == 40
        assert (eval_type, split) == ("retrieval", "dev")
        assert config["mode"] == "hybrid" and config["chunk_size"] and config["k"] == 30
        assert "@" in models["embed"] and "@" in models["rerank"] and created
        n = conn.execute("SELECT count(*) FROM eval_results WHERE run_id = %s", (run_id,))
        assert n.fetchone()[0] == 2


def test_paired_bootstrap_ci():
    assert bootstrap([1.0] * 50, [1.0] * 50) == (0.0, 0.0, 0.0)
    assert bootstrap([1.0] * 50, [0.0] * 50) == (1.0, 1.0, 1.0)
    a = [1.0] * 30 + [0.0] * 70  # A wins 30 questions, B wins 10, 60 ties
    b = [0.0] * 30 + [1.0] * 10 + [0.0] * 60
    diff, lo, hi = bootstrap(a, b)
    assert diff == pytest.approx(0.2) and 0 < lo < 0.2 < hi
    assert bootstrap(a, b) == (diff, lo, hi)  # seeded
    diff, lo, hi = bootstrap(a[:20] + [0.0] * 80, [0.0] * 20 + [1.0] * 20 + [0.0] * 60)
    assert diff == 0.0 and lo < 0 < hi
