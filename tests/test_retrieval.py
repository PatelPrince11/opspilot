"""rag/retrieval.py on a tiny fixture DB, opspilot_retrieval_test (needs `make up`)."""

import json
from datetime import date

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from config import get_settings
from rag.embeddings import as_pgvector, embed
from rag.retrieval import Hit, RetrievalConfig, retrieve, rrf
from tests.test_schema import ALEMBIC_INI, with_db

pytestmark = pytest.mark.db

TEST_DB = "opspilot_retrieval_test"

# (doc id, doc type, severity, date, services, chunk texts)
DOCS = [
    (
        "RB-901",
        "runbook",
        None,
        "2025-12-01",
        ["redis"],
        [
            "Redis eviction storm: when redis-01 evicts keys under memory pressure, raise"
            " maxmemory and check the eviction policy."
        ],
    ),
    (
        "INC-9001",
        "incident",
        "sev1",
        "2026-01-05",
        ["payments-api"],
        [
            "payments-api returned PAYAPI-E1042 to every checkout after the 14:00 deploy; the"
            " connection pool was exhausted."
        ],
    ),
    (
        "INC-9002",
        "incident",
        "sev3",
        "2026-03-01",
        ["logging"],
        [
            "The disk on logs-01 filled up because log rotation stopped; the node went read-only.",
            "The team deleted old archives and restarted logrotate on logs-01.",
        ],
    ),
]


def load_fixture(conn) -> None:
    conn.execute("INSERT INTO services (name) VALUES ('payments-api'), ('logging')")
    for doc_id, doc_type, sev, day, services, texts in DOCS:
        inc = None
        if doc_type == "incident":
            inc = doc_id
            conn.execute(
                "INSERT INTO incidents (id, service_id, severity, started_at) VALUES"
                " (%s, (SELECT id FROM services WHERE name = %s), %s, %s)",
                (doc_id, services[0], sev, day),
            )
        conn.execute(
            "INSERT INTO documents (id, doc_type, title, content_hash, incident_id)"
            " VALUES (%s, %s, %s, 'x', %s)",
            (doc_id, doc_type, doc_id, inc),
        )
        meta = {"doc_type": doc_type, "services": services, "date": day}
        for i, (text, vec) in enumerate(zip(texts, embed(texts), strict=True)):
            conn.execute(
                "INSERT INTO chunks (document_id, chunk_index, content, token_count, embedding,"
                " metadata) VALUES (%s, %s, %s, 0, %s::vector, %s)",
                (doc_id, i, text, as_pgvector(vec), json.dumps(meta)),
            )


