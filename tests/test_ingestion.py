"""make ingest against a throwaway opspilot_ingest_test database (needs `make up`).

Embeds the whole corpus once on CPU (about 10 s), then ingests a second time.
"""

import json
import re
import tomllib

import psycopg
import pytest
from alembic import command
from alembic.config import Config

from config import get_settings
from rag.embeddings import embed
from rag.ingestion import LOG_FILES, ingest
from scripts.corpus.checks import label_errors, leaks_demo, load_sidecar, sidecar_labels
from scripts.corpus.common import PROCESSED, SIDECAR
from scripts.corpus.incidents import LABEL_RE
from tests.test_schema import ALEMBIC_INI, with_db

pytestmark = pytest.mark.db

TEST_DB = "opspilot_ingest_test"


def lines(name: str) -> int:
    return len((PROCESSED / name).read_text().splitlines())


def snapshot(conn) -> dict:
    q = {
        "counts": "SELECT (SELECT count(*) FROM services), (SELECT count(*) FROM incidents),"
        " (SELECT count(*) FROM log_events), (SELECT count(*) FROM documents),"
        " (SELECT count(*) FROM chunks)",
        "chunks": "SELECT id, document_id, chunk_index, md5(content), md5(embedding::text)"
        " FROM chunks ORDER BY id",
        "docs": "SELECT id, content_hash FROM documents ORDER BY id",
        "logs": "SELECT max(id), md5(string_agg(message || coalesce(incident_id, ''), '|'"
        " ORDER BY id)) FROM log_events",
        "bm25": "SELECT (SELECT md5(string_agg(lexeme || ':' || df, '|' ORDER BY lexeme))"
        " FROM lexeme_df), n_chunks, avg_len FROM bm25_stats",
    }
    return {k: conn.execute(v).fetchall() for k, v in q.items()}


@pytest.fixture(scope="module")
def db():
    if not (PROCESSED / "log_events.loghub.jsonl").exists():
        pytest.skip(
            "LogHub background lines are downloaded at build time, not committed: run"
            " `make corpus` (local-only data)"
        )
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
    with psycopg.connect(with_db(base_url, TEST_DB)) as conn:
        first = ingest(conn)
        conn.commit()
        before = snapshot(conn)
        second = ingest(conn)
        conn.commit()
        yield {"conn": conn, "first": first, "second": second, "before": before}


def test_row_counts_match_corpus_outputs(db):
    services = len(json.loads((PROCESSED / "services.json").read_text()))
    logs = sum(lines(f) for f in LOG_FILES)
    assert lines("incidents.jsonl") == 117 and lines("runbooks.jsonl") == 30
    assert db["before"]["counts"][0][:4] == (services, 117, logs, 147)
    by_type = dict(db["conn"].execute("SELECT doc_type, count(*) FROM documents GROUP BY 1"))
    assert by_type == {"incident": 117, "runbook": 30}
    with_incident = "SELECT count(*) FROM log_events WHERE incident_id IS NOT NULL"
    assert db["conn"].execute(with_incident).fetchone()[0] == lines(LOG_FILES[0])


def test_second_ingest_changes_nothing(db):
    assert db["first"]["embedded_docs"] == 147
    assert db["second"]["embedded_docs"] == 0
    assert snapshot(db["conn"]) == db["before"]


def test_no_sidecar_eval_or_demo_text_in_documents_or_chunks(db):
    conn = db["conn"]
    rows = conn.execute(
        "SELECT id || ' ' || title || ' ' || source_path || ' ' || metadata::text FROM documents"
        " UNION ALL SELECT content || ' ' || metadata::text FROM chunks"
    ).fetchall()
    texts = [r[0] for r in rows]
    everything = "\n".join(texts)

    assert not LABEL_RE.search(everything)
    runbooks = [json.loads(x) for x in (PROCESSED / "runbooks.jsonl").read_text().splitlines()]
    assert label_errors(everything, sidecar_labels(load_sidecar(), runbooks)) == []

    questions = [json.loads(x) for x in (SIDECAR.parent / "questions.jsonl").open()]
    low = everything.lower()
    assert not [q["id"] for q in questions if q["question"].lower() in low]
    for field in ("relevant_doc_ids", "expected_key_facts", "false_premise"):
        assert field not in everything

    demo = tomllib.loads((SIDECAR / "demo_answer.toml").read_text())
    assert "INC-LIVE-001" not in everything
    assert demo["question"] not in everything and demo["likely_cause"] not in everything
    assert not [t[:60] for t in texts if leaks_demo(t)]


