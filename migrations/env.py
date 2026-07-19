"""Alembic migration environment (synchronous psycopg3 connection; the app itself runs async)."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool


def _sync_url() -> str:
    # Migrations run as the OWNER (creates tables, policies, the app role). The app itself connects
    # as the non-superuser ec_app role (EC_DATABASE_URL) so RLS is enforced.
    url = os.environ.get("EC_MIGRATION_URL") or os.environ.get(
        "EC_DATABASE_URL", "postgresql+psycopg://ec:ec@localhost:5433/ec"
    )
    # psycopg3 driver works for both sync and async; alembic uses it synchronously here.
    return url


def run_migrations_online() -> None:
    engine = create_engine(_sync_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


run_migrations_online()
