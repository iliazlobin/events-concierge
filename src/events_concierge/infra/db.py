"""Async SQLAlchemy engine and explicit tenant/system transaction scopes.

Every transaction explicitly clears or establishes the transaction-local `app.tenant_id` GUC read by
FORCE-RLS policies. A tenant scope with an omitted identity records a durable policy-violation audit before
yielding its intentionally empty context; catalog/control-plane work must use the explicit system scope
(FR-1.3/1.4, AC-3)."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    global _engine, _sessionmaker
    _engine = create_async_engine(database_url, echo=echo, pool_pre_ping=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("engine not initialized; call init_engine() first")
    return _engine


async def dispose_engine() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@contextlib.asynccontextmanager
async def tenant_session_scope(tenant_id: UUID | None) -> AsyncIterator[AsyncSession]:
    """Yield one tenant-access transaction with an explicitly established RLS context.

    Passing ``None`` is an intentional missing-context tenant attempt: the private FR-1.4 audit commits
    before the empty-context transaction begins, so the evidence survives a subsequent RLS rejection.
    Tenant-neutral catalog and system-control-plane work must use :func:`system_session_scope` instead.
    """
    if _sessionmaker is None:
        raise RuntimeError("engine not initialized; call init_engine() first")
    async with _sessionmaker() as session:
        if tenant_id is None:
            async with session.begin():
                await _set_tenant_context(session, None)
                await session.execute(text("SELECT public.fn_record_missing_tenant_context()"))
        async with session.begin():
            await _set_tenant_context(session, tenant_id)
            yield session


@contextlib.asynccontextmanager
async def system_session_scope() -> AsyncIterator[AsyncSession]:
    """Yield one explicitly tenant-neutral catalog or control-plane transaction.

    The empty transaction-local GUC prevents a pooled connection from inheriting a prior session-level
    tenant setting. This scope is not valid for tenant data and emits no FR-1.4 violation event.
    """
    if _sessionmaker is None:
        raise RuntimeError("engine not initialized; call init_engine() first")
    async with _sessionmaker() as session, session.begin():
        await _set_tenant_context(session, None)
        yield session


async def _set_tenant_context(session: AsyncSession, tenant_id: UUID | None) -> None:
    """Set a transaction-local tenant GUC, clearing inherited state for an empty context (FR-1.3)."""
    value = "" if tenant_id is None else str(tenant_id)
    await session.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": value},
    )
