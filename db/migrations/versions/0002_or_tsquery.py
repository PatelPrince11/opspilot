"""opspilot_or_tsquery: keyword retrieval's OR query (opspilot_tsquery is unchanged).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


# Same punctuation split as opspilot_tsquery, so `payments-db-01` becomes payments | db | 01
# and still meets the tsvector side's split tokens. The simple parser tokenises (lexemes are
# lowercased and de-duplicated); a lexeme is dropped only when the english_stem dictionary
# calls it a stopword (ts_lexize returns '{}'), so codes, numbers and hostname parts stay.
# Lexemes are ORed: a sentence ANDed together would need every word in one chunk.
# STABLE, not IMMUTABLE: ts_lexize reads a dictionary.
def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION opspilot_or_tsquery(query text) RETURNS tsquery
        LANGUAGE sql STABLE PARALLEL SAFE
        RETURN to_tsquery('simple'::regconfig, coalesce((
            SELECT string_agg(quote_literal(w), ' | ' ORDER BY w)
            FROM unnest(tsvector_to_array(to_tsvector('simple'::regconfig,
                regexp_replace(query, '[._/:-]', ' ', 'g')))) AS w
            WHERE ts_lexize('english_stem'::regdictionary, w) <> '{}'
        ), ''))
    """)


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS opspilot_or_tsquery(text)")
