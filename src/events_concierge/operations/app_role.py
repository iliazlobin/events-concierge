"""Explicit, sanitized rotation of the non-owner PostgreSQL application-role password."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from sqlalchemy import create_engine, text
from sqlalchemy.pool import NullPool

APP_ROLE = "ec_app"
APP_ROLE_PASSWORD_ENV = "EC_APP_ROLE_PASSWORD"
_MAX_PASSWORD_BYTES = 1024

_ROLE_POSTURE = text(
    """
    SELECT
        role.rolcanlogin,
        role.rolsuper,
        role.rolcreatedb,
        role.rolcreaterole,
        role.rolreplication,
        role.rolbypassrls,
        (
            SELECT count(*)
            FROM pg_catalog.pg_auth_members membership
            WHERE membership.member = role.oid
        ) AS role_memberships
    FROM pg_catalog.pg_roles role
    WHERE role.rolname = :role_name
    """
)
_STAGE_PASSWORD = text(
    "SELECT pg_catalog.set_config('events_concierge.app_role_password', :password, true)"
)
_ROTATE_PASSWORD = text(
    """
    DO $app_role_rotation$
    DECLARE
        new_password text := pg_catalog.current_setting(
            'events_concierge.app_role_password',
            false
        );
    BEGIN
        IF new_password = '' THEN
            RAISE EXCEPTION 'invalid application role password';
        END IF;
        EXECUTE pg_catalog.format('ALTER ROLE ec_app LOGIN PASSWORD %L', new_password);
        PERFORM pg_catalog.set_config('events_concierge.app_role_password', '', true);
    END
    $app_role_rotation$;
    """
)
_ROLE_CAN_LOGIN = text("SELECT rolcanlogin FROM pg_catalog.pg_roles WHERE rolname = :role_name")


@dataclass(frozen=True, slots=True)
class AppRoleRotationReport:
    """Non-secret evidence emitted by the explicit rotation job."""

    schema_version: int
    kind: str
    role: str
    status: str
    role_can_login: bool
    elevated_attributes_absent: bool
    role_memberships_absent: bool

    @property
    def passed(self) -> bool:
        return (
            self.status == "passed"
            and self.role_can_login
            and self.elevated_attributes_absent
            and self.role_memberships_absent
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def rotate_app_role_password(migration_url: str, password: str) -> AppRoleRotationReport:
    """Rotate ``ec_app`` through an owner connection without rendering the secret in SQL text."""
    if not migration_url.strip():
        raise ValueError("EC_MIGRATION_URL must be non-empty")
    validate_app_role_password(password)
    engine = create_engine(
        migration_url,
        poolclass=NullPool,
        pool_pre_ping=True,
        hide_parameters=True,
    )
    try:
        with engine.begin() as connection:
            posture = (
                connection.execute(
                    _ROLE_POSTURE,
                    {"role_name": APP_ROLE},
                )
                .mappings()
                .one_or_none()
            )
            if posture is None:
                raise RuntimeError("application role does not exist; run migrations first")
            elevated = any(
                bool(posture[field])
                for field in (
                    "rolsuper",
                    "rolcreatedb",
                    "rolcreaterole",
                    "rolreplication",
                    "rolbypassrls",
                )
            )
            if elevated:
                raise RuntimeError("application role has elevated attributes")
            if int(posture["role_memberships"]) != 0:
                raise RuntimeError("application role has role memberships")
            connection.execute(_STAGE_PASSWORD, {"password": password})
            connection.execute(_ROTATE_PASSWORD)
            role_can_login = bool(
                connection.execute(
                    _ROLE_CAN_LOGIN,
                    {"role_name": APP_ROLE},
                ).scalar_one()
            )
            if not role_can_login:
                raise RuntimeError("application role password rotation did not enable login")
    finally:
        engine.dispose()
    return AppRoleRotationReport(
        schema_version=1,
        kind="events-concierge-app-role-password-rotation",
        role=APP_ROLE,
        status="passed",
        role_can_login=True,
        elevated_attributes_absent=True,
        role_memberships_absent=True,
    )


def validate_app_role_password(password: str) -> None:
    """Keep the mounted password bounded before opening an owner connection."""
    if not password or "\x00" in password or len(password.encode("utf-8")) > _MAX_PASSWORD_BYTES:
        raise ValueError(
            f"{APP_ROLE_PASSWORD_ENV} must be non-empty, contain no NUL, and be at most "
            f"{_MAX_PASSWORD_BYTES} UTF-8 bytes"
        )
