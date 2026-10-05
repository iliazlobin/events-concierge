"""Verify the CLI's actual SQL admission scope under the non-owner application role."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.integration.test_operator_management import _owner_transaction, _role, _source

from events_concierge.quality.catalog_coverage import _QUERY

pytestmark = pytest.mark.integration


async def _included(connection: AsyncConnection, source: str) -> bool:
    await _role(connection, "ec_app")
    result = await connection.execute(
        text(f"SELECT EXISTS(SELECT 1 FROM ({_QUERY}) AS scoped WHERE source_key=:source)"),
        {"source": source},
    )
    await _role(connection, None)
    return bool(result.scalar_one())


@pytest.mark.parametrize(
    "change",
    [
        "enabled=false",
        "retired_at=clock_timestamp(),enabled=false,retired_reason='retired'",
        "handoff_only=false",
        "reviewed_at=NULL",
        "reviewed_at=clock_timestamp()+interval '1 hour'",
        "review_expires_at=statement_timestamp()",
        "review_expires_at=clock_timestamp()-interval '1 hour'",
        "publisher='Tests'",
    ],
)
async def test_coverage_excludes_sources_that_are_not_currently_admitted(change: str) -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        assert await _included(connection, source)
        await connection.execute(
            text(f"UPDATE public.catalog_sources SET {change} WHERE source_key=:source"),
            {"source": source},
        )
        assert not await _included(connection, source)


@pytest.mark.parametrize(
    ("change", "parameters"),
    [
        ("quarantined=true", {}),
        ("automation_allowed=CAST(:allowed AS jsonb)", {"allowed": "{}"}),
        ("automation_allowed=CAST(:allowed AS jsonb)", {"allowed": '{"browser":false}'}),
        ("automation_allowed=CAST(:allowed AS jsonb)", {"allowed": '{"api":true}'}),
    ],
)
async def test_coverage_excludes_sources_when_the_shared_discovery_policy_denies(
    change: str,
    parameters: dict[str, str],
) -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        assert await _included(connection, source)
        await connection.execute(
            text(f"UPDATE public.source_policy SET {change} WHERE source='public_jsonld'"),
            parameters,
        )
        assert not await _included(connection, source)


async def test_missing_discovery_policy_is_excluded_without_default_allow() -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        assert await _included(connection, source)
        await connection.execute(
            text("DELETE FROM public.source_policy WHERE source='public_jsonld'")
        )
        assert not await _included(connection, source)


async def test_read_only_discovery_does_not_require_paid_permission_or_mutation_kill_release() -> (
    None
):
    async with _owner_transaction() as connection:
        source = await _source(connection)
        await connection.execute(text("UPDATE public.policy_global_control SET kill_switch=true"))
        await connection.execute(
            text("UPDATE public.source_policy SET paid_allowed=false WHERE source='public_jsonld'")
        )
        assert await _included(connection, source)
