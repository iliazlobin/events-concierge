"""Run pytest against one fresh PostgreSQL database, then delete only that database.

The long-lived local ``ec`` database is never migrated or cleaned by this command.  A UUID-derived
name and a strict allowlist are carried into pytest so collection fails if either URL is redirected.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from collections.abc import Sequence
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

from .integration_database import (
    isolated_database_name,
    maintenance_url,
    replace_database,
    validate_isolated_test_environment,
)


def _create_database(owner_url: str, database: str) -> None:
    """Create the already-validated UUID database outside a transaction."""
    if database != isolated_database_name(database.removeprefix("ec_test_")):
        raise ValueError("refusing to create a database outside the integration-test allowlist")
    engine = create_engine(
        maintenance_url(owner_url), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        with engine.connect() as connection:
            connection.exec_driver_sql(f'CREATE DATABASE "{database}"')
    finally:
        engine.dispose()


def _drop_database(owner_url: str, database: str) -> None:
    """Terminate only test-database sessions and drop only the exact UUID database."""
    if database != isolated_database_name(database.removeprefix("ec_test_")):
        raise ValueError("refusing to drop a database outside the integration-test allowlist")
    engine = create_engine(
        maintenance_url(owner_url), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        with engine.connect() as connection:
            connection.execute(
                text(
                    """SELECT pg_catalog.pg_terminate_backend(pid)
                       FROM pg_catalog.pg_stat_activity
                       WHERE datname = :database
                         AND pid <> pg_catalog.pg_backend_pid()"""
                ),
                {"database": database},
            )
            connection.exec_driver_sql(f'DROP DATABASE IF EXISTS "{database}"')
    finally:
        engine.dispose()


def run(pytest_args: Sequence[str], environ: dict[str, str] | None = None) -> int:
    """Create, migrate, test, and unconditionally clean one isolated database."""
    source_environment = os.environ if environ is None else environ
    owner_url = source_environment.get("EC_MIGRATION_URL")
    app_url = source_environment.get("EC_DATABASE_URL")
    if owner_url is None or app_url is None:
        raise ValueError("EC_DATABASE_URL and EC_MIGRATION_URL are required")

    run_id = uuid4().hex
    database = isolated_database_name(run_id)
    child_environment = dict(source_environment)
    child_environment.update(
        {
            "EC_DATABASE_URL": replace_database(app_url, database),
            "EC_MIGRATION_URL": replace_database(owner_url, database),
            "EC_INTEGRATION_TEST_RUN_ID": run_id,
            # A defensive boundary for any future test that connects to the persistent local
            # Temporal service instead of WorkflowEnvironment's ephemeral server.
            "EC_TEMPORAL_TASK_QUEUE": f"events-concierge-test-{run_id}",
            # Fresh CI clusters do not have the application role yet. This is an explicit local
            # fixture secret; production migration jobs receive a separately managed value.
            "EC_APP_ROLE_PASSWORD": source_environment.get(
                "EC_LOCAL_APP_ROLE_PASSWORD", "ec_app"
            ),
        }
    )
    validate_isolated_test_environment(child_environment)

    created = False
    try:
        print(f"Creating disposable integration database {database}", flush=True)
        _create_database(owner_url, database)
        created = True
        migration = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            env=child_environment,
            check=False,
        )
        if migration.returncode != 0:
            return migration.returncode
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *pytest_args],
            env=child_environment,
            check=False,
        )
        return result.returncode
    finally:
        if created:
            print(f"Dropping disposable integration database {database}", flush=True)
            _drop_database(owner_url, database)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; every argument after ``--`` is passed directly to pytest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    parsed = parser.parse_args(argv)
    pytest_args = list(parsed.pytest_args)
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]
    if not pytest_args:
        parser.error("at least one pytest selector or option is required after `--`")
    try:
        return run(pytest_args)
    except ValueError as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
