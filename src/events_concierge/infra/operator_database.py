"""Dedicated least-privilege operator pool; it never replaces the consumer database engine."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from ..config import Settings
from ..operations.network_safety import is_non_remote_host

_MAX_PORT = 65535

OperatorDatabaseRole = Literal["ec_operator_controller", "ec_ingestion_executor"]


def validate_operator_database_url(settings: Settings, url: str | None) -> str:
    """Reject consumer/owner credentials and insecure deployed transport without printing secrets."""
    if not url:
        raise ValueError("a dedicated operator or executor database credential is required")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("operator database credential is malformed") from error
    if (
        parsed.scheme != "postgresql+psycopg"
        or not parsed.hostname
        or not parsed.username
        or parsed.username in {"ec_app", "postgres", "ec_owner"}
        or not parsed.password
        or not parsed.path.strip("/")
        or parsed.fragment
        or (port is not None and not 1 <= port <= _MAX_PORT)
    ):
        raise ValueError("operator database requires a separate non-owner PostgreSQL login")
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if any(
        key in {"options", "user", "password", "host", "hostaddr", "dbname"} for key, _ in query
    ):
        raise ValueError("operator database connection overrides are forbidden")
    if not settings.mock_cloud:
        sslmodes = [value for key, value in query if key == "sslmode"]
        if settings.database_connection_mode == "cloud_sql_proxy":
            valid = parsed.hostname == "127.0.0.1" and port is not None and sslmodes == ["disable"]
        else:
            valid = not is_non_remote_host(parsed.hostname) and sslmodes == ["verify-full"]
        if not valid:
            raise ValueError("operator database requires verified TLS or loopback Cloud SQL Proxy")
        if settings.database_max_overflow != 0:
            raise ValueError("operator database overflow connections must be disabled")
    return url


async def verify_database_role(session: AsyncSession, role: OperatorDatabaseRole) -> None:
    """Check actual login authority, not the credential filename or configured username."""
    opposite = (
        "ec_ingestion_executor" if role == "ec_operator_controller" else "ec_operator_controller"
    )
    result = (
        (
            await session.execute(
                text("""
            SELECT current_user AS role_name, r.rolsuper, r.rolbypassrls, r.rolcreaterole,
                   r.rolcreatedb, r.rolreplication,
                   pg_has_role(current_user, :role, 'USAGE') AS permitted,
                   pg_has_role(current_user, 'ec_app', 'MEMBER') AS consumer_role,
                   pg_has_role(current_user, :opposite, 'MEMBER') AS opposite_role,
                   pg_has_role(current_user, 'ec_operator_aggregate_definer', 'MEMBER') AS aggregate_role,
                   EXISTS (
                       SELECT 1 FROM pg_catalog.pg_roles elevated
                       WHERE (elevated.rolsuper OR elevated.rolbypassrls OR elevated.rolcreaterole
                              OR elevated.rolcreatedb OR elevated.rolreplication)
                         AND pg_has_role(current_user, elevated.oid, 'MEMBER')
                   ) AS elevated_membership,
                   pg_has_role(current_user,
                       (SELECT datdba FROM pg_catalog.pg_database WHERE datname = current_database()),
                       'MEMBER') AS database_owner
            FROM pg_catalog.pg_roles r WHERE r.rolname = current_user
        """),
                {"role": role, "opposite": opposite},
            )
        )
        .mappings()
        .one()
    )
    if (
        result["role_name"] in {"ec_app", "postgres", "ec_owner"}
        or any(
            result[name]
            for name in (
                "rolsuper",
                "rolbypassrls",
                "rolcreaterole",
                "rolcreatedb",
                "rolreplication",
                "elevated_membership",
                "database_owner",
            )
        )
        or not result["permitted"]
        or result["consumer_role"]
        or result["opposite_role"]
        or result["aggregate_role"]
    ):
        raise RuntimeError("operator database login does not have the isolated runtime role")


class OperatorDatabase:
    """Independent pool shared only by operator projections and controller capabilities."""

    def __init__(self, settings: Settings) -> None:
        url = validate_operator_database_url(settings, settings.operator_database_url)
        self.engine = create_async_engine(
            url,
            pool_size=min(settings.database_pool_size, 4),
            max_overflow=0,
            pool_timeout=settings.database_pool_timeout_seconds,
            pool_recycle=settings.database_pool_recycle_seconds,
            pool_pre_ping=True,
            connect_args={
                "options": "-c statement_timeout=5000 -c lock_timeout=2000 -c idle_in_transaction_session_timeout=10000"
            },
        )
        self._sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    @asynccontextmanager
    async def session_scope(self) -> AsyncIterator[AsyncSession]:
        async with self._sessions() as session, session.begin():
            await verify_database_role(session, "ec_operator_controller")
            await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
            yield session

    async def ready(self) -> bool:
        try:
            async with asyncio.timeout(5), self.session_scope() as session:
                await session.execute(text("SELECT * FROM public.fn_get_operator_schema_head_v1()"))
            return True
        except Exception:
            return False

    async def aclose(self) -> None:
        await self.engine.dispose()
