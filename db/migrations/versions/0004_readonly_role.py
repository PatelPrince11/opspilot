"""Read-only role opspilot_ro for the agent's tools.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-12
"""

from alembic import op

from config import get_settings

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Enforced by Postgres, not by tool code: the role has SELECT only, and its sessions default to
# read-only transactions as a second lock. Roles are cluster-wide, so the role may already exist
# (another database, e.g. the test DBs); the password is (re)set from READONLY_DB_PASSWORD.
# Default privileges make tables created later by the migrating role readable too.


def upgrade() -> None:
    pw = get_settings().readonly_db_password.get_secret_value().replace("'", "''")
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'opspilot_ro') THEN
                CREATE ROLE opspilot_ro LOGIN;
            END IF;
        END $$
    """)
    op.execute(f"ALTER ROLE opspilot_ro LOGIN PASSWORD '{pw}'")
    op.execute("ALTER ROLE opspilot_ro SET default_transaction_read_only = on")
    op.execute(
        "DO $$ BEGIN EXECUTE format('GRANT CONNECT ON DATABASE %I TO opspilot_ro',"
        " current_database()); END $$"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO opspilot_ro")
    op.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO opspilot_ro")
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO opspilot_ro")


def downgrade() -> None:
    # The role is cluster-wide and may hold grants in other databases, so it is not dropped.
    op.execute("ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE SELECT ON TABLES FROM opspilot_ro")
    op.execute("REVOKE SELECT ON ALL TABLES IN SCHEMA public FROM opspilot_ro")
    op.execute("REVOKE USAGE ON SCHEMA public FROM opspilot_ro")
    op.execute(
        "DO $$ BEGIN EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM opspilot_ro',"
        " current_database()); END $$"
    )
