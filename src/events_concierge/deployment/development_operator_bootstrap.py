"""Provision isolated development logins after schema migration in the private GKE profile."""

from __future__ import annotations

import argparse
import os
import subprocess
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from ..secret_files import resolve_env_or_file
from .operator_logins import (
    Credentials,
    bootstrap_operator_logins,
    preflight_operator_logins,
    validate_credentials,
)

_POSTGRES_PORT = 5432


def validate_development_bootstrap(
    *, environment: str, mock_cloud: str, connection_mode: str, migration_url: str
) -> None:
    """Fail before migration unless the fixed private development database is selected."""
    try:
        parsed = urlsplit(migration_url)
        valid = (
            environment == "development"
            and mock_cloud.lower() == "true"
            and connection_mode == "development_plaintext"
            and parsed.scheme == "postgresql+psycopg"
            and parsed.username == "ec_owner"
            and bool(parsed.password)
            and parsed.hostname == "ec-dev-application-postgres"
            and parsed.port == _POSTGRES_PORT
            and parsed.path == "/events"
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("operator bootstrap requires the fixed private development database")


def verify_login_passwords(migration_url: str, credentials: Credentials) -> None:
    """Check the pinned credentials without changing existing passwords or logging DSNs."""
    for login, _, password in credentials:
        engine = create_engine(
            make_url(migration_url).set(username=login, password=password),
            hide_parameters=True,
            connect_args={"connect_timeout": 10},
        )
        try:
            with engine.connect() as connection:
                if connection.execute(text("SELECT current_user")).scalar_one() != login:
                    raise RuntimeError("development operator login verification failed")
        except SQLAlchemyError:
            raise RuntimeError("development operator login verification failed") from None
        finally:
            engine.dispose()


def bootstrap(*, migrate: bool = False) -> None:
    migration_url = resolve_env_or_file("EC_MIGRATION_URL") or ""
    validate_development_bootstrap(
        environment=os.environ.get("EC_ENV", ""),
        mock_cloud=os.environ.get("EC_MOCK_CLOUD", ""),
        connection_mode=os.environ.get("EC_DATABASE_CONNECTION_MODE", ""),
        migration_url=migration_url,
    )
    credentials = (
        (
            "ec_dev_operator",
            "ec_operator_controller",
            resolve_env_or_file("EC_DEV_OPERATOR_PASSWORD") or "",
        ),
        (
            "ec_dev_ingestion",
            "ec_ingestion_executor",
            resolve_env_or_file("EC_DEV_INGESTION_PASSWORD") or "",
        ),
    )
    validate_credentials(credentials)
    engine = create_engine(
        migration_url, hide_parameters=True, connect_args={"connect_timeout": 10}
    )
    try:
        existing = preflight_operator_logins(engine, credentials)
        verify_login_passwords(migration_url, tuple(c for c in credentials if c[0] in existing))
        if migrate:
            subprocess.run(["alembic", "upgrade", "head"], check=True)
        bootstrap_operator_logins(engine, credentials)
        verify_login_passwords(migration_url, credentials)
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true")
    args = parser.parse_args()
    bootstrap(migrate=args.migrate)


if __name__ == "__main__":
    main()
