"""Alembic environment: connects to Postgres and runs migrations online.

Migrations are hand-written SQL (no ORM models, no autogenerate), so there is no
target_metadata. The URL comes from config.py unless a caller (the tests) passes one.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from config import get_settings

config = context.config

# Tests call alembic.command programmatically and keep their own logging setup.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)


def database_url() -> str:
    url = config.attributes.get("database_url") or get_settings().database_url
    # SQLAlchemy needs the driver named; we use psycopg 3, already a dependency.
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_online() -> None:
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    raise SystemExit("Offline (--sql) mode is not supported; run against a live database.")
run_migrations_online()
