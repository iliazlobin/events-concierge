"""Explicit local fixture logins, run by the local migration command after Alembic.

This never grants operator authority to ec_app. Production logins and passwords are provisioned
separately. Existing login passwords are preserved, including across rebuilds.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from urllib.parse import urlsplit

from sqlalchemy import create_engine

from ..operations.network_safety import is_loopback_host
from ..secret_files import resolve_env_or_file
from .operator_logins import bootstrap_operator_logins


def validate_local_bootstrap(*, environment: str, mock_cloud: str, migration_url: str) -> None:
    parsed = urlsplit(migration_url)
    if (
        environment != "local"
        or mock_cloud.lower() != "true"
        or parsed.scheme != "postgresql+psycopg"
        or not (is_loopback_host(parsed.hostname) or parsed.hostname == "postgres")
        or parsed.path != "/ec"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("operator fixture bootstrap requires the explicit local mock ec database")


def bootstrap() -> None:
    migration_url = resolve_env_or_file("EC_MIGRATION_URL") or ""
    validate_local_bootstrap(
        environment=os.environ.get("EC_ENV", ""),
        mock_cloud=os.environ.get("EC_MOCK_CLOUD", ""),
        migration_url=migration_url,
    )
    engine = create_engine(migration_url, hide_parameters=True)
    try:
        bootstrap_operator_logins(
            engine,
            (
                (
                    "ec_local_operator",
                    "ec_operator_controller",
                    os.environ.get("EC_LOCAL_OPERATOR_PASSWORD", "ec_local_operator"),
                ),
                (
                    "ec_local_ingestion",
                    "ec_ingestion_executor",
                    os.environ.get("EC_LOCAL_INGESTION_PASSWORD", "ec_local_ingestion"),
                ),
            ),
        )
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--migrate", action="store_true", help="Run Alembic before local bootstrap")
    args = parser.parse_args()
    # Validate before starting a migration, not after an accidentally remote write.
    validate_local_bootstrap(
        environment=os.environ.get("EC_ENV", ""),
        mock_cloud=os.environ.get("EC_MOCK_CLOUD", ""),
        migration_url=resolve_env_or_file("EC_MIGRATION_URL") or "",
    )
    if args.migrate:
        subprocess.run(["alembic", "upgrade", "head"], check=True)
    bootstrap()


if __name__ == "__main__":
    main()
