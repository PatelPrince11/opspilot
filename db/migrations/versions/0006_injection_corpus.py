"""BM25 corpus stats count the main corpus only, so injection payloads never shift them.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-13
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

# Injection payloads (eval/injection.jsonl, Phase 4) are chunks of documents with
# corpus = 'injection'. Retrieval filters them out of every result, but IDF and the average
# chunk length are corpus-wide, so counting them would still move the retrieval eval's BM25
# scores. An injection chunk's lexemes that only payloads contain get no IDF and add nothing
# to its score: the payload is ranked by the words it shares with the real corpus.


def refresh(where: str) -> str:
    chunks = f"chunks c JOIN documents d ON d.id = c.document_id{where}"
    return f"""
        CREATE OR REPLACE FUNCTION opspilot_bm25_refresh() RETURNS void
        LANGUAGE sql
        BEGIN ATOMIC
            DELETE FROM lexeme_df;
            INSERT INTO lexeme_df (lexeme, df)
                SELECT word, ndoc FROM ts_stat('SELECT c.tsv FROM {chunks.replace("'", "''")}');
            DELETE FROM bm25_stats;
            INSERT INTO bm25_stats (n_chunks, avg_len)
                SELECT count(*), coalesce(avg(len), 0)
                FROM (SELECT (SELECT sum(cardinality(u.positions)) FROM unnest(c.tsv) u) AS len
                      FROM {chunks}) AS x;
        END
    """


def upgrade() -> None:
    op.execute(refresh(" WHERE d.corpus = 'main'"))


def downgrade() -> None:
    op.execute(refresh(""))
