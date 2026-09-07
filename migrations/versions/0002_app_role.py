"""non-superuser application role so RLS is actually enforced (FR-1.3)

The bootstrap `ec` role is a superuser and BYPASSES row-level security even under FORCE. The application
must connect as this non-owner, non-BYPASSRLS role for tenant isolation to hold. Migrations still run as
the owner (EC_MIGRATION_URL).

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

from events_concierge.secret_files import resolve_env_or_file

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

APP_ROLE = "ec_app"
APP_ROLE_PASSWORD_ENV = "EC_APP_ROLE_PASSWORD"
_MAX_PASSWORD_BYTES = 1024


def upgrade() -> None:
    bind = op.get_bind()
    role_exists = bool(
        bind.execute(
            text("SELECT EXISTS (SELECT FROM pg_roles WHERE rolname = :role_name)"),
            {"role_name": APP_ROLE},
        ).scalar_one()
    )
    if not role_exists:
        # A migration must never mint a remotely usable role with a repository-known password.
        # Production may provision the login out of band, or pass its secret only to the migration
        # Job through EC_APP_ROLE_PASSWORD.  With neither, the group role remains deliberately
        # NOLOGIN and application startup fails closed until deployment provisioning is complete.
        op.execute(
            f"CREATE ROLE {APP_ROLE} NOLOGIN NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        )
        password = resolve_env_or_file(APP_ROLE_PASSWORD_ENV)
        if password is not None:
            _validate_password(password)
            # Stage the bind parameter only in this transaction, then quote and execute it entirely
            # inside PostgreSQL. The formatted ALTER statement never becomes a Python string or a
            # SQLAlchemy log value; rollback clears the transaction-local setting on any failure.
            bind.execute(
                text(
                    "SELECT pg_catalog.set_config("
                    "'events_concierge.app_role_password', :password, true)"
                ),
                {"password": password},
            )
            op.execute(
                """
                DO $app_role_bootstrap$
                DECLARE
                    new_password text := pg_catalog.current_setting(
                        'events_concierge.app_role_password', false
                    );
                BEGIN
                    EXECUTE pg_catalog.format(
                        'ALTER ROLE ec_app LOGIN PASSWORD %L', new_password
                    );
                    PERFORM pg_catalog.set_config(
                        'events_concierge.app_role_password', '', true
                    );
                END
                $app_role_bootstrap$;
                """
            )
    op.execute(f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {APP_ROLE}")
    op.execute(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {APP_ROLE}")
    # Future tables/sequences (later migrations) inherit these grants.
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {APP_ROLE}"
    )
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {APP_ROLE}"
    )


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {APP_ROLE}")
    op.execute(f"REVOKE USAGE ON SCHEMA public FROM {APP_ROLE}")
    op.execute(f"DROP ROLE IF EXISTS {APP_ROLE}")


def _validate_password(password: str) -> None:
    if not password or "\x00" in password or len(password.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        raise ValueError(
            f"{APP_ROLE_PASSWORD_ENV} must be non-empty, contain no NUL, and be at most "
            f"{_MAX_PASSWORD_BYTES} UTF-8 bytes"
        )
