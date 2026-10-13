"""Initial schema: pgvector, 8 tables, HNSW and GIN indexes.

Revision ID: 0001
Revises:
Create Date: 2026-10-03
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

# text + CHECK instead of ENUM types: a CHECK is one statement to change, while
# enum values can never be removed. Documents and incidents use stable text ids
# (RB-012, INC-0042) because eval labels and agent citations point at them.


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.execute("""
        CREATE TABLE services (
            id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            name         text NOT NULL UNIQUE,
            owner        text,
            dependencies text[] NOT NULL DEFAULT '{}',  -- service names
            status       text NOT NULL DEFAULT 'healthy'
                         CHECK (status IN ('healthy', 'degraded', 'down')),
            updated_at   timestamptz NOT NULL DEFAULT now()
        )
    """)

    op.execute("""
        CREATE TABLE incidents (
            id          text PRIMARY KEY,
            service_id  bigint NOT NULL REFERENCES services (id),
            severity    text NOT NULL CHECK (severity IN ('sev1', 'sev2', 'sev3', 'sev4')),
            started_at  timestamptz NOT NULL,
            resolved_at timestamptz CHECK (resolved_at >= started_at),
            symptoms    text,
            root_cause  text,
            resolution  text,
            runbook_ids text[] NOT NULL DEFAULT '{}',
            synthetic   boolean NOT NULL DEFAULT true,
            metadata    jsonb NOT NULL DEFAULT '{}'
        )
    """)

    # No FK on service: LogHub background noise has services outside our table.
    # corpus is here too because injection payloads also live in log messages.
    op.execute("""
        CREATE TABLE log_events (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            ts          timestamptz NOT NULL,
            service     text NOT NULL,
            level       text NOT NULL CHECK (level IN ('DEBUG', 'INFO', 'WARN', 'ERROR', 'FATAL')),
            message     text NOT NULL,
            source      text NOT NULL,  -- 'synthetic' or 'loghub:<dataset>'
            incident_id text REFERENCES incidents (id),
            corpus      text NOT NULL DEFAULT 'main' CHECK (corpus IN ('main', 'injection'))
        )
    """)
    op.execute("CREATE INDEX log_events_service_ts ON log_events (service, ts)")

    op.execute("""
        CREATE TABLE documents (
            id           text PRIMARY KEY,
            doc_type     text NOT NULL CHECK (doc_type IN ('runbook', 'incident')),
            title        text NOT NULL,
            source_path  text,
            content_hash text NOT NULL,  -- lets re-ingestion skip unchanged docs
            corpus       text NOT NULL DEFAULT 'main' CHECK (corpus IN ('main', 'injection')),
            incident_id  text REFERENCES incidents (id),
            synthetic    boolean NOT NULL DEFAULT true,
            metadata     jsonb NOT NULL DEFAULT '{}',
            created_at   timestamptz NOT NULL DEFAULT now()
        )
    """)

    # Keyword search: the parser keeps `payments-db-01.prod.internal` as one `host`
    # token, but splits the query `payments-db-01` into other tokens, so partial
    # hostnames never matched. Both sides now also split on . _ / : - and every
    # keyword query must go through opspilot_tsquery. Two-argument to_tsvector with
    # 'simple': the one-argument form depends on a session setting (not immutable),
    # and simple neither stems nor drops error codes. RETURN bodies are parsed at
    # creation time, so they don't depend on search_path.
    op.execute("""
        CREATE FUNCTION opspilot_tsvector(content text) RETURNS tsvector
        LANGUAGE sql IMMUTABLE PARALLEL SAFE
        RETURN to_tsvector('simple'::regconfig, content)
            || to_tsvector('simple'::regconfig, regexp_replace(content, '[._/:-]', ' ', 'g'))
    """)
    op.execute("""
        CREATE FUNCTION opspilot_tsquery(query text) RETURNS tsquery
        LANGUAGE sql IMMUTABLE PARALLEL SAFE
        RETURN plainto_tsquery('simple'::regconfig, regexp_replace(query, '[._/:-]', ' ', 'g'))
    """)

    # tsv is generated so it can never drift from content or use another config.
    op.execute("""
        CREATE TABLE chunks (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            document_id text NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
            chunk_index integer NOT NULL,
            content     text NOT NULL,
            token_count integer NOT NULL,
            embedding   vector(384) NOT NULL,
            tsv         tsvector GENERATED ALWAYS AS (opspilot_tsvector(content)) STORED,
            metadata    jsonb NOT NULL DEFAULT '{}',
            UNIQUE (document_id, chunk_index)
        )
    """)
    # Cosine ops: only queries ordering by `embedding <=> q` use this index.
    # m and ef_construction are pgvector's defaults, written out to be visible.
    op.execute("""
        CREATE INDEX chunks_embedding_hnsw ON chunks
        USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)
    """)
    op.execute("CREATE INDEX chunks_tsv_gin ON chunks USING gin (tsv)")

    # investigation_id has no FK yet; the investigations table arrives in Phase 3.
    op.execute("""
        CREATE TABLE tool_calls (
            id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            investigation_id uuid NOT NULL,
            tool             text NOT NULL,
            input            jsonb NOT NULL,
            output_bytes     integer,
            latency_ms       double precision NOT NULL,
            success          boolean NOT NULL,
            error            text,
            created_at       timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX tool_calls_investigation_id ON tool_calls (investigation_id)")

    op.execute("""
        CREATE TABLE eval_runs (
            id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            git_commit text NOT NULL,
            eval_type  text NOT NULL CHECK (eval_type IN ('retrieval', 'generation', 'injection')),
            split      text NOT NULL CHECK (split IN ('dev', 'test', 'tune', 'heldout')),
            config     jsonb NOT NULL,
            models     jsonb NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now()
        )
    """)

    # metrics is jsonb because each eval type reports a different metric set.
    op.execute("""
        CREATE TABLE eval_results (
            id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            run_id      bigint NOT NULL REFERENCES eval_runs (id) ON DELETE CASCADE,
            question_id text NOT NULL,
            metrics     jsonb NOT NULL,
            UNIQUE (run_id, question_id)
        )
    """)


def downgrade() -> None:
    for table in [
        "eval_results",
        "eval_runs",
        "tool_calls",
        "chunks",
        "documents",
        "log_events",
        "incidents",
        "services",
    ]:
        op.execute(f"DROP TABLE IF EXISTS {table}")
    op.execute("DROP FUNCTION IF EXISTS opspilot_tsquery(text), opspilot_tsvector(text)")
    op.execute("DROP EXTENSION IF EXISTS vector")
