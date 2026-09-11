"""Explicit local fixture logins, run by the local migration command after Alembic.

This never grants operator authority to ec_app. Production logins and passwords are provisioned
separately. Existing login passwords are preserved, including across rebuilds.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from urllib.parse import urlsplit

from sqlalchemy import create_engine, text

from ..operations.network_safety import is_loopback_host
from ..secret_files import resolve_env_or_file

_MAX_PASSWORD_BYTES = 1024


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
        with engine.begin() as connection:
            for login, capability, password_variable in (
                ("ec_local_operator", "ec_operator_controller", "EC_LOCAL_OPERATOR_PASSWORD"),
                ("ec_local_ingestion", "ec_ingestion_executor", "EC_LOCAL_INGESTION_PASSWORD"),
            ):
                existing = (
                    connection.execute(
                        text("""
                    SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreaterole, rolcreatedb,
                           rolreplication, pg_has_role(oid, 'ec_app', 'MEMBER') AS consumer_role,
                           pg_has_role(oid, :opposite, 'MEMBER') AS opposite_role,
                           pg_has_role(oid, 'ec_operator_aggregate_definer', 'MEMBER') AS aggregate_role,
                           EXISTS (
                               SELECT 1 FROM pg_catalog.pg_roles elevated
                               WHERE (elevated.rolsuper OR elevated.rolbypassrls
                                      OR elevated.rolcreaterole OR elevated.rolcreatedb
                                      OR elevated.rolreplication)
                                 AND pg_has_role(r.oid, elevated.oid, 'MEMBER')
                           ) AS elevated_membership,
                           pg_has_role(oid,
                               (SELECT datdba FROM pg_catalog.pg_database
                                WHERE datname = current_database()), 'MEMBER') AS database_owner
                    FROM pg_catalog.pg_roles r WHERE rolname = :login
                """),
                        {
                            "login": login,
                            "opposite": (
                                "ec_ingestion_executor"
                                if capability == "ec_operator_controller"
                                else "ec_operator_controller"
                            ),
                        },
                    )
                    .mappings()
                    .one_or_none()
                )
                if existing is not None and (
                    not existing["rolcanlogin"]
                    or any(
                        existing[field]
                        for field in (
                            "rolsuper",
                            "rolbypassrls",
                            "rolcreaterole",
                            "rolcreatedb",
                            "rolreplication",
                            "consumer_role",
                            "opposite_role",
                            "aggregate_role",
                            "elevated_membership",
                            "database_owner",
                        )
                    )
                ):
                    raise RuntimeError("local operator fixture login collides with another role")
                if existing is None:
                    # Login names and capabilities above are fixed program constants.
                    connection.execute(
                        text(
                            f"CREATE ROLE {login} NOLOGIN NOSUPERUSER NOCREATEDB "
                            "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                        )
                    )
                    password = os.environ.get(password_variable, login)
                    if (
                        not password
                        or len(password.encode()) > _MAX_PASSWORD_BYTES
                        or "\x00" in password
                    ):
                        raise ValueError("invalid local fixture password")
                    connection.execute(
                        text("""
                        SELECT set_config('events_concierge.local_login', :login, true),
                               set_config('events_concierge.local_password', :password, true)
                    """),
                        {"login": login, "password": password},
                    )
                    connection.execute(
                        text("""
                        DO $$ BEGIN
                            EXECUTE format('ALTER ROLE %I LOGIN PASSWORD %L',
                                current_setting('events_concierge.local_login'),
                                current_setting('events_concierge.local_password'));
                            PERFORM set_config('events_concierge.local_password', '', true);
                        END $$
                    """)
                    )
                connection.execute(text(f"GRANT {capability} TO {login}"))
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
