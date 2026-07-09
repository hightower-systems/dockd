"""Alembic migration environment for Dockd.

Reads the connection string from the DATABASE_URL environment variable so
no DSN is ever committed. Dockd has no SQLAlchemy models -- migrations are
authored by hand as raw DDL -- so there is no target_metadata for
autogenerate; that is intentional.
"""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _database_url() -> str:
    url = (os.environ.get('DATABASE_URL') or '').strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set; alembic needs a Postgres DSN, e.g. "
            "postgresql://dockd_app:...@host:5432/dockd"
        )
    # SQLAlchemy's default postgres driver is psycopg2; a bare
    # postgresql:// URL resolves to it, matching the runtime driver.
    return url


target_metadata = None


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={'paramstyle': 'named'},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    section['sqlalchemy.url'] = _database_url()
    connectable = engine_from_config(
        section,
        prefix='sqlalchemy.',
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
