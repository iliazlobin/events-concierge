"""Provision only isolated local/development operator logins; never elevate consumer roles."""

from __future__ import annotations

from sqlalchemy import Connection, Engine, text

_ALLOWED_LOGINS = {
    "ec_local_operator": "ec_operator_controller",
    "ec_local_ingestion": "ec_ingestion_executor",
    "ec_dev_operator": "ec_operator_controller",
    "ec_dev_ingestion": "ec_ingestion_executor",
}
_MAX_PASSWORD_BYTES = 1024
Credentials = tuple[tuple[str, str, str], ...]


def validate_credentials(credentials: Credentials) -> None:
    for login, capability, password in credentials:
        if _ALLOWED_LOGINS.get(login) != capability:
            raise ValueError("operator bootstrap login or capability is not allowed")
        if not password or len(password.encode()) > _MAX_PASSWORD_BYTES or "\x00" in password:
            raise ValueError("invalid operator bootstrap password")


def _validate_existing(connection: Connection, login: str, capability: str) -> bool:
    # Reserved logins must be leaves in the role graph. Inbound membership could
    # otherwise elevate ec_app (or another principal) when we grant the capability.
    # This query also works before the capability roles have been migrated.
    existing = (
        connection.execute(
            text("""
            SELECT rolcanlogin, rolinherit, rolsuper, rolbypassrls, rolcreaterole,
                   rolcreatedb, rolreplication,
                   EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
                           JOIN pg_catalog.pg_roles granted ON granted.oid = m.roleid
                           WHERE m.member = r.oid AND granted.rolname <> :capability)
                       AS unexpected_membership,
                   EXISTS (SELECT 1 FROM pg_catalog.pg_auth_members m
                           WHERE m.roleid = r.oid) AS has_members,
                   EXISTS (SELECT 1 FROM pg_catalog.pg_database d
                           WHERE d.datdba = r.oid) AS database_owner
            FROM pg_catalog.pg_roles r WHERE rolname = :login
        """),
            {"login": login, "capability": capability},
        )
        .mappings()
        .one_or_none()
    )
    if existing is not None and (
        not existing["rolcanlogin"]
        or not existing["rolinherit"]
        or any(
            existing[field]
            for field in (
                "rolsuper",
                "rolbypassrls",
                "rolcreaterole",
                "rolcreatedb",
                "rolreplication",
                "unexpected_membership",
                "has_members",
                "database_owner",
            )
        )
    ):
        raise RuntimeError("operator bootstrap login collides with another role")
    return existing is not None


def preflight_operator_logins(engine: Engine, credentials: Credentials) -> tuple[str, ...]:
    """Reject known collisions before migration; return logins that already exist."""
    validate_credentials(credentials)
    with engine.connect() as connection:
        return tuple(
            login
            for login, capability, _ in credentials
            if _validate_existing(connection, login, capability)
        )


def bootstrap_operator_logins(engine: Engine, credentials: Credentials) -> None:
    """Create missing logins transactionally, preserving passwords on repeat runs."""
    validate_credentials(credentials)
    with engine.begin() as connection:
        for login, capability, password in credentials:
            if not _validate_existing(connection, login, capability):
                # Identifiers are checked against the fixed allowlist above.
                connection.execute(
                    text(
                        f"CREATE ROLE {login} NOLOGIN NOSUPERUSER NOCREATEDB "
                        "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                    )
                )
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
