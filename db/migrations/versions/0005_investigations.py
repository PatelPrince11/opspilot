"""investigations table; tool_calls gets its FK and an output token estimate.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-12
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

# One row per POST /investigate. llm_calls holds every LLM call's token counts (provider usage,
# or the estimate, flagged); tool_calls rows are written as each call finishes, so a run that
# dies midway keeps its trail. output_tokens is the estimated size of what a tool put in context.


def upgrade() -> None:
    op.execute("""
        CREATE TABLE investigations (
            id                uuid PRIMARY KEY,
            question          text NOT NULL,
            as_of             timestamptz NOT NULL,
            llm               text NOT NULL,  -- provider:model
            verify_enabled    boolean NOT NULL,
            status            text NOT NULL DEFAULT 'running'
                              CHECK (status IN ('running', 'done', 'error')),
            diagnosis         jsonb,
            llm_calls         jsonb NOT NULL DEFAULT '[]',
            loops             integer,
            prompt_tokens     integer,
            completion_tokens integer,
            error             text,
            created_at        timestamptz NOT NULL DEFAULT now(),
            finished_at       timestamptz
        )
    """)
    op.execute("ALTER TABLE tool_calls ADD COLUMN output_tokens integer")
    op.execute("""
        ALTER TABLE tool_calls ADD CONSTRAINT tool_calls_investigation_id_fkey
        FOREIGN KEY (investigation_id) REFERENCES investigations (id) ON DELETE CASCADE
    """)


def downgrade() -> None:
    op.execute("ALTER TABLE tool_calls DROP CONSTRAINT tool_calls_investigation_id_fkey")
    op.execute("ALTER TABLE tool_calls DROP COLUMN output_tokens")
    op.execute("DROP TABLE investigations")
