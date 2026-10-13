"""Schema tests: run migration 0001 against a throwaway opspilot_test database.

Needs Postgres on the host port (`make up`, then `make test`). If it is unreachable
these tests skip with a visible reason; the rest of the suite still runs.
"""

from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from psycopg import errors

from config import get_settings

pytestmark = pytest.mark.db

TEST_DB = "opspilot_test"
ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
TABLES = {
    "services",
    "incidents",
    "log_events",
    "documents",
    "chunks",
    "tool_calls",
    "eval_runs",
    "eval_results",
}
HOST_TEXT = "Connection to payments-db-01.prod.internal failed: ECONNREFUSED, upstream returned 503"


def with_db(url: str, dbname: str) -> str:
    return urlunsplit(urlsplit(url)._replace(path=f"/{dbname}"))


def vec(dim: int = 384, value: float = 0.1) -> str:
    return "[" + ",".join([str(value)] * dim) + "]"


@pytest.fixture(scope="module")
def alembic_cfg():
    base_url = get_settings().database_url
    try:
        admin = psycopg.connect(with_db(base_url, "postgres"), autocommit=True, connect_timeout=3)
    except psycopg.OperationalError as exc:
        host = urlsplit(base_url).netloc.split("@")[-1]
        reason = str(exc).splitlines()[0]
        pytest.skip(f"Postgres not reachable at {host} (run `make up`, then `make test`): {reason}")
    with admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")

    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["database_url"] = with_db(base_url, TEST_DB)
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    return cfg


@pytest.fixture
def conn(alembic_cfg):
    """One transaction per test, rolled back at the end so tests don't share rows."""
    with psycopg.connect(alembic_cfg.attributes["database_url"]) as connection:
        yield connection
        connection.rollback()


def add_document(conn, doc_id="RB-001", corpus="main"):
    conn.execute(
        "INSERT INTO documents (id, doc_type, title, content_hash, corpus)"
        " VALUES (%s, 'runbook', 'test', 'hash', %s)",
        (doc_id, corpus),
    )


def add_chunk(conn, doc_id="RB-001", index=0, content="text", embedding=None):
    conn.execute(
        "INSERT INTO chunks (document_id, chunk_index, content, token_count, embedding)"
        " VALUES (%s, %s, %s, 1, %s::vector)",
        (doc_id, index, content, embedding or vec()),
    )


def test_extension_and_all_tables_exist(conn):
    ext = conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'").fetchone()
    assert ext is not None
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ).fetchall()
    assert TABLES <= {r[0] for r in rows}


def test_migration_round_trips(alembic_cfg):
    command.downgrade(alembic_cfg, "base")
    with psycopg.connect(alembic_cfg.attributes["database_url"]) as c:
        left = c.execute(
            "SELECT table_name FROM information_schema.tables"
            " WHERE table_schema = 'public' AND table_name <> 'alembic_version'"
        ).fetchall()
        assert left == []
        assert c.execute("SELECT 1 FROM pg_extension WHERE extname = 'vector'").fetchone() is None
        funcs = c.execute("SELECT 1 FROM pg_proc WHERE proname LIKE 'opspilot_%'").fetchall()
        assert funcs == []
    command.upgrade(alembic_cfg, "head")


def test_embedding_dimension_is_enforced(conn):
    add_document(conn)
    add_chunk(conn, index=0, embedding=vec(384))
    with pytest.raises(errors.DataException, match="expected 384 dimensions"), conn.transaction():
        add_chunk(conn, index=1, embedding=vec(383))


def test_hnsw_index_uses_cosine_ops_and_serves_cosine_queries(conn):
    indexdef = conn.execute(
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'chunks_embedding_hnsw'"
    ).fetchone()[0]
    assert "USING hnsw (embedding vector_cosine_ops)" in indexdef

    conn.execute("SET LOCAL enable_seqscan = off")
    plan = conn.execute(
        f"EXPLAIN SELECT id FROM chunks ORDER BY embedding <=> '{vec()}'::vector LIMIT 5"
    ).fetchall()
    assert "chunks_embedding_hnsw" in "\n".join(r[0] for r in plan)


