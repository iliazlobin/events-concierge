"""Integration fixtures: a per-test async engine bound to the test's event loop. Skips the whole
integration suite when EC_DATABASE_URL is not set (i.e. no docker-compose services)."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.support.integration_database import validate_isolated_test_environment

from events_concierge.infra.db import dispose_engine, init_engine


@pytest.fixture(scope="session", autouse=True)
def require_isolated_integration_database() -> None:
    """Reject service-backed tests pointed at a developer/runtime database.

    A plain unit-test run has no service URLs and keeps the historical skip behavior.  Once a
    database URL is supplied, however, both connections must name the randomized database created
    by ``run_isolated_integration``.  This makes an accidentally direct pytest invocation fail
    before it can enqueue durable request-start work in the local application's database.
    """
    configured = os.environ.get("EC_DATABASE_URL") or os.environ.get("EC_MIGRATION_URL")
    if configured is None:
        return
    try:
        validate_isolated_test_environment(os.environ)
    except ValueError as exc:
        raise pytest.UsageError(str(exc)) from exc


@pytest.fixture
async def db() -> AsyncIterator[None]:
    url = os.environ.get("EC_DATABASE_URL")
    if not url:
        pytest.skip("EC_DATABASE_URL not set; run `make test-integration`")
    init_engine(url)
    try:
        yield
    finally:
        await dispose_engine()


@pytest.fixture
async def configure_ticketmaster_budget_scope() -> AsyncIterator[Callable[[str], Awaitable[None]]]:
    """Temporarily configure a unique app scope through the owner-only deployment control plane.

    The production app role must never alter this singleton.  P1c tests use a migration-owner
    connection only to isolate their irreversible daily-ledger fixtures, then restore the prior
    deployment scope before the next integration case.
    """
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    original_scope: str | None = None
    try:
        async with owner_engine.connect() as connection:
            original_scope = (
                await connection.execute(
                    text(
                        """SELECT quota_scope FROM public.provider_budget_scope
                           WHERE source = 'ticketmaster'"""
                    )
                )
            ).scalar_one()

        async def configure(quota_scope: str) -> None:
            async with owner_engine.begin() as connection:
                await connection.execute(
                    text(
                        """UPDATE public.provider_budget_scope
                           SET quota_scope = :quota_scope, configured_at = clock_timestamp()
                           WHERE source = 'ticketmaster'"""
                    ),
                    {"quota_scope": quota_scope},
                )

        yield configure
    finally:
        if original_scope is not None:
            async with owner_engine.begin() as connection:
                await connection.execute(
                    text(
                        """UPDATE public.provider_budget_scope
                           SET quota_scope = :quota_scope, configured_at = clock_timestamp()
                           WHERE source = 'ticketmaster'"""
                    ),
                    {"quota_scope": original_scope},
                )
        await owner_engine.dispose()
