"""log_events.corpus may be 'live': the live-incident eval's logs (eval/live_eval.py).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

# Rows with source 'live:<case id>' are visible only to a connection that set
# opspilot.injection_case to that id (agent/tools), as the injection payloads are.


def upgrade() -> None:
    op.execute("ALTER TABLE log_events DROP CONSTRAINT log_events_corpus_check")
    op.execute(
        "ALTER TABLE log_events ADD CONSTRAINT log_events_corpus_check"
        " CHECK (corpus IN ('main', 'injection', 'live'))"
    )


def downgrade() -> None:
    op.execute("DELETE FROM log_events WHERE corpus = 'live'")
    op.execute("ALTER TABLE log_events DROP CONSTRAINT log_events_corpus_check")
    op.execute(
        "ALTER TABLE log_events ADD CONSTRAINT log_events_corpus_check"
        " CHECK (corpus IN ('main', 'injection'))"
    )