def test_tsv_column_is_generated_with_opspilot_tsvector(conn):
    expr = conn.execute(
        "SELECT pg_get_expr(d.adbin, d.adrelid) FROM pg_attrdef d"
        " JOIN pg_attribute a ON a.attrelid = d.adrelid AND a.attnum = d.adnum"
        " WHERE d.adrelid = 'chunks'::regclass AND a.attname = 'tsv'"
    ).fetchone()[0]
    assert expr == "opspilot_tsvector(content)"


def test_keyword_search_finds_error_codes_and_hostnames(conn):
    add_document(conn)
    add_chunk(conn, content=HOST_TEXT)

    tokens = conn.execute(
        "SELECT alias, token, lexemes FROM ts_debug('simple', %s) WHERE alias <> 'blank'",
        (HOST_TEXT,),
    ).fetchall()
    stored = conn.execute("SELECT tsv::text FROM chunks").fetchone()[0]
    print("\nts_debug('simple') on the raw text:")
    for alias, token, lexemes in tokens:
        print(f"  {alias:<12} {token!r:<34} -> {lexemes}")
    print(f"stored tsv: {stored}")

    def matches(q: str) -> bool:
        parsed, raw, hit = conn.execute(
            "SELECT opspilot_tsquery(%s)::text, plainto_tsquery('simple', %s)::text,"
            " EXISTS (SELECT 1 FROM chunks WHERE tsv @@ opspilot_tsquery(%s))",
            (q, q, q),
        ).fetchone()
        print(f"query {q!r:<32} opspilot_tsquery {parsed:<48} (raw {raw}) matches={hit}")
        return hit

    should_match = [
        "ECONNREFUSED",
        "503",
        "payments-db-01.prod.internal",
        "payments-db-01",  # partial hostname: the case the plain parser misses
        "prod.internal",
        "db-01",
    ]
    should_not_match = ["payments-db-02", "orders-db-01", "ETIMEDOUT"]
    missed = [q for q in should_match if not matches(q)]
    false_hits = [q for q in should_not_match if matches(q)]
    assert not missed, f"keyword search missed {missed}"
    assert not false_hits, f"keyword search wrongly matched {false_hits}"


def test_gin_index_serves_full_text_queries(conn):
    conn.execute("SET LOCAL enable_seqscan = off")
    plan = conn.execute(
        "EXPLAIN SELECT id FROM chunks WHERE tsv @@ opspilot_tsquery('payments-db-01')"
    ).fetchall()
    assert "chunks_tsv_gin" in "\n".join(r[0] for r in plan)


@pytest.mark.parametrize("table", ["documents", "log_events"])
def test_corpus_defaults_to_main_and_rejects_unknown(conn, table):
    if table == "documents":
        insert = (
            "INSERT INTO documents (id, doc_type, title, content_hash{col})"
            " VALUES ('RB-001', 'runbook', 't', 'h'{val}) RETURNING corpus"
        )
    else:
        insert = (
            "INSERT INTO log_events (ts, service, level, message, source{col})"
            " VALUES (now(), 'payment-api', 'ERROR', 'm', 'synthetic'{val}) RETURNING corpus"
        )
    assert conn.execute(insert.format(col="", val="")).fetchone()[0] == "main"
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute(insert.format(col=", corpus", val=", 'foo'"))


def test_eval_type_rejects_unknown(conn):
    insert = (
        "INSERT INTO eval_runs (git_commit, eval_type, split, config)"
        " VALUES ('abc', %s, 'dev', '{}')"
    )
    conn.execute(insert, ("retrieval",))
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute(insert, ("foo",))


def test_deleting_a_document_cascades_to_its_chunks(conn):
    add_document(conn)
    add_chunk(conn)
    conn.execute("DELETE FROM documents WHERE id = 'RB-001'")
    assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0


def test_incident_cannot_resolve_before_it_starts(conn):
    service_id = conn.execute(
        "INSERT INTO services (name) VALUES ('payment-api') RETURNING id"
    ).fetchone()[0]
    with pytest.raises(errors.CheckViolation), conn.transaction():
        conn.execute(
            "INSERT INTO incidents (id, service_id, severity, started_at, resolved_at)"
            " VALUES ('INC-0001', %s, 'sev2', now(), now() - interval '1 hour')",
            (service_id,),
        )