def test_bm25_stats_match_a_direct_count(db):
    conn = db["conn"]
    direct = conn.execute(
        "SELECT u.lexeme, count(DISTINCT c.id) FROM chunks c, unnest(c.tsv) u GROUP BY 1"
    ).fetchall()
    assert dict(direct) == dict(conn.execute("SELECT lexeme, df FROM lexeme_df").fetchall())
    for word in ("payapi", "503", "01"):
        n = conn.execute(
            "SELECT count(*) FROM chunks WHERE tsv @@ to_tsquery('simple', %s)", (word,)
        ).fetchone()[0]
        assert (
            n == conn.execute("SELECT df FROM lexeme_df WHERE lexeme = %s", (word,)).fetchone()[0]
        )
    n, avg = conn.execute("SELECT n_chunks, avg_len FROM bm25_stats").fetchone()
    lens = conn.execute(
        "SELECT count(*), avg((SELECT sum(cardinality(u.positions)) FROM unnest(tsv) u))"
        " FROM chunks"
    ).fetchone()
    assert n == lens[0] and avg == pytest.approx(float(lens[1]))


def test_every_chunk_has_a_384_dim_unit_vector(db):
    bad = db["conn"].execute(
        "SELECT count(*) FROM chunks"
        " WHERE vector_dims(embedding) <> 384 OR abs(vector_norm(embedding) - 1) > 1e-3"
    )
    assert bad.fetchone()[0] == 0


@pytest.mark.parametrize("token", ["PAYAPI-E1042", "503", "LEDGER-W210"])
def test_keyword_search_finds_corpus_tokens(db, token):
    hits = (
        db["conn"]
        .execute("SELECT content FROM chunks WHERE tsv @@ opspilot_tsquery(%s)", (token,))
        .fetchall()
    )
    assert hits
    assert all(re.search(rf"(?<![\w-]){re.escape(token)}(?![\w-])", h[0]) for h in hits)


def test_short_hostname_finds_full_hostname(db):
    hits = (
        db["conn"]
        .execute("SELECT content FROM chunks WHERE tsv @@ opspilot_tsquery('payments-db-01')")
        .fetchall()
    )
    assert any("payments-db-01.prod.internal" in h[0] for h in hits)


def test_econnrefused_is_in_no_chunk(db):
    """Demo guard: only the live logs hold it (the schema test covers the tsvector side)."""
    n = db["conn"].execute(
        "SELECT count(*) FROM chunks"
        " WHERE content ILIKE '%econnrefused%' OR tsv @@ opspilot_tsquery('ECONNREFUSED')"
    )
    assert n.fetchone()[0] == 0


def test_dense_query_smoke(db):
    """Smoke only, not a metric: one fixed query, a sensible top document."""
    q = "ledger service JVM burning CPU because garbage collection keeps running"
    vec = "[" + ",".join(str(x) for x in embed([q])[0]) + "]"
    top = db["conn"].execute(
        "SELECT document_id FROM chunks ORDER BY embedding <=> %s::vector LIMIT 1", (vec,)
    )
    assert top.fetchone()[0] == "RB-001"


def test_changed_document_is_replaced(db):
    """Runs last: RB-001's hash goes stale, so only RB-001 is rebuilt."""
    conn = db["conn"]
    conn.execute("UPDATE documents SET content_hash = 'stale' WHERE id = 'RB-001'")
    stats = ingest(conn)
    conn.commit()
    assert stats["embedded_docs"] == 1
    after = snapshot(conn)
    old = [c for c in db["before"]["chunks"] if c[1] != "RB-001"]
    assert [c for c in after["chunks"] if c[1] != "RB-001"] == old
    new_ids = {c[0] for c in after["chunks"] if c[1] == "RB-001"}
    assert new_ids and not new_ids & {c[0] for c in db["before"]["chunks"]}
    assert after["docs"] == db["before"]["docs"]
