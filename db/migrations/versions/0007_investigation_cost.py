"""investigations.cost_usd: what an investigation's LLM calls actually cost.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-14
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# The sum of cost_usd over the calls that were made (cache hits cost nothing again); per-call
# rows, cached flags included, stay in llm_calls. Tokens already have their own columns.


def upgrade() -> None:
    op.execute("ALTER TABLE investigations ADD COLUMN cost_usd double precision")


def downgrade() -> None:
    op.execute("ALTER TABLE investigations DROP COLUMN cost_usd")