@pytest.fixture(scope="module")
def conn():
    base_url = get_settings().database_url
    try:
        admin = psycopg.connect(with_db(base_url, "postgres"), autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable (run `make up`): {str(exc).splitlines()[0]}")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["database_url"] = with_db(base_url, TEST_DB)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    with psycopg.connect(with_db(base_url, TEST_DB), autocommit=True) as c:
        load_fixture(c)
        c.execute("SELECT opspilot_bm25_refresh()")
        yield c


def docs(hits: list[Hit]) -> list[str]:
    return [h.document_id for h in hits]


def test_or_tsquery_drops_stopwords_keeps_codes_numbers_hosts(conn):
    q = conn.execute("SELECT opspilot_or_tsquery('What is PAYAPI-E1042 on payments-db-01?')")
    assert q.fetchone()[0] == "'01' | 'db' | 'e1042' | 'payapi' | 'payments'"
    q = conn.execute("SELECT opspilot_or_tsquery('what is the') = ''::tsquery")
    assert q.fetchone()[0] is True


def test_keyword_is_or_not_and(conn):
    q = "why did checkout fail with PAYAPI-E1042 yesterday at lunch"
    anded = conn.execute("SELECT count(*) FROM chunks WHERE tsv @@ opspilot_tsquery(%s)", (q,))
    assert anded.fetchone()[0] == 0  # the whole sentence ANDed matches nothing
    hits = retrieve(q, RetrievalConfig(mode="keyword", k=5), conn)
    assert docs(hits)[0] == "INC-9001"
    assert hits == sorted(hits, key=lambda h: -h.score)


def test_bm25_rare_term_outranks_common_term(conn):
    """X and Y have the same length and one query term each; X's is rare (df 1), Y's is in 4
    chunks. BM25 ranks X first; ts_rank_cd, which has no IDF, scores them the same."""
    texts = {
        "RB-990": "zebracode filler words here",
        "RB-991": "commonword filler words here",
        "RB-992": "commonword other",
        "RB-993": "commonword other",
        "RB-994": "commonword other",
    }
    with conn.transaction(force_rollback=True):
        for doc_id, text in texts.items():
            conn.execute(
                "INSERT INTO documents (id, doc_type, title, content_hash) VALUES"
                " (%s, 'runbook', %s, 'x')",
                (doc_id, doc_id),
            )
            conn.execute(
                "INSERT INTO chunks (document_id, chunk_index, content, token_count, embedding,"
                " metadata) VALUES (%s, 0, %s, 0, %s::vector, %s)",
                (doc_id, text, as_pgvector(embed([text])[0]), '{"doc_type": "runbook"}'),
            )
        conn.execute("SELECT opspilot_bm25_refresh()")
        q = "zebracode commonword"
        bm25 = retrieve(q, RetrievalConfig(mode="keyword", k=10), conn)
        score = {h.document_id: h.score for h in bm25}
        assert docs(bm25)[0] == "RB-990"
        assert score["RB-990"] > score["RB-991"]  # same length: only the IDF differs
        tsrank = {
            h.document_id: h.score
            for h in retrieve(q, RetrievalConfig(mode="keyword_tsrank", k=10), conn)
        }
        assert tsrank["RB-990"] == tsrank["RB-991"]


def test_dense_finds_a_paraphrase(conn):
    q = "keys being thrown out of the cache because it ran out of memory"
    for prefix in (True, False):
        hits = retrieve(q, RetrievalConfig(mode="dense", k=4, bge_prefix=prefix), conn)
        assert docs(hits)[0] == "RB-901"
        assert len(hits) == 4  # every chunk, in cosine order
        assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


@pytest.mark.parametrize("mode", ["dense", "keyword", "keyword_tsrank", "hybrid", "hybrid_rerank"])
@pytest.mark.parametrize(
    ("flt", "want"),
    [
        ({"service": "logging"}, {"INC-9002"}),
        ({"doc_type": "runbook"}, {"RB-901"}),
        ({"severity": "sev1"}, {"INC-9001"}),
        ({"since": date(2026, 2, 1)}, {"INC-9002"}),
        ({"until": date(2026, 1, 5)}, {"RB-901", "INC-9001"}),
        ({"since": date(2026, 1, 1), "until": date(2026, 1, 31)}, {"INC-9001"}),
    ],
)
def test_filters(conn, mode, flt, want):
    q = "PAYAPI-E1042 redis-01 logs-01 logrotate"  # every doc matches by keyword
    hits = retrieve(q, RetrievalConfig(mode=mode, k=10, **flt), conn)
    assert set(docs(hits)) == want


def test_rrf_sums_reciprocal_ranks():
    def hit(i):
        return Hit(i, f"D{i}", 0, "", {}, 0.0)

    fused = rrf([hit(1), hit(2), hit(3)], [hit(3), hit(1)], k=60)
    assert [h.chunk_id for h in fused] == [1, 3, 2]
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)
    assert fused[1].score == pytest.approx(1 / 63 + 1 / 61)
    assert fused[2].score == pytest.approx(1 / 62)


def test_hybrid_rerank_rescores_and_sorts(conn):
    q = "checkout errors from the payments API after a deploy"
    hits = retrieve(q, RetrievalConfig(mode="hybrid_rerank", k=3), conn)
    assert docs(hits)[0] == "INC-9001"
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)
    fused = retrieve(q, RetrievalConfig(mode="hybrid", k=3), conn)
    assert {h.score for h in hits} != {h.score for h in fused}  # cross-encoder scores, not RRF


def test_ef_search_is_at_least_the_candidate_depth(conn):
    with conn.transaction():
        retrieve("redis", RetrievalConfig(mode="dense", candidates=120), conn)
        assert conn.execute("SHOW hnsw.ef_search").fetchone()[0] == "120"
        assert conn.execute("SHOW hnsw.iterative_scan").fetchone()[0] == "strict_order"


def test_unknown_mode_is_refused(conn):
    with pytest.raises(ValueError, match="mode"):
        retrieve("x", RetrievalConfig(mode="bm25"), conn)
