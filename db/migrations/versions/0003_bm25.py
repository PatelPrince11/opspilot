"""BM25 keyword ranking: lexeme document frequencies, corpus stats, opspilot_bm25(query).

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# tf and chunk length both count positions in chunks.tsv. opspilot_tsvector concatenates the
# plain parse and the punctuation-split parse, so an ordinary word gets two positions per
# occurrence while a hostname part gets one (the plain parse keeps the whole host as one token).
# Length and its corpus average are inflated the same way, so b's normalisation is consistent;
# df (and so IDF) counts chunks and is unaffected. Doubled words saturate under k1 a bit sooner.
#
# k1 = 1.2 and b = 0.75 are the standard defaults, fixed: not tuned on dev.

OR_TSQUERY_0002 = """
    CREATE OR REPLACE FUNCTION opspilot_or_tsquery(query text) RETURNS tsquery
    LANGUAGE sql STABLE PARALLEL SAFE
    RETURN to_tsquery('simple'::regconfig, coalesce((
        SELECT string_agg(quote_literal(w), ' | ' ORDER BY w)
        FROM unnest(tsvector_to_array(to_tsvector('simple'::regconfig,
            regexp_replace(query, '[._/:-]', ' ', 'g')))) AS w
        WHERE ts_lexize('english_stem'::regdictionary, w) <> '{}'
    ), ''))
"""


def upgrade() -> None:
    op.execute("CREATE TABLE lexeme_df (lexeme text PRIMARY KEY, df integer NOT NULL)")
    op.execute("""
        CREATE TABLE bm25_stats (
            id       boolean PRIMARY KEY DEFAULT true CHECK (id),  -- one row
            n_chunks integer NOT NULL,
            avg_len  double precision NOT NULL
        )
    """)

    # The query's lexemes, one place: the same split and stopword drop as 0002's OR query,
    # which is now built from this function (same output).
    op.execute("""
        CREATE FUNCTION opspilot_query_lexemes(query text) RETURNS SETOF text
        LANGUAGE sql STABLE PARALLEL SAFE
        BEGIN ATOMIC
            SELECT w FROM unnest(tsvector_to_array(to_tsvector('simple'::regconfig,
                regexp_replace(query, '[._/:-]', ' ', 'g')))) AS w
            WHERE ts_lexize('english_stem'::regdictionary, w) <> '{}';
        END
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION opspilot_or_tsquery(query text) RETURNS tsquery
        LANGUAGE sql STABLE PARALLEL SAFE
        RETURN to_tsquery('simple'::regconfig, coalesce((
            SELECT string_agg(quote_literal(w), ' | ' ORDER BY w)
            FROM opspilot_query_lexemes(query) AS w
        ), ''))
    """)

    # Rebuilt at the end of every ingest (rag/ingestion.py).
    op.execute("""
        CREATE FUNCTION opspilot_bm25_refresh() RETURNS void
        LANGUAGE sql
        BEGIN ATOMIC
            DELETE FROM lexeme_df;
            INSERT INTO lexeme_df (lexeme, df)
                SELECT word, ndoc FROM ts_stat('SELECT tsv FROM chunks');
            DELETE FROM bm25_stats;
            INSERT INTO bm25_stats (n_chunks, avg_len)
                SELECT count(*), coalesce(avg(len), 0)
                FROM (SELECT (SELECT sum(cardinality(u.positions)) FROM unnest(c.tsv) u) AS len
                      FROM chunks c) AS x;
        END
    """)

    # Candidates come from the GIN index (tsv @@ the OR query); every candidate is scored.
    # ponytail: scores all OR-matching chunks (hundreds here); add a cheap prefilter at scale.
    op.execute("""
        CREATE FUNCTION opspilot_bm25(query text)
        RETURNS TABLE (chunk_id bigint, score double precision)
        LANGUAGE sql STABLE PARALLEL SAFE
        BEGIN ATOMIC
            WITH cand AS (
                SELECT c.id, c.tsv,
                       (SELECT sum(cardinality(u.positions)) FROM unnest(c.tsv) u) AS len
                FROM chunks c
                WHERE c.tsv @@ opspilot_or_tsquery(query)
            )
            SELECT cand.id, sum(
                ln(1 + (s.n_chunks - d.df + 0.5)::float8 / (d.df + 0.5))
                * t.tf * (1.2 + 1)
                / (t.tf + 1.2 * (1 - 0.75 + 0.75 * cand.len / s.avg_len))
            )
            FROM cand
            CROSS JOIN bm25_stats s
            CROSS JOIN LATERAL (
                SELECT u.lexeme, cardinality(u.positions) AS tf FROM unnest(cand.tsv) u
            ) t
            JOIN lexeme_df d ON d.lexeme = t.lexeme
            WHERE t.lexeme IN (SELECT opspilot_query_lexemes(query))
            GROUP BY cand.id;
        END
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS opspilot_bm25(text), opspilot_bm25_refresh()")
    op.execute(OR_TSQUERY_0002)
    op.execute("DROP FUNCTION IF EXISTS opspilot_query_lexemes(text)")
    op.execute("DROP TABLE IF EXISTS bm25_stats, lexeme_df")
