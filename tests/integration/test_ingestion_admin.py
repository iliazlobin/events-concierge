"""Capability, fixture-exclusion, and lease-fencing tests for ingestion administration."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession, create_async_engine

from events_concierge.adapters.postgres.ingestion_admin import (
    PostgresIngestionAdminRepository as _PostgresIngestionAdminRepository,
)
from events_concierge.domain.ingestion_admin import (
    IngestionCommandAction,
    IngestionCommandLease,
    IngestionCommandRunTarget,
    IngestionEffectiveStatus,
    IngestionFilterValue,
    IngestionSourceRevisionTarget,
)
from events_concierge.infra.db import system_session_scope
from events_concierge.ports.ingestion_admin import (
    IngestionCommandConflictError,
    IngestionCommandUnavailableError,
    IngestionSourceConfigurationConflictError,
    IngestionSourceConfigurationUnavailableError,
    IngestionSourceNotFoundError,
)

pytestmark = pytest.mark.integration


@dataclass(slots=True)
class _OperatorConnections:
    engine: AsyncEngine | None = None


_operator_connections_state = _OperatorConnections()


@pytest.fixture(autouse=True)
async def _operator_connections() -> AsyncIterator[None]:
    """Use explicit operator capabilities while preserving ec_app for denial assertions."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the isolated integration suite")
    engine = create_async_engine(migration_url, pool_pre_ping=True)
    _operator_connections_state.engine = engine
    try:
        yield
    finally:
        await engine.dispose()
        _operator_connections_state.engine = None


@asynccontextmanager
async def _operator_session_scope(role: str) -> AsyncIterator[AsyncSession]:
    assert role in {"ec_operator_controller", "ec_ingestion_executor"}
    assert _operator_connections_state.engine is not None
    async with AsyncSession(_operator_connections_state.engine) as session, session.begin():
        await session.execute(text(f"SET LOCAL ROLE {role}"))
        await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        yield session


def _controller_session_scope() -> Any:
    return _operator_session_scope("ec_operator_controller")


def _executor_session_scope() -> Any:
    return _operator_session_scope("ec_ingestion_executor")


class PostgresIngestionAdminRepository:
    """Test facade across the two real process roles; no runtime role receives both grants."""

    _EXECUTOR_METHODS = frozenset(
        {
            "claim_batch",
            "renew_lease",
            "link_command_runs",
            "complete",
            "defer",
            "fail",
        }
    )

    def __init__(self) -> None:
        self._controller = _PostgresIngestionAdminRepository(
            session_scope=_controller_session_scope
        )
        self._executor = _PostgresIngestionAdminRepository(session_scope=_executor_session_scope)

    def __getattr__(self, name: str) -> Any:
        repository = self._executor if name in self._EXECUTOR_METHODS else self._controller
        return getattr(repository, name)


@dataclass(frozen=True, slots=True)
class _Fixture:
    owner: AsyncEngine
    requested_by: str
    live_source: str
    fixture_sources: tuple[str, str, str]
    live_event_id: UUID
    fixture_event_id: UUID


async def _delete_fixture_commands(connection: AsyncConnection, requested_by: str) -> None:
    """Explicitly remove this fixture's evidence; production history stays restrictive."""
    command_ids = list(
        (
            await connection.scalars(
                text(
                    "SELECT command_id FROM public.ingestion_admin_commands WHERE requested_by=:requested_by"
                ),
                {"requested_by": requested_by},
            )
        ).all()
    )
    for table in (
        "ingestion_command_events",
        "ingestion_command_attempts",
        "ingestion_command_tasks",
        "ingestion_command_plans",
        "ingestion_admin_commands",
    ):
        await connection.execute(
            text(f"DELETE FROM public.{table} WHERE command_id=ANY(CAST(:command_ids AS uuid[]))"),
            {"command_ids": command_ids},
        )


@asynccontextmanager
async def _ingestion_fixture() -> AsyncIterator[_Fixture]:
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    suffix = uuid4().hex[:12]
    requested_by = f"integration-ingestion-{suffix}"
    live_source = f"admin-live-{suffix}"
    fixture_sources = (
        f"test-admin-{suffix}",
        f"admin-tests-{suffix}",
        f"admin-host-{suffix}",
    )
    live_event_id = uuid4()
    fixture_event_id = uuid4()
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    async with owner.begin() as connection:
        policy = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT automation_allowed, quarantined
                    FROM public.source_policy
                    WHERE source = 'public_jsonld'
                    """
                    )
                )
            )
            .mappings()
            .one()
        )
        await connection.execute(
            text(
                """
                UPDATE public.source_policy
                SET automation_allowed =
                        jsonb_set(automation_allowed, '{browser}', 'true'::jsonb, true),
                    quarantined = false
                WHERE source = 'public_jsonld'
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO public.catalog_sources (
                    source_key, display_name, publisher, seed_url, approved_origins,
                    region, mode, handoff_only, enabled, reviewed_at, review_expires_at,
                    refresh_interval_minutes, min_interval_ms, page_limit, source_revision
                )
                VALUES (
                    :source_key, :display_name, :publisher, :seed_url,
                    CAST(:approved_origins AS text[]), 'bay_area_9_county', 'public_jsonld',
                    true, true, :reviewed_at, NULL, 60, 1500, 1, 1
                )
                """
            ),
            [
                {
                    "source_key": fixture_sources[0],
                    "display_name": "Fixture by key",
                    "publisher": "Reviewed Publisher",
                    "seed_url": f"https://events.example.com/{fixture_sources[0]}",
                    "approved_origins": ["https://events.example.com"],
                    "reviewed_at": datetime(2000, 1, 1, tzinfo=UTC),
                },
                {
                    "source_key": fixture_sources[1],
                    "display_name": "Fixture by publisher",
                    "publisher": "Tests",
                    "seed_url": f"https://events.example.com/{fixture_sources[1]}",
                    "approved_origins": ["https://events.example.com"],
                    "reviewed_at": datetime(2000, 1, 2, tzinfo=UTC),
                },
                {
                    "source_key": fixture_sources[2],
                    "display_name": "Fixture by host",
                    "publisher": "Reviewed Publisher",
                    "seed_url": f"https://{fixture_sources[2]}.example.test/events",
                    "approved_origins": [f"https://{fixture_sources[2]}.example.test"],
                    "reviewed_at": datetime(2000, 1, 3, tzinfo=UTC),
                },
                {
                    "source_key": live_source,
                    "display_name": "Reviewed live source",
                    "publisher": "Reviewed Publisher",
                    "seed_url": f"https://events.example.com/{live_source}",
                    "approved_origins": ["https://events.example.com"],
                    "reviewed_at": datetime(2001, 1, 1, tzinfo=UTC),
                },
            ],
        )
    try:
        yield _Fixture(
            owner,
            requested_by,
            live_source,
            fixture_sources,
            live_event_id,
            fixture_event_id,
        )
    finally:
        async with owner.begin() as connection:
            await _delete_fixture_commands(connection, requested_by)
            await connection.execute(
                text(
                    """
                    DELETE FROM public.catalog_event_observations
                    WHERE source_key = ANY(CAST(:source_keys AS text[]))
                    """
                ),
                {"source_keys": [live_source, *fixture_sources]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM public.catalog_refresh_runs
                    WHERE source_key = ANY(CAST(:source_keys AS text[]))
                    """
                ),
                {"source_keys": [live_source, *fixture_sources]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM public.canonical_events
                    WHERE canonical_event_id = ANY(CAST(:event_ids AS uuid[]))
                    """
                ),
                {"event_ids": [live_event_id, fixture_event_id]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM public.catalog_source_configuration_audit
                    WHERE source_key = ANY(CAST(:source_keys AS text[]))
                    """
                ),
                {"source_keys": [live_source, *fixture_sources]},
            )
            await connection.execute(
                text(
                    """
                    DELETE FROM public.catalog_sources
                    WHERE source_key = ANY(CAST(:source_keys AS text[]))
                    """
                ),
                {"source_keys": [live_source, *fixture_sources]},
            )
            await connection.execute(
                text(
                    """
                    UPDATE public.source_policy
                    SET automation_allowed = CAST(:automation_allowed AS jsonb),
                        quarantined = :quarantined
                    WHERE source = 'public_jsonld'
                    """
                ),
                {
                    "automation_allowed": json.dumps(policy["automation_allowed"]),
                    "quarantined": policy["quarantined"],
                },
            )
        await owner.dispose()


async def test_retired_source_is_inspectable_but_cannot_be_scheduled_or_reenabled(
    db: None,
) -> None:
    repository = PostgresIngestionAdminRepository()
    source_key = "alameda-county-library-fremont-events"
    replacement_key = "alameda-county-library-all-physical-branches-events"

    page = await repository.list_sources(source_key=source_key)
    assert page.total == 1
    source = page.items[0]
    assert source.enabled is False
    assert source.effective_status is IngestionEffectiveStatus.RETIRED
    assert source.retired_at is not None
    assert source.retired_reason == "superseded_by_aggregate_source"
    assert source.superseded_by_source_key == replacement_key

    detail = await repository.get_source_detail(
        source_key,
        window_hours=24,
        bucket_hours=6,
    )
    assert detail.source.effective_status is IngestionEffectiveStatus.RETIRED
    assert detail.source.retired_at == source.retired_at
    assert detail.source.retired_reason == source.retired_reason
    assert detail.source.superseded_by_source_key == replacement_key

    due = await repository.list_due_refreshes(datetime.now(UTC), limit=500)
    assert source_key not in {item.source.source_key for item in due}

    with pytest.raises(IngestionSourceConfigurationUnavailableError):
        await repository.set_sources_enabled(
            (IngestionSourceRevisionTarget(source_key, source.source_revision),),
            enabled=True,
            requested_by="integration-retired-lifecycle",
        )

    configuration = detail.source
    with pytest.raises(IngestionSourceConfigurationUnavailableError):
        await repository.update_source_configuration(
            source_key,
            expected_revision=configuration.source_revision,
            seed_url=configuration.seed_url,
            approved_origins=configuration.approved_origins,
            mode=configuration.mode,
            enabled=True,
            handoff_only=configuration.handoff_only,
            review_expires_at=configuration.review_expires_at,
            refresh_interval_minutes=configuration.refresh_interval_minutes,
            min_interval_ms=configuration.min_interval_ms,
            page_limit=configuration.page_limit,
            requested_by="integration-retired-lifecycle",
        )


async def test_retired_source_commands_are_terminalized_before_claim_and_cannot_renew(
    db: None,
) -> None:
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    repository = PostgresIngestionAdminRepository()
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    source_key = "alameda-county-library-fremont-events"
    requested_by = f"integration-retired-command-{uuid4().hex}"
    queued_id = uuid4()
    running_id = uuid4()
    running_token = uuid4()

    try:
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.ingestion_admin_commands (
                        command_id, action, source_key, requested_by
                    )
                    VALUES (:command_id, 'refresh_source', :source_key, :requested_by)
                    """
                ),
                {
                    "command_id": queued_id,
                    "source_key": source_key,
                    "requested_by": requested_by,
                },
            )

        claimed = await repository.claim_batch(10, 300, "integration", None)
        assert queued_id not in {lease.command_id for lease in claimed}
        queued = await repository.get_command(queued_id)
        assert queued is not None
        assert queued.status.value == "failed"
        assert queued.error_code == "source_retired"
        assert queued.result is None
        async with owner.connect() as connection:
            queued_attempt_count = await connection.scalar(
                text(
                    """SELECT attempt_count
                       FROM public.ingestion_admin_commands
                       WHERE command_id = :command_id"""
                ),
                {"command_id": queued_id},
            )
        assert queued_attempt_count == 0

        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.ingestion_admin_commands (
                        command_id, action, source_key, requested_by, status,
                        started_at, attempt_count, lease_token, lease_expires_at
                    )
                    VALUES (
                        :command_id, 'refresh_source', :source_key, :requested_by, 'running',
                        clock_timestamp(), 1, :lease_token,
                        clock_timestamp() + INTERVAL '10 minutes'
                    )
                    """
                ),
                {
                    "command_id": running_id,
                    "source_key": source_key,
                    "requested_by": requested_by,
                    "lease_token": running_token,
                },
            )

        live_lease = IngestionCommandLease(
            command_id=running_id,
            action=IngestionCommandAction.REFRESH_SOURCE,
            source_key=source_key,
            attempt_count=1,
            lease_token=running_token,
        )
        assert await repository.renew_lease(live_lease, 300) is False

        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.ingestion_admin_commands
                    SET lease_expires_at = clock_timestamp() - INTERVAL '1 second'
                    WHERE command_id = :command_id
                    """
                ),
                {"command_id": running_id},
            )
        reclaimed = await repository.claim_batch(10, 300, "integration", None)
        assert running_id not in {lease.command_id for lease in reclaimed}
        running = await repository.get_command(running_id)
        assert running is not None
        assert running.status.value == "failed"
        assert running.error_code == "source_retired"
        async with owner.connect() as connection:
            running_attempt_count = await connection.scalar(
                text(
                    """SELECT attempt_count
                       FROM public.ingestion_admin_commands
                       WHERE command_id = :command_id"""
                ),
                {"command_id": running_id},
            )
        assert running_attempt_count == 1
    finally:
        async with owner.begin() as connection:
            await _delete_fixture_commands(connection, requested_by)
        await owner.dispose()


async def test_bulk_source_enabled_update_is_atomic_audited_and_revision_guarded(
    db: None,
) -> None:
    repository = PostgresIngestionAdminRepository()

    async with _ingestion_fixture() as fixture:
        first_fixture = fixture.fixture_sources[0]
        targets = (
            IngestionSourceRevisionTarget(fixture.live_source, 1),
            IngestionSourceRevisionTarget(first_fixture, 1),
        )

        changed = await repository.set_sources_enabled(
            targets,
            enabled=False,
            requested_by=fixture.requested_by,
        )

        assert changed.enabled is False
        assert changed.requested == 2
        assert changed.updated == 2
        assert changed.unchanged == 0
        assert {(item.source_key, item.source_revision) for item in changed.items} == {
            (fixture.live_source, 2),
            (first_fixture, 2),
        }
        projected = await repository.list_sources(
            source_key=fixture.live_source,
            include_fixtures=True,
        )
        assert projected.items[0].enabled is False
        assert projected.items[0].source_revision == 2

        unchanged = await repository.set_sources_enabled(
            (
                IngestionSourceRevisionTarget(fixture.live_source, 2),
                IngestionSourceRevisionTarget(first_fixture, 2),
            ),
            enabled=False,
            requested_by=fixture.requested_by,
        )
        assert unchanged.requested == 2
        assert unchanged.updated == 0
        assert unchanged.unchanged == 2
        assert unchanged.items == ()

        with pytest.raises(IngestionSourceConfigurationConflictError):
            await repository.set_sources_enabled(
                (
                    IngestionSourceRevisionTarget(fixture.live_source, 2),
                    IngestionSourceRevisionTarget(first_fixture, 1),
                ),
                enabled=True,
                requested_by=fixture.requested_by,
            )

        async with fixture.owner.begin() as connection:
            after_conflict = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT source_key, enabled, source_revision
                            FROM public.catalog_sources
                            WHERE source_key = ANY(CAST(:source_keys AS text[]))
                            ORDER BY source_key
                            """
                        ),
                        {"source_keys": [fixture.live_source, first_fixture]},
                    )
                )
                .mappings()
                .all()
            )
            audit_rows = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT source_key, prior_revision, new_revision, requested_by,
                                   before_config ->> 'enabled' AS before_enabled,
                                   after_config ->> 'enabled' AS after_enabled
                            FROM public.catalog_source_configuration_audit
                            WHERE source_key = ANY(CAST(:source_keys AS text[]))
                            ORDER BY source_key
                            """
                        ),
                        {"source_keys": [fixture.live_source, first_fixture]},
                    )
                )
                .mappings()
                .all()
            )
        assert {(row["enabled"], row["source_revision"]) for row in after_conflict} == {(False, 2)}
        assert len(audit_rows) == 2
        assert {row["requested_by"] for row in audit_rows} == {fixture.requested_by}
        assert {
            (
                row["prior_revision"],
                row["new_revision"],
                row["before_enabled"],
                row["after_enabled"],
            )
            for row in audit_rows
        } == {(1, 2, "true", "false")}

        unreviewed = fixture.fixture_sources[1]
        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.catalog_sources
                    SET reviewed_at = NULL
                    WHERE source_key = :source_key
                    """
                ),
                {"source_key": unreviewed},
            )
        with pytest.raises(IngestionSourceConfigurationUnavailableError):
            await repository.set_sources_enabled(
                (
                    IngestionSourceRevisionTarget(fixture.live_source, 2),
                    IngestionSourceRevisionTarget(unreviewed, 1),
                ),
                enabled=True,
                requested_by=fixture.requested_by,
            )
        live_after_unavailable = (
            await repository.list_sources(
                source_key=fixture.live_source,
                include_fixtures=True,
            )
        ).items[0]
        assert live_after_unavailable.enabled is False
        assert live_after_unavailable.source_revision == 2

        with pytest.raises(DBAPIError):
            async with system_session_scope() as session:
                await session.execute(
                    text("SELECT * FROM public.catalog_source_configuration_audit LIMIT 1")
                )


async def test_default_projections_hide_all_fixture_signals_before_due_limit(db: None) -> None:
    repository = PostgresIngestionAdminRepository()
    baseline = await repository.overview()

    async with _ingestion_fixture() as fixture:
        live_run_key = f"overview-live:{uuid4()}"
        fixture_run_key = f"overview-fixture:{uuid4()}"
        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.catalog_refresh_runs (
                        source_key, run_key, status, lease_token, started_at,
                        lease_expires_at, completed_at, candidate_count,
                        canonical_count, error, attempt_count
                    )
                    VALUES
                        (
                            :live_source, :live_run_key, 'succeeded', NULL,
                            :started_at, NULL, :completed_at, 1, 1, NULL, 1
                        ),
                        (
                            :fixture_source, :fixture_run_key, 'succeeded', NULL,
                            :started_at, NULL, :completed_at, 1, 1, NULL, 1
                        )
                    """
                ),
                {
                    "live_source": fixture.live_source,
                    "live_run_key": live_run_key,
                    "fixture_source": fixture.fixture_sources[0],
                    "fixture_run_key": fixture_run_key,
                    "started_at": datetime(2001, 1, 1, tzinfo=UTC),
                    "completed_at": datetime(2001, 1, 1, 0, 1, tzinfo=UTC),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO public.canonical_events (
                        canonical_event_id, title, start_at, description, price_status
                    )
                    VALUES
                        (
                            :live_event_id, 'Live admin overview event',
                            :start_at, 'non-fixture catalog event', 'free'
                        ),
                        (
                            :fixture_event_id, 'Fixture-only admin overview event',
                            :start_at, 'hidden fixture catalog event', 'free'
                        )
                    """
                ),
                {
                    "live_event_id": fixture.live_event_id,
                    "fixture_event_id": fixture.fixture_event_id,
                    "start_at": datetime(2099, 1, 1, tzinfo=UTC),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO public.catalog_event_observations (
                        source_key, source, source_event_id, canonical_event_id,
                        registration_url, price_status, content_hash, last_run_key
                    )
                    VALUES
                        (
                            :live_source, 'public_jsonld', :live_source_event_id,
                            :live_event_id, 'https://events.example.com/live-admin-event',
                            'free', :live_hash, :live_run_key
                        ),
                        (
                            :fixture_source, 'public_jsonld', :fixture_source_event_id,
                            :fixture_event_id, 'https://events.example.com/fixture-admin-event',
                            'free', :fixture_hash, :fixture_run_key
                        )
                    """
                ),
                {
                    "live_source": fixture.live_source,
                    "live_source_event_id": f"live-{fixture.live_event_id}",
                    "live_event_id": fixture.live_event_id,
                    "live_hash": "a" * 64,
                    "live_run_key": live_run_key,
                    "fixture_source": fixture.fixture_sources[0],
                    "fixture_source_event_id": f"fixture-{fixture.fixture_event_id}",
                    "fixture_event_id": fixture.fixture_event_id,
                    "fixture_hash": "b" * 64,
                    "fixture_run_key": fixture_run_key,
                },
            )

        overview = await repository.overview()
        assert overview.summary.fixture_sources == baseline.summary.fixture_sources + 3
        assert overview.summary.catalog_events == baseline.summary.catalog_events + 1

        for source_key in fixture.fixture_sources:
            hidden = await repository.list_sources(source_key=source_key)
            explicit = await repository.list_sources(
                source_key=source_key,
                include_fixtures=True,
            )
            assert hidden.total == 0
            assert explicit.total == 1

        visible = await repository.list_sources(source_key=fixture.live_source)
        assert visible.total == 1
        assert visible.items[0].source_key == fixture.live_source
        assert visible.items[0].event_count == 1

        # The three fixture slots are intentionally older than the control. Returning the live
        # control at limit=1 proves fixture exclusion happens before LIMIT, not in Python.
        due = await repository.list_due_refreshes(
            datetime(2026, 7, 23, tzinfo=UTC),
            limit=1,
        )
        assert [item.source.source_key for item in due] == [fixture.live_source]


@pytest.mark.parametrize(
    ("manual_status", "completion_offset_seconds", "recovers"),
    [
        ("succeeded", -60, False),
        ("succeeded", 0, False),
        ("failed", 60, False),
        ("succeeded", 60, True),
    ],
)
async def test_cadence_recovery_requires_a_newer_successful_publication(
    db: None, manual_status: str, completion_offset_seconds: int, recovers: bool
) -> None:
    """Recovery resumes the next due slot while retaining exhausted runs and failed retries."""
    repository = PostgresIngestionAdminRepository()
    # Future timestamps keep the fixture's registry creation from masquerading as a later edit.
    failed_at = datetime(2099, 1, 1, tzinfo=UTC)
    manual_completed_at = failed_at + timedelta(seconds=completion_offset_seconds)
    due_at = manual_completed_at + timedelta(hours=1)
    async with _ingestion_fixture() as fixture:
        failed_key = f"cadence:{fixture.live_source}:old"
        manual_key = f"admin:{uuid4()}"
        async with fixture.owner.begin() as connection:
            await connection.execute(
                text("""
                INSERT INTO public.catalog_refresh_runs (
                    source_key, run_key, status, started_at, completed_at,
                    candidate_count, canonical_count, error, attempt_count
                ) VALUES (
                    :source_key, :run_key, :status, :started_at, :completed_at,
                    :candidate_count, :canonical_count, :error, :attempt_count
                )
                """),
                [
                    {
                        "source_key": fixture.live_source,
                        "run_key": failed_key,
                        "status": "failed",
                        "started_at": failed_at - timedelta(minutes=5),
                        "completed_at": failed_at,
                        "candidate_count": None,
                        "canonical_count": None,
                        "error": "source refresh failed",
                        "attempt_count": 50,
                    },
                    {
                        "source_key": fixture.live_source,
                        "run_key": manual_key,
                        "status": manual_status,
                        "started_at": manual_completed_at - timedelta(seconds=1),
                        "completed_at": manual_completed_at,
                        "candidate_count": 1 if manual_status == "succeeded" else None,
                        "canonical_count": 1 if manual_status == "succeeded" else None,
                        "error": None if manual_status == "succeeded" else "source refresh failed",
                        "attempt_count": 1,
                    },
                ],
            )

        before_due = await repository.list_due_refreshes(due_at - timedelta(seconds=1), limit=500)
        assert fixture.live_source not in {item.source.source_key for item in before_due}
        after_due = await repository.list_due_refreshes(due_at, limit=500)
        recovered = [item for item in after_due if item.source.source_key == fixture.live_source]
        assert bool(recovered) is recovers
        if recovers:
            assert recovered[0].due_at == due_at
        assert not {item.source.source_key for item in after_due}.intersection(
            fixture.fixture_sources
        )

        async with fixture.owner.begin() as connection:
            retained = (
                await connection.execute(
                    text("""SELECT status, attempt_count FROM public.catalog_refresh_runs
                     WHERE source_key=:source_key AND run_key=:run_key"""),
                    {"source_key": fixture.live_source, "run_key": failed_key},
                )
            ).one()
            assert retained == ("failed", 50)

            if recovers:
                # A later exhausted cadence slot must still trip its own circuit breaker.
                await connection.execute(
                    text("""
                    INSERT INTO public.catalog_refresh_runs (
                        source_key, run_key, status, started_at, completed_at, error, attempt_count
                    ) VALUES (:source_key, :run_key, 'failed', :started_at, :completed_at,
                              'source refresh failed', 50)
                    """),
                    {
                        "source_key": fixture.live_source,
                        "run_key": f"cadence:{fixture.live_source}:new",
                        "started_at": due_at,
                        "completed_at": due_at + timedelta(seconds=1),
                    },
                )
        if recovers:
            after_new_failure = await repository.list_due_refreshes(
                due_at + timedelta(days=3), limit=500
            )
            assert fixture.live_source not in {item.source.source_key for item in after_new_failure}


async def test_source_registry_sort_is_global_stable_and_nulls_last(db: None) -> None:
    repository = PostgresIngestionAdminRepository()

    async with _ingestion_fixture() as fixture:
        first_fixture, second_fixture, third_fixture = fixture.fixture_sources
        suffix = fixture.live_source.removeprefix("admin-live-")
        now = datetime.now(UTC)
        live_run_key = f"sort-live:{uuid4()}"
        first_run_key = f"sort-first:{uuid4()}"
        second_run_key = f"sort-second:{uuid4()}"
        third_run_key = f"sort-third:{uuid4()}"

        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.catalog_sources
                    SET display_name = CASE source_key
                            WHEN :first_fixture THEN 'Alpha source'
                            WHEN :second_fixture THEN 'Bravo source'
                            WHEN :third_fixture THEN 'bravo source'
                            WHEN :live_source THEN 'Zulu source'
                        END,
                        enabled = CASE WHEN source_key = :first_fixture THEN false ELSE true END,
                        reviewed_at = CASE
                            WHEN source_key = :second_fixture THEN NULL
                            ELSE reviewed_at
                        END,
                        review_expires_at = CASE
                            WHEN source_key = :third_fixture THEN :expired_at
                            ELSE NULL
                        END
                    WHERE source_key = ANY(CAST(:source_keys AS text[]))
                    """
                ),
                {
                    "first_fixture": first_fixture,
                    "second_fixture": second_fixture,
                    "third_fixture": third_fixture,
                    "live_source": fixture.live_source,
                    "expired_at": now - timedelta(days=1),
                    "source_keys": [fixture.live_source, *fixture.fixture_sources],
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO public.catalog_refresh_runs (
                        source_key, run_key, status, lease_token, started_at,
                        lease_expires_at, completed_at, candidate_count,
                        canonical_count, error, attempt_count
                    )
                    VALUES
                        (
                            :first_fixture, :first_run_key, 'succeeded', NULL,
                            :first_started_at, NULL, :first_completed_at, 10, 8, NULL, 1
                        ),
                        (
                            :second_fixture, :second_run_key, 'failed', NULL,
                            :second_started_at, NULL, :second_completed_at,
                            NULL, NULL, 'provider timeout', 1
                        ),
                        (
                            :third_fixture, :third_run_key, 'paused', NULL,
                            :third_started_at, NULL, NULL, NULL, NULL, NULL, 1
                        ),
                        (
                            :live_source, :live_run_key, 'succeeded', NULL,
                            :live_started_at, NULL, :live_completed_at, 24, 20, NULL, 1
                        )
                    """
                ),
                {
                    "first_fixture": first_fixture,
                    "first_run_key": first_run_key,
                    "first_started_at": now - timedelta(minutes=31),
                    "first_completed_at": now - timedelta(minutes=30),
                    "second_fixture": second_fixture,
                    "second_run_key": second_run_key,
                    "second_started_at": now - timedelta(minutes=21),
                    "second_completed_at": now - timedelta(minutes=20),
                    "third_fixture": third_fixture,
                    "third_run_key": third_run_key,
                    "third_started_at": now - timedelta(minutes=10),
                    "live_source": fixture.live_source,
                    "live_run_key": live_run_key,
                    "live_started_at": now - timedelta(minutes=2),
                    "live_completed_at": now - timedelta(minutes=1),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO public.canonical_events (
                        canonical_event_id, title, start_at, description, price_status
                    )
                    VALUES
                        (
                            :live_event_id, 'Sorted live event',
                            :start_at, 'live source catalog evidence', 'free'
                        ),
                        (
                            :fixture_event_id, 'Sorted fixture event',
                            :start_at, 'fixture source catalog evidence', 'free'
                        )
                    """
                ),
                {
                    "live_event_id": fixture.live_event_id,
                    "fixture_event_id": fixture.fixture_event_id,
                    "start_at": datetime(2099, 1, 1, tzinfo=UTC),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO public.catalog_event_observations (
                        source_key, source, source_event_id, canonical_event_id,
                        registration_url, price_status, content_hash, last_run_key
                    )
                    VALUES
                        (
                            :live_source, 'public_jsonld', :live_source_event_id,
                            :live_event_id, 'https://events.example.com/sorted-live',
                            'free', :live_hash, :live_run_key
                        ),
                        (
                            :first_fixture, 'public_jsonld', :fixture_source_event_id,
                            :fixture_event_id, 'https://events.example.com/sorted-fixture',
                            'free', :fixture_hash, :first_run_key
                        )
                    """
                ),
                {
                    "live_source": fixture.live_source,
                    "live_source_event_id": f"sort-live-{fixture.live_event_id}",
                    "live_event_id": fixture.live_event_id,
                    "live_hash": "c" * 64,
                    "live_run_key": live_run_key,
                    "first_fixture": first_fixture,
                    "fixture_source_event_id": f"sort-fixture-{fixture.fixture_event_id}",
                    "fixture_event_id": fixture.fixture_event_id,
                    "fixture_hash": "d" * 64,
                    "first_run_key": first_run_key,
                },
            )

        expected_by_sort = {
            "source": [first_fixture, third_fixture, second_fixture, fixture.live_source],
            "health": [third_fixture, second_fixture, fixture.live_source, first_fixture],
            # Operator Catalog always excludes fixture inventory, even when the
            # source roster includes fixtures; equal zero counts use source keys.
            "catalog": [fixture.live_source, *sorted(fixture.fixture_sources)],
            "catalog_total": [fixture.live_source, *sorted(fixture.fixture_sources)],
            "last_success": [fixture.live_source, first_fixture, third_fixture, second_fixture],
            "latest_run": [second_fixture, third_fixture, first_fixture, fixture.live_source],
            "output": [fixture.live_source, first_fixture, third_fixture, second_fixture],
        }
        for sort_by, expected in expected_by_sort.items():
            page = await repository.list_sources(
                query=suffix,
                include_fixtures=True,
                sort_by=sort_by,
                sort_direction="asc" if sort_by in {"source", "health", "latest_run"} else "desc",
                limit=10,
            )
            assert page.total == 4
            assert [item.source_key for item in page.items] == expected

        paged = await repository.list_sources(
            query=suffix,
            include_fixtures=True,
            sort_by="output",
            sort_direction="desc",
            limit=2,
            offset=1,
        )
        assert paged.total == 4
        assert [item.source_key for item in paged.items] == expected_by_sort["output"][1:3]


async def test_command_replay_conflict_and_lease_fences_are_database_authoritative(
    db: None,
) -> None:
    repository = PostgresIngestionAdminRepository()

    async with _ingestion_fixture() as fixture:
        with pytest.raises(IngestionSourceNotFoundError) as missing:
            await repository.enqueue(
                uuid4(),
                IngestionCommandAction.REFRESH_SOURCE,
                f"missing-{uuid4().hex[:12]}",
                fixture.requested_by,
            )
        assert missing.value.code == "source_not_found"

        with pytest.raises(IngestionCommandUnavailableError) as fixture_rejected:
            await repository.enqueue(
                uuid4(),
                IngestionCommandAction.REFRESH_SOURCE,
                fixture.fixture_sources[0],
                fixture.requested_by,
            )
        assert fixture_rejected.value.code == "source_unavailable"

        command_id = uuid4()
        queued = await repository.enqueue(
            command_id,
            IngestionCommandAction.REFRESH_SOURCE,
            fixture.live_source,
            fixture.requested_by,
        )
        replay = await repository.enqueue(
            command_id,
            IngestionCommandAction.REFRESH_SOURCE,
            fixture.live_source,
            fixture.requested_by,
        )
        assert replay == queued

        with pytest.raises(IngestionCommandConflictError) as conflict:
            await repository.enqueue(
                command_id,
                IngestionCommandAction.REFRESH_DUE,
                None,
                fixture.requested_by,
            )
        assert conflict.value.code == "command_conflict"

        lease = (await repository.claim_batch(1, 300))[0]
        assert lease.command_id == command_id
        async with _executor_session_scope() as session:
            null_semantics_bypass = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_complete_ingestion_admin_command(
                            :command_id,
                            :lease_token,
                            CAST(:result AS jsonb)
                        )
                        """
                    ),
                    {
                        "command_id": command_id,
                        "lease_token": lease.lease_token,
                        "result": (
                            '{"action":"refresh_source","source_key":"'
                            f'{fixture.live_source}","run_key":null,"outcome":null}}'
                        ),
                    },
                )
            ).scalar_one()
        assert null_semantics_bypass is False

        assert await repository.complete(
            lease,
            {
                "action": "refresh_source",
                "source_key": fixture.live_source,
                "run_key": f"admin:{command_id}",
                "outcome": "succeeded",
                "candidate_count": 2,
                "canonical_count": 2,
            },
        )
        assert not await repository.fail(lease, "source_refresh_failed")

        second_id = uuid4()
        await repository.enqueue(
            second_id,
            IngestionCommandAction.REFRESH_DUE,
            None,
            fixture.requested_by,
        )
        stale = (await repository.claim_batch(1, 300))[0]
        assert stale.command_id == second_id
        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.ingestion_admin_commands
                    SET lease_expires_at = clock_timestamp() - INTERVAL '1 second'
                    WHERE command_id = :command_id
                    """
                ),
                {"command_id": second_id},
            )
        reclaimed = (await repository.claim_batch(1, 300))[0]
        assert reclaimed.command_id == second_id
        assert reclaimed.attempt_count == 2
        assert reclaimed.lease_token != stale.lease_token
        assert not await repository.fail(stale, "due_refresh_failed")
        assert await repository.fail(reclaimed, "due_refresh_failed")

        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.source_policy
                    SET automation_allowed =
                            jsonb_set(automation_allowed, '{browser}', 'false'::jsonb, true)
                    WHERE source = 'public_jsonld'
                    """
                )
            )
        assert (
            await repository.enqueue(
                command_id,
                IngestionCommandAction.REFRESH_SOURCE,
                fixture.live_source,
                fixture.requested_by,
            )
        ).status.value == "completed"
        with pytest.raises(IngestionCommandUnavailableError) as policy_blocked:
            await repository.enqueue(
                uuid4(),
                IngestionCommandAction.REFRESH_DUE,
                None,
                fixture.requested_by,
            )
        assert policy_blocked.value.code == "policy_blocked"


async def test_command_lease_renewal_is_live_exact_token_and_terminal_fenced(
    db: None,
) -> None:
    repository = PostgresIngestionAdminRepository()

    async with _ingestion_fixture() as fixture:
        command_id = uuid4()
        await repository.enqueue(
            command_id,
            IngestionCommandAction.REFRESH_SOURCE,
            fixture.live_source,
            fixture.requested_by,
        )
        first_lease = (await repository.claim_batch(1, 300))[0]
        assert first_lease.command_id == command_id

        async with fixture.owner.connect() as connection:
            first_expiry = (
                await connection.execute(
                    text(
                        """
                        SELECT lease_expires_at
                        FROM public.ingestion_admin_commands
                        WHERE command_id = :command_id
                        """
                    ),
                    {"command_id": command_id},
                )
            ).scalar_one()

        wrong_token = type(first_lease)(
            command_id=first_lease.command_id,
            action=first_lease.action,
            source_key=first_lease.source_key,
            attempt_count=first_lease.attempt_count,
            lease_token=uuid4(),
        )
        assert not await repository.renew_lease(wrong_token, 600)
        assert await repository.renew_lease(first_lease, 600)

        async with fixture.owner.connect() as connection:
            renewed_expiry = (
                await connection.execute(
                    text(
                        """
                        SELECT lease_expires_at
                        FROM public.ingestion_admin_commands
                        WHERE command_id = :command_id
                        """
                    ),
                    {"command_id": command_id},
                )
            ).scalar_one()
        assert renewed_expiry > first_expiry

        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.ingestion_admin_commands
                    SET lease_expires_at = clock_timestamp() - INTERVAL '1 second'
                    WHERE command_id = :command_id
                    """
                ),
                {"command_id": command_id},
            )
        assert not await repository.renew_lease(first_lease, 600)

        reclaimed = (await repository.claim_batch(1, 300))[0]
        assert reclaimed.command_id == command_id
        assert reclaimed.attempt_count == 2
        assert reclaimed.lease_token != first_lease.lease_token
        assert not await repository.renew_lease(first_lease, 600)
        assert await repository.renew_lease(reclaimed, 600)

        assert await repository.complete(
            reclaimed,
            {
                "action": "refresh_source",
                "source_key": fixture.live_source,
                "run_key": f"admin:{command_id}",
                "outcome": "succeeded",
                "candidate_count": 1,
                "canonical_count": 1,
            },
        )
        assert not await repository.renew_lease(reclaimed, 600)


async def test_command_run_links_are_lease_fenced_current_attempt_and_safe(
    db: None,
) -> None:
    repository = PostgresIngestionAdminRepository()

    async with _ingestion_fixture() as fixture:
        command_id = uuid4()
        run_key = f"admin:{command_id}"
        target = IngestionCommandRunTarget(
            position=0,
            source_key=fixture.live_source,
            run_key=run_key,
        )
        await repository.enqueue(
            command_id,
            IngestionCommandAction.REFRESH_SOURCE,
            fixture.live_source,
            fixture.requested_by,
        )
        first_lease = (await repository.claim_batch(1, 300))[0]
        assert first_lease.command_id == command_id

        with pytest.raises(RuntimeError, match="lost its live lease"):
            await repository.link_command_runs(
                command_id,
                first_lease.attempt_count,
                uuid4(),
                (target,),
            )
        await repository.link_command_runs(
            command_id,
            first_lease.attempt_count,
            first_lease.lease_token,
            (target,),
        )

        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.ingestion_admin_commands
                    SET lease_expires_at = clock_timestamp() - INTERVAL '1 second'
                    WHERE command_id = :command_id
                    """
                ),
                {"command_id": command_id},
            )
        reclaimed = (await repository.claim_batch(1, 300))[0]
        assert reclaimed.command_id == command_id
        assert reclaimed.attempt_count == first_lease.attempt_count + 1
        assert reclaimed.lease_token != first_lease.lease_token

        with pytest.raises(RuntimeError, match="lost its live lease"):
            await repository.link_command_runs(
                command_id,
                first_lease.attempt_count,
                first_lease.lease_token,
                (target,),
            )
        await repository.link_command_runs(
            command_id,
            reclaimed.attempt_count,
            reclaimed.lease_token,
            (target,),
        )

        now = datetime.now(UTC)
        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.catalog_refresh_runs (
                        source_key, run_key, status, lease_token, started_at,
                        lease_expires_at, completed_at, candidate_count,
                        canonical_count, error, attempt_count
                    ) VALUES (
                        :source_key, :run_key, 'running', :run_lease_token, :started_at,
                        :lease_expires_at, NULL, NULL, NULL, NULL, 1
                    )
                    """
                ),
                {
                    "source_key": fixture.live_source,
                    "run_key": run_key,
                    "run_lease_token": uuid4(),
                    "started_at": now,
                    "lease_expires_at": now + timedelta(minutes=5),
                },
            )
            linked_attempts = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT command_attempt
                        FROM public.ingestion_admin_command_runs
                        WHERE command_id = :command_id
                        ORDER BY command_attempt
                        """
                        ),
                        {"command_id": command_id},
                    )
                )
                .scalars()
                .all()
            )
        assert linked_attempts == [first_lease.attempt_count, reclaimed.attempt_count]

        assert await repository.complete(
            reclaimed,
            {
                "action": "refresh_source",
                "source_key": fixture.live_source,
                "run_key": run_key,
                "outcome": "queued",
                "candidate_count": 0,
                "canonical_count": 0,
            },
        )
        active_detail = await repository.get_command_detail(command_id)
        assert active_detail is not None
        assert active_detail.command.status.value == "completed"
        assert active_detail.command.attempt_count == reclaimed.attempt_count
        assert len(active_detail.runs) == 1
        assert active_detail.runs[0].status == "running"
        assert active_detail.progress.total == 1
        assert active_detail.progress.running == 1
        assert active_detail.progress.completed == 0

        raw_error = "forbidden authorization token=private-secret"
        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.catalog_refresh_runs
                    SET status = 'failed',
                        lease_token = NULL,
                        lease_expires_at = NULL,
                        completed_at = clock_timestamp(),
                        error = :raw_error
                    WHERE source_key = :source_key
                      AND run_key = :run_key
                    """
                ),
                {
                    "source_key": fixture.live_source,
                    "run_key": run_key,
                    "raw_error": raw_error,
                },
            )

        failed_detail = await repository.get_command_detail(command_id)
        assert failed_detail is not None
        assert len(failed_detail.runs) == 1
        assert failed_detail.runs[0].status == "failed"
        assert failed_detail.runs[0].phase == "failed"
        assert failed_detail.runs[0].error_code == "source_access_denied"
        assert failed_detail.progress.running == 0
        assert failed_detail.progress.failed == 1
        assert failed_detail.progress.completed == 1
        assert "private-secret" not in repr(failed_detail)


async def test_active_command_targets_are_atomically_deduplicated_across_sessions(
    db: None,
) -> None:
    """Distinct browser UUIDs cannot reserve the same active source or fleet slot."""
    first_repository = PostgresIngestionAdminRepository()
    second_repository = PostgresIngestionAdminRepository()

    async with _ingestion_fixture() as fixture:

        async def admit(
            repository: PostgresIngestionAdminRepository,
            command_id: UUID,
            action: IngestionCommandAction,
            source_key: str | None,
        ) -> tuple[str, UUID | None]:
            try:
                command = await repository.enqueue(
                    command_id,
                    action,
                    source_key,
                    fixture.requested_by,
                )
            except IngestionCommandConflictError:
                return "conflict", None
            return "accepted", command.command_id

        source_ids = (uuid4(), uuid4())
        source_outcomes = await asyncio.gather(
            admit(
                first_repository,
                source_ids[0],
                IngestionCommandAction.REFRESH_SOURCE,
                fixture.live_source,
            ),
            admit(
                second_repository,
                source_ids[1],
                IngestionCommandAction.REFRESH_SOURCE,
                fixture.live_source,
            ),
        )
        assert sorted(outcome for outcome, _ in source_outcomes) == [
            "accepted",
            "conflict",
        ]
        active_source_id = next(
            command_id
            for outcome, command_id in source_outcomes
            if outcome == "accepted" and command_id is not None
        )

        due_ids = (uuid4(), uuid4())
        due_outcomes = await asyncio.gather(
            admit(
                first_repository,
                due_ids[0],
                IngestionCommandAction.REFRESH_DUE,
                None,
            ),
            admit(
                second_repository,
                due_ids[1],
                IngestionCommandAction.REFRESH_DUE,
                None,
            ),
        )
        assert sorted(outcome for outcome, _ in due_outcomes) == [
            "accepted",
            "conflict",
        ]
        active_due_id = next(
            command_id
            for outcome, command_id in due_outcomes
            if outcome == "accepted" and command_id is not None
        )

        exact_replays = await asyncio.gather(
            admit(
                first_repository,
                active_source_id,
                IngestionCommandAction.REFRESH_SOURCE,
                fixture.live_source,
            ),
            admit(
                second_repository,
                active_source_id,
                IngestionCommandAction.REFRESH_SOURCE,
                fixture.live_source,
            ),
        )
        assert exact_replays == [
            ("accepted", active_source_id),
            ("accepted", active_source_id),
        ]

        async with fixture.owner.connect() as connection:
            active_rows = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT action, source_key, count(*) AS active_count
                            FROM public.ingestion_admin_commands
                            WHERE requested_by = :requested_by
                              AND status IN ('queued', 'running')
                            GROUP BY action, source_key
                            ORDER BY action, source_key
                            """
                        ),
                        {"requested_by": fixture.requested_by},
                    )
                )
                .mappings()
                .all()
            )
        assert [(row["action"], row["source_key"], row["active_count"]) for row in active_rows] == [
            ("refresh_due", None, 1),
            ("refresh_source", fixture.live_source, 1),
        ]

        leases = await first_repository.claim_batch(10, 300)
        assert {lease.command_id for lease in leases} == {
            active_source_id,
            active_due_id,
        }
        for lease in leases:
            error_code = (
                "source_refresh_failed"
                if lease.action is IngestionCommandAction.REFRESH_SOURCE
                else "due_refresh_failed"
            )
            assert await first_repository.fail(lease, error_code)

        replacement_source = await first_repository.enqueue(
            uuid4(),
            IngestionCommandAction.REFRESH_SOURCE,
            fixture.live_source,
            fixture.requested_by,
        )
        replacement_due = await second_repository.enqueue(
            uuid4(),
            IngestionCommandAction.REFRESH_DUE,
            None,
            fixture.requested_by,
        )
        assert replacement_source.command_id not in source_ids
        assert replacement_due.command_id not in due_ids


async def test_deferred_command_is_not_claimed_until_available_and_remains_fenced(  # noqa: PLR0915
    db: None,
) -> None:
    repository = PostgresIngestionAdminRepository()

    async with _ingestion_fixture() as fixture:
        command_id = uuid4()
        await repository.enqueue(
            command_id,
            IngestionCommandAction.REFRESH_SOURCE,
            fixture.live_source,
            fixture.requested_by,
        )
        first_lease = (
            await repository.claim_batch(
                1,
                300,
                "worker-one",
                "sha256:" + "1" * 64,
            )
        )[0]
        assert first_lease.command_id == command_id
        first_command = await repository.get_command(command_id)
        assert first_command is not None
        assert first_command.executor_release_revision == "worker-one"

        async with _executor_session_scope() as session:
            invalid_delays = (
                (
                    await session.execute(
                        text(
                            """
                            SELECT
                                public.fn_defer_ingestion_admin_command(
                                    :command_id, :lease_token, 0
                                ) AS zero_delay,
                                public.fn_defer_ingestion_admin_command(
                                    :command_id, :lease_token, 21601
                                ) AS excessive_delay
                            """
                        ),
                        {
                            "command_id": first_lease.command_id,
                            "lease_token": first_lease.lease_token,
                        },
                    )
                )
                .mappings()
                .one()
            )
        assert invalid_delays["zero_delay"] is False
        assert invalid_delays["excessive_delay"] is False
        stale_lease = type(first_lease)(
            command_id=first_lease.command_id,
            action=first_lease.action,
            source_key=first_lease.source_key,
            attempt_count=first_lease.attempt_count,
            lease_token=uuid4(),
        )
        assert not await repository.defer(stale_lease, 60)
        deferred_at = datetime.now(UTC)
        assert await repository.defer(first_lease, 60)

        async with fixture.owner.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT status, available_at, started_at, completed_at,
                                   attempt_count, lease_token, lease_expires_at,
                                   result, error_code
                            FROM public.ingestion_admin_commands
                            WHERE command_id = :command_id
                            """
                        ),
                        {"command_id": command_id},
                    )
                )
                .mappings()
                .one()
            )
        assert row["status"] == "queued"
        assert row["available_at"] >= deferred_at + timedelta(seconds=59)
        assert row["started_at"] is None
        assert row["completed_at"] is None
        assert row["attempt_count"] == 1
        assert row["lease_token"] is None
        assert row["lease_expires_at"] is None
        assert row["result"] is None
        assert row["error_code"] is None

        assert await repository.claim_batch(1, 300) == ()
        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.ingestion_admin_commands
                    SET available_at = clock_timestamp() - INTERVAL '1 second'
                    WHERE command_id = :command_id
                    """
                ),
                {"command_id": command_id},
            )
        second_lease = (
            await repository.claim_batch(
                1,
                300,
                "worker-two",
                "sha256:" + "2" * 64,
            )
        )[0]
        assert second_lease.command_id == command_id
        assert second_lease.attempt_count == 2
        assert second_lease.lease_token != first_lease.lease_token
        second_command = await repository.get_command(command_id)
        assert second_command is not None
        assert second_command.executor_release_revision == "worker-two"
        assert not await repository.defer(first_lease, 1)

        # A future availability timestamp gates queued work only. Once a running lease expires,
        # the existing reclaim path must remain authoritative regardless of that timestamp.
        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.ingestion_admin_commands
                    SET available_at = clock_timestamp() + INTERVAL '1 day',
                        lease_expires_at = clock_timestamp() - INTERVAL '1 second'
                    WHERE command_id = :command_id
                    """
                ),
                {"command_id": command_id},
            )
        reclaimed = (
            await repository.claim_batch(
                1,
                300,
                "worker-three",
                "sha256:" + "3" * 64,
            )
        )[0]
        assert reclaimed.command_id == command_id
        assert reclaimed.attempt_count == 3
        assert reclaimed.lease_token != second_lease.lease_token
        reclaimed_command = await repository.get_command(command_id)
        assert reclaimed_command is not None
        assert reclaimed_command.executor_release_revision == "worker-three"
        assert reclaimed_command.executor_image_digest == "sha256:" + "3" * 64
        assert not await repository.defer(second_lease, 1)
        assert await repository.defer(reclaimed, 1)


async def test_expired_runs_are_failed_and_raw_errors_never_cross_capability(db: None) -> None:
    repository = PostgresIngestionAdminRepository()

    async with _ingestion_fixture() as fixture:
        baseline = await repository.overview()
        stale_run_key = f"stale:{uuid4()}"
        raw_run_key = f"raw:{uuid4()}"
        now = datetime.now(UTC)
        expired_at = now - timedelta(minutes=5)
        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.catalog_refresh_runs (
                        source_key, run_key, status, lease_token, started_at,
                        lease_expires_at, completed_at, candidate_count,
                        canonical_count, error, attempt_count
                    )
                    VALUES
                        (
                            :source_key, :stale_run_key, 'running', :lease_token,
                            :stale_started_at, :expired_at, NULL, NULL, NULL, NULL, 1
                        ),
                        (
                            :source_key, :raw_run_key, 'failed', NULL,
                            :raw_started_at, NULL, :raw_completed_at, NULL, NULL,
                            'forbidden authorization token=private-secret', 1
                        )
                    """
                ),
                {
                    "source_key": fixture.live_source,
                    "stale_run_key": stale_run_key,
                    "lease_token": uuid4(),
                    "stale_started_at": now - timedelta(minutes=10),
                    "expired_at": expired_at,
                    "raw_run_key": raw_run_key,
                    "raw_started_at": now - timedelta(minutes=20),
                    "raw_completed_at": now - timedelta(minutes=19),
                },
            )

        failed = await repository.list_runs(
            status="failed",
            source_key=fixture.live_source,
        )
        by_key = {run.run_key: run for run in failed.items}
        assert failed.total == 2
        assert by_key[stale_run_key].status == "failed"
        assert by_key[stale_run_key].error == "lease_lost"
        assert by_key[stale_run_key].completed_at == expired_at
        assert by_key[raw_run_key].error == "source_access_denied"
        assert "private-secret" not in repr(failed)

        running = await repository.list_runs(
            status="running",
            source_key=fixture.live_source,
        )
        assert running.total == 0
        source = await repository.list_sources(source_key=fixture.live_source)
        assert source.items[0].effective_status.value == "due"
        assert source.items[0].latest_run is not None
        assert source.items[0].latest_run.error == "lease_lost"

        overview = await repository.overview()
        assert overview.summary.running_runs == baseline.summary.running_runs
        assert overview.summary.failed_runs_24h == baseline.summary.failed_runs_24h + 1


async def test_run_latest_and_resolution_are_derived_before_filters_and_pagination(
    db: None,
) -> None:
    repository = PostgresIngestionAdminRepository()

    async with _ingestion_fixture() as fixture:
        baseline = await repository.overview()
        old_failed_key = f"resolution:old:{uuid4()}"
        tie_failed_key = f"resolution:a:{uuid4()}"
        tie_success_key = f"resolution:z:{uuid4()}"
        old_started_at = datetime.now(UTC) - timedelta(minutes=10)
        tied_started_at = old_started_at + timedelta(minutes=5)
        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.catalog_refresh_runs (
                        source_key, run_key, status, lease_token, started_at,
                        lease_expires_at, completed_at, candidate_count,
                        canonical_count, error, attempt_count
                    )
                    VALUES
                        (
                            :source_key, :old_failed_key, 'failed', NULL,
                            :old_started_at, NULL, :old_completed_at, NULL, NULL,
                            'source refresh failed', 1
                        ),
                        (
                            :source_key, :tie_failed_key, 'failed', NULL,
                            :tied_started_at, NULL, :tied_completed_at, NULL, NULL,
                            'source refresh failed', 1
                        )
                    """
                ),
                {
                    "source_key": fixture.live_source,
                    "old_failed_key": old_failed_key,
                    "old_started_at": old_started_at,
                    "old_completed_at": old_started_at + timedelta(seconds=1),
                    "tie_failed_key": tie_failed_key,
                    "tied_started_at": tied_started_at,
                    "tied_completed_at": tied_started_at + timedelta(seconds=1),
                },
            )

        failed_before_success = await repository.list_runs(
            status="failed",
            source_key=fixture.live_source,
            window_hours=24,
        )

        assert [run.run_key for run in failed_before_success.items] == [
            tie_failed_key,
            old_failed_key,
        ]
        assert failed_before_success.items[0].is_latest_for_source is True
        assert failed_before_success.items[0].resolved_by_newer_success is False
        assert failed_before_success.items[1].is_latest_for_source is False
        assert failed_before_success.items[1].resolved_by_newer_success is False
        failed_overview = await repository.overview()
        assert failed_overview.summary.failed_runs_24h == baseline.summary.failed_runs_24h + 1

        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.catalog_refresh_runs (
                        source_key, run_key, status, lease_token, started_at,
                        lease_expires_at, completed_at, candidate_count,
                        canonical_count, error, attempt_count
                    )
                    VALUES (
                        :source_key, :run_key, 'succeeded', NULL, :started_at,
                        NULL, :completed_at, 1, 1, NULL, 1
                    )
                    """
                ),
                {
                    "source_key": fixture.live_source,
                    "run_key": tie_success_key,
                    "started_at": tied_started_at,
                    "completed_at": tied_started_at + timedelta(seconds=2),
                },
            )

        all_runs = await repository.list_runs(
            source_key=fixture.live_source,
            window_hours=24,
        )
        assert [run.run_key for run in all_runs.items] == [
            tie_success_key,
            tie_failed_key,
            old_failed_key,
        ]
        assert all_runs.items[0].is_latest_for_source is True
        assert all_runs.items[0].resolved_by_newer_success is False

        failed_after_success = await repository.list_runs(
            status="failed",
            source_key=fixture.live_source,
            window_hours=24,
        )
        assert [run.run_key for run in failed_after_success.items] == [
            tie_failed_key,
            old_failed_key,
        ]
        assert all(run.is_latest_for_source is False for run in failed_after_success.items)
        assert all(run.resolved_by_newer_success is True for run in failed_after_success.items)
        resolved_overview = await repository.overview()
        assert resolved_overview.summary.failed_runs_24h == baseline.summary.failed_runs_24h

        second_failed_page = await repository.list_runs(
            status="failed",
            source_key=fixture.live_source,
            window_hours=24,
            limit=1,
            offset=1,
        )
        assert [run.run_key for run in second_failed_page.items] == [old_failed_key]
        assert second_failed_page.items[0].is_latest_for_source is False
        assert second_failed_page.items[0].resolved_by_newer_success is True


async def test_historical_test_runs_are_projection_only_fixtures(db: None) -> None:
    repository = PostgresIngestionAdminRepository()

    async with _ingestion_fixture() as fixture:
        baseline = await repository.overview()
        legitimate_run_key = f"legitimate:{uuid4()}"
        manual_run_key = f"manual:p1222:{uuid4()}"
        fixture_error_run_key = f"quality-artifact:{uuid4()}"
        now = datetime.now(UTC)
        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.catalog_refresh_runs (
                        source_key, run_key, status, lease_token, started_at,
                        lease_expires_at, completed_at, candidate_count,
                        canonical_count, error, attempt_count
                    )
                    VALUES
                        (
                            :source_key, :legitimate_run_key, 'failed', NULL,
                            '2002-01-01 00:00:00+00', NULL,
                            '2002-01-01 00:01:00+00', NULL, NULL,
                            'source timed out', 1
                        ),
                        (
                            :source_key, :manual_run_key, 'succeeded', NULL,
                            :manual_started_at, NULL, :manual_completed_at,
                            1, 1, NULL, 1
                        ),
                        (
                            :source_key, :fixture_error_run_key, 'failed', NULL,
                            :fixture_started_at, NULL, :fixture_completed_at,
                            NULL, NULL, 'integration fixture forced failure', 1
                        )
                    """
                ),
                {
                    "source_key": fixture.live_source,
                    "legitimate_run_key": legitimate_run_key,
                    "manual_run_key": manual_run_key,
                    "manual_started_at": now - timedelta(minutes=10),
                    "manual_completed_at": now - timedelta(minutes=9),
                    "fixture_error_run_key": fixture_error_run_key,
                    "fixture_started_at": now - timedelta(minutes=5),
                    "fixture_completed_at": now - timedelta(minutes=4),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO public.canonical_events (
                        canonical_event_id, title, start_at, description, price_status
                    )
                    VALUES
                        (
                            :live_event_id, 'Legitimate-run admin event',
                            :start_at, 'visible catalog observation', 'free'
                        ),
                        (
                            :fixture_event_id, 'Manual-run admin artifact',
                            :start_at, 'hidden catalog observation', 'free'
                        )
                    """
                ),
                {
                    "live_event_id": fixture.live_event_id,
                    "fixture_event_id": fixture.fixture_event_id,
                    "start_at": datetime(2099, 1, 1, tzinfo=UTC),
                },
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO public.catalog_event_observations (
                        source_key, source, source_event_id, canonical_event_id,
                        registration_url, price_status, content_hash, last_run_key
                    )
                    VALUES
                        (
                            :source_key, 'public_jsonld', :legitimate_source_event_id,
                            :live_event_id, 'https://events.example.com/legitimate-admin-event',
                            'free', :legitimate_hash, :legitimate_run_key
                        ),
                        (
                            :source_key, 'public_jsonld', :manual_source_event_id,
                            :fixture_event_id, 'https://events.example.com/manual-admin-artifact',
                            'free', :manual_hash, :manual_run_key
                        )
                    """
                ),
                {
                    "source_key": fixture.live_source,
                    "legitimate_source_event_id": f"legitimate-{fixture.live_event_id}",
                    "live_event_id": fixture.live_event_id,
                    "legitimate_hash": "c" * 64,
                    "legitimate_run_key": legitimate_run_key,
                    "manual_source_event_id": f"manual-{fixture.fixture_event_id}",
                    "fixture_event_id": fixture.fixture_event_id,
                    "manual_hash": "d" * 64,
                    "manual_run_key": manual_run_key,
                },
            )

        projected_runs = await repository.list_runs(source_key=fixture.live_source)
        forensic_runs = await repository.list_runs(
            source_key=fixture.live_source,
            include_fixtures=True,
        )
        assert projected_runs.total == 1
        assert projected_runs.items[0].run_key == legitimate_run_key
        assert {run.run_key for run in forensic_runs.items} == {
            legitimate_run_key,
            manual_run_key,
            fixture_error_run_key,
        }
        assert (
            next(run for run in forensic_runs.items if run.run_key == fixture_error_run_key).error
            == "test_fixture"
        )

        projected_source = (await repository.list_sources(source_key=fixture.live_source)).items[0]
        forensic_source = (
            await repository.list_sources(
                source_key=fixture.live_source,
                include_fixtures=True,
            )
        ).items[0]
        assert projected_source.latest_run is not None
        assert projected_source.latest_run.run_key == legitimate_run_key
        assert projected_source.latest_run.error == "source_timeout"
        assert projected_source.last_succeeded_at is None
        assert projected_source.due is True
        assert projected_source.event_count == 1
        assert forensic_source.latest_run is not None
        assert forensic_source.latest_run.run_key == fixture_error_run_key
        assert forensic_source.latest_run.error == "test_fixture"
        assert forensic_source.last_succeeded_at == now - timedelta(minutes=9)
        assert forensic_source.event_count == 2

        due = await repository.list_due_refreshes(now, limit=500)
        assert fixture.live_source in {item.source.source_key for item in due}
        async with _controller_session_scope() as session:
            exact_artifact_count = (
                await session.execute(
                    text(
                        """
                        SELECT count(*)
                        FROM public.fn_get_ingestion_admin_run(:source_key, :run_key)
                        """
                    ),
                    {"source_key": fixture.live_source, "run_key": manual_run_key},
                )
            ).scalar_one()
        assert exact_artifact_count == 0
        async with fixture.owner.connect() as connection:
            paged_race_artifact = (
                await connection.execute(
                    text(
                        """
                        SELECT public.fn_ingestion_admin_run_is_fixture(
                            'manual:paged-stage-contract-race-example',
                            NULL
                        )
                        """
                    )
                )
            ).scalar_one()
        assert paged_race_artifact is True

        overview = await repository.overview()
        assert overview.latest_success_at == baseline.latest_success_at
        assert overview.summary.failed_runs_24h == baseline.summary.failed_runs_24h
        assert overview.summary.catalog_events == baseline.summary.catalog_events + 1


async def test_source_analysis_filters_and_new_command_provenance_are_bounded(  # noqa: PLR0915
    db: None,
) -> None:
    repository = PostgresIngestionAdminRepository()
    accepted_image_digest = "sha256:" + "a" * 64
    executor_image_digest = "sha256:" + "b" * 64

    async with _ingestion_fixture() as fixture:
        command_id = uuid4()
        command = await repository.enqueue(
            command_id,
            IngestionCommandAction.REFRESH_SOURCE,
            fixture.live_source,
            fixture.requested_by,
            "rev-abc123",
            accepted_image_digest,
        )
        assert command.source_revision == 1
        assert command.release_revision == "rev-abc123"
        assert command.image_digest == accepted_image_digest
        assert command.executor_source_revision is None
        assert command.executor_release_revision is None

        replay = await repository.enqueue(
            command_id,
            IngestionCommandAction.REFRESH_SOURCE,
            fixture.live_source,
            fixture.requested_by,
            "rev-after-api-deploy",
            "sha256:" + "c" * 64,
        )
        assert replay.release_revision == "rev-abc123"
        assert replay.image_digest == accepted_image_digest

        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.catalog_sources
                    SET min_interval_ms = 1600
                    WHERE source_key = :source_key
                    """
                ),
                {"source_key": fixture.live_source},
            )
        lease = (
            await repository.claim_batch(
                1,
                300,
                "worker-rev-def456",
                executor_image_digest,
            )
        )[0]
        assert lease.command_id == command_id
        claimed_command = await repository.get_command(command_id)
        assert claimed_command is not None
        assert claimed_command.source_revision == 1
        assert claimed_command.release_revision == "rev-abc123"
        assert claimed_command.executor_source_revision == 2
        assert claimed_command.executor_release_revision == "worker-rev-def456"
        assert claimed_command.executor_image_digest == executor_image_digest

        now = datetime.now(UTC)
        admin_run_key = f"admin:{command_id}"
        legacy_run_key = f"legacy:{uuid4()}"
        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.catalog_refresh_runs (
                        source_key, run_key, status, lease_token, started_at,
                        lease_expires_at, completed_at, candidate_count,
                        canonical_count, error, attempt_count
                    )
                    VALUES
                        (
                            :source_key, :admin_run_key, 'succeeded', NULL,
                            :admin_started_at, NULL, :admin_completed_at, 10, 8, NULL, 1
                        ),
                        (
                            :source_key, :legacy_run_key, 'failed', NULL,
                            :legacy_started_at, NULL, :legacy_completed_at,
                            NULL, NULL, 'provider timeout with private detail', 2
                        )
                    """
                ),
                {
                    "source_key": fixture.live_source,
                    "admin_run_key": admin_run_key,
                    "admin_started_at": now - timedelta(minutes=5, seconds=5),
                    "admin_completed_at": now - timedelta(minutes=5),
                    "legacy_run_key": legacy_run_key,
                    "legacy_started_at": now - timedelta(minutes=10, seconds=1),
                    "legacy_completed_at": now - timedelta(minutes=10),
                },
            )

        metadata = await repository.get_filter_metadata()
        assert any(item.value == "public_jsonld" for item in metadata.modes)
        assert any(item.value == "Reviewed Publisher" for item in metadata.publishers)
        assert any(item.value == "bay_area_9_county" for item in metadata.regions)
        contextual_metadata = await repository.get_filter_metadata(
            query=fixture.live_source,
            state="active",
            mode="public_jsonld",
            publisher="Reviewed Publisher",
            region="bay_area_9_county",
        )
        assert contextual_metadata.modes == (IngestionFilterValue(value="public_jsonld", count=1),)
        assert contextual_metadata.publishers == (
            IngestionFilterValue(value="Reviewed Publisher", count=1),
        )
        assert contextual_metadata.regions == (
            IngestionFilterValue(value="bay_area_9_county", count=1),
        )

        filtered_sources = await repository.list_sources(
            mode="public_jsonld",
            publisher="Reviewed Publisher",
            region="bay_area_9_county",
            source_key=fixture.live_source,
        )
        assert [item.source_key for item in filtered_sources.items] == [fixture.live_source]
        source_list_latest = filtered_sources.items[0].latest_run
        assert source_list_latest is not None
        assert source_list_latest.run_key == admin_run_key

        filtered_runs = await repository.list_runs(
            source_key=fixture.live_source,
            mode="public_jsonld",
            publisher="Reviewed Publisher",
            region="bay_area_9_county",
            window_hours=24,
        )
        assert filtered_runs.total == 2
        admin_run = next(item for item in filtered_runs.items if item.run_key == admin_run_key)
        legacy_run = next(item for item in filtered_runs.items if item.run_key == legacy_run_key)
        assert admin_run.duration_ms == 5_000
        assert admin_run.source_revision == 2
        assert admin_run.release_revision == "worker-rev-def456"
        assert admin_run.image_digest == executor_image_digest
        assert admin_run.provenance_status == "claim_recorded"
        assert admin_run.trigger == "admin_source"
        assert (
            source_list_latest.duration_ms,
            source_list_latest.source_revision,
            source_list_latest.release_revision,
            source_list_latest.image_digest,
            source_list_latest.provenance_status,
            source_list_latest.trigger,
        ) == (
            admin_run.duration_ms,
            admin_run.source_revision,
            admin_run.release_revision,
            admin_run.image_digest,
            admin_run.provenance_status,
            admin_run.trigger,
        )
        assert legacy_run.error == "source_timeout"
        assert legacy_run.source_revision is None
        assert legacy_run.release_revision is None
        assert legacy_run.image_digest is None
        assert legacy_run.provenance_status == "legacy_unavailable"
        assert legacy_run.trigger == "cadence_or_manual"

        detail = await repository.get_source_detail(
            fixture.live_source,
            window_hours=168,
            bucket_hours=24,
        )
        assert detail is not None
        assert detail.source.seed_host == "events.example.com"
        assert detail.source.approved_origins == ("https://events.example.com",)
        assert detail.source.source_revision == 2
        assert detail.source.refresh_interval_minutes == 60
        assert detail.source.min_interval_ms == 1_600
        assert detail.source.page_limit == 1
        assert detail.summary.total_runs == 2
        assert detail.summary.succeeded_runs == 1
        assert detail.summary.failed_runs == 1
        assert detail.summary.success_rate == 0.5
        assert detail.summary.candidate_count == 10
        assert detail.summary.canonical_count == 8
        assert detail.summary.yield_rate == 0.8
        detail_latest = detail.source.latest_run
        assert detail_latest is not None
        assert detail_latest.run_key == admin_run_key
        assert (
            detail_latest.duration_ms,
            detail_latest.source_revision,
            detail_latest.release_revision,
            detail_latest.image_digest,
            detail_latest.provenance_status,
            detail_latest.trigger,
        ) == (
            admin_run.duration_ms,
            admin_run.source_revision,
            admin_run.release_revision,
            admin_run.image_digest,
            admin_run.provenance_status,
            admin_run.trigger,
        )
        assert len(detail.history) == 7
        assert sum(bucket.total_runs for bucket in detail.history) == 2
        assert {item.run_key for item in detail.recent_runs} == {
            admin_run_key,
            legacy_run_key,
        }
        detail_runs = {item.run_key: item for item in detail.recent_runs}
        assert detail_runs[admin_run_key].is_latest_for_source is True
        assert detail_runs[admin_run_key].resolved_by_newer_success is False
        assert detail_runs[legacy_run_key].is_latest_for_source is False
        assert detail_runs[legacy_run_key].resolved_by_newer_success is True

        # Emulate a worker that persisted the successful run and then lost its command lease
        # before completing the receipt. A reclaimer can only observe prior success, so the build
        # is deliberately labelled claim-recorded rather than causal execution provenance.
        async with fixture.owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.ingestion_admin_commands
                    SET lease_expires_at = clock_timestamp() - INTERVAL '1 second'
                    WHERE command_id = :command_id
                    """
                ),
                {"command_id": command_id},
            )
        reclaimed = (
            await repository.claim_batch(
                1,
                300,
                "already-succeeded-observer",
                "sha256:" + "d" * 64,
            )
        )[0]
        assert reclaimed.command_id == command_id
        reprojected = await repository.list_runs(
            source_key=fixture.live_source,
            window_hours=24,
        )
        observed_success = next(item for item in reprojected.items if item.run_key == admin_run_key)
        assert observed_success.release_revision == "already-succeeded-observer"
        assert observed_success.provenance_status == "claim_recorded"


async def test_consumer_role_has_no_ingestion_admin_function_grants(db: None) -> None:
    async with system_session_scope() as session:
        row = (
            (
                await session.execute(
                    text(
                        """
                    SELECT
                        has_table_privilege(
                            current_user,
                            'public.ingestion_admin_commands',
                            'SELECT'
                        ) AS table_select,
                        has_table_privilege(
                            current_user,
                            'public.ingestion_admin_commands',
                            'INSERT,UPDATE,DELETE'
                        ) AS table_write,
                        has_function_privilege(
                            current_user,
                            'public.fn_claim_ingestion_admin_commands(integer,integer)',
                            'EXECUTE'
                        ) AS claim_execute,
                        has_function_privilege(
                            current_user,
                            'public.fn_complete_ingestion_admin_command(uuid,uuid,jsonb)',
                            'EXECUTE'
                        ) AS complete_execute,
                        has_function_privilege(
                            current_user,
                            'public.fn_defer_ingestion_admin_command(uuid,uuid,integer)',
                            'EXECUTE'
                        ) AS defer_execute,
                        has_function_privilege(
                            current_user,
                            'public.fn_renew_ingestion_admin_command_lease(uuid,uuid,integer)',
                            'EXECUTE'
                        ) AS renew_execute,
                        has_function_privilege(
                            current_user,
                            'public.fn_ingestion_admin_run_is_fixture(text,text)',
                            'EXECUTE'
                        ) AS fixture_helper_execute,
                        has_function_privilege(
                            current_user,
                            'public.fn_ingestion_admin_run_facts_v2(boolean,text,timestamptz)',
                            'EXECUTE'
                        ) AS run_facts_execute,
                        has_function_privilege(
                            current_user,
                            'public.fn_enqueue_ingestion_admin_command(uuid,text,text,text)',
                            'EXECUTE'
                        ) AS legacy_enqueue_execute,
                        has_function_privilege(
                            current_user,
                            'public.fn_enqueue_ingestion_admin_command_v2(uuid,text,text,text,text,text)',
                            'EXECUTE'
                        ) AS provenance_enqueue_execute,
                        has_function_privilege(
                            current_user,
                            'public.fn_claim_ingestion_admin_commands(integer,integer)',
                            'EXECUTE'
                        ) AS legacy_claim_execute,
                        has_function_privilege(
                            current_user,
                            'public.fn_claim_ingestion_admin_commands_v2(integer,integer,text,text)',
                            'EXECUTE'
                        ) AS provenance_claim_execute,
                        has_function_privilege(
                            current_user,
                            'public.fn_get_ingestion_admin_source_detail(text,boolean)',
                            'EXECUTE'
                        ) AS source_detail_execute,
                        has_function_privilege(
                            current_user,
                            'public.fn_list_ingestion_admin_sources_v3(text,text,text,text,text,text,boolean,integer,integer)',
                            'EXECUTE'
                        ) AS source_list_v3_execute,
                        has_function_privilege(
                            current_user,
                            'public.fn_list_ingestion_admin_runs_v3(text,text,text,text,text,integer,boolean,integer,integer)',
                            'EXECUTE'
                        ) AS run_list_v3_execute,
                        NOT EXISTS (
                            SELECT 1
                            FROM pg_catalog.pg_proc AS proc
                            JOIN pg_catalog.pg_namespace AS namespace
                              ON namespace.oid = proc.pronamespace
                            CROSS JOIN LATERAL pg_catalog.aclexplode(
                                COALESCE(
                                    proc.proacl,
                                    pg_catalog.acldefault('f', proc.proowner)
                                )
                            ) AS acl
                            WHERE namespace.nspname = 'public'
                              AND proc.proname = 'fn_list_ingestion_admin_sources_v3'
                              AND pg_catalog.pg_get_function_identity_arguments(proc.oid)
                                  = 'text, text, text, text, text, text, boolean, integer, integer'
                              AND acl.grantee = 0
                              AND acl.privilege_type = 'EXECUTE'
                        ) AS source_list_v3_public_execute_revoked,
                        NOT EXISTS (
                            SELECT 1
                            FROM pg_catalog.pg_proc AS proc
                            JOIN pg_catalog.pg_namespace AS namespace
                              ON namespace.oid = proc.pronamespace
                            CROSS JOIN LATERAL pg_catalog.aclexplode(
                                COALESCE(
                                    proc.proacl,
                                    pg_catalog.acldefault('f', proc.proowner)
                                )
                            ) AS acl
                            WHERE namespace.nspname = 'public'
                              AND proc.proname = 'fn_list_ingestion_admin_runs_v3'
                              AND pg_catalog.pg_get_function_identity_arguments(proc.oid)
                                  = 'text, text, text, text, text, integer, boolean, integer, integer'
                              AND acl.grantee = 0
                              AND acl.privilege_type = 'EXECUTE'
                        ) AS run_list_v3_public_execute_revoked
                    """
                    )
                )
            )
            .mappings()
            .one()
        )

    assert row["table_select"] is False
    assert row["table_write"] is False
    assert row["claim_execute"] is False
    assert row["complete_execute"] is False
    assert row["defer_execute"] is False
    assert row["renew_execute"] is False
    assert row["fixture_helper_execute"] is False
    assert row["run_facts_execute"] is False
    assert row["legacy_enqueue_execute"] is False
    assert row["provenance_enqueue_execute"] is False
    assert row["legacy_claim_execute"] is False
    assert row["provenance_claim_execute"] is False
    assert row["source_detail_execute"] is False
    assert row["source_list_v3_execute"] is False
    assert row["source_list_v3_public_execute_revoked"] is True
    assert row["run_list_v3_execute"] is False
    assert row["run_list_v3_public_execute_revoked"] is True


async def test_runtime_count_capabilities_reject_unbounded_inputs(db: None) -> None:
    with pytest.raises(DBAPIError, match="invalid"):
        async with _controller_session_scope() as session:
            await session.execute(
                text(
                    """
                    SELECT public.fn_count_ingestion_admin_sources_v2(
                        NULL, 'all', :mode, NULL, NULL, NULL, false
                    )
                    """
                ),
                {"mode": "x" * 81},
            )

    with pytest.raises(DBAPIError, match="invalid"):
        async with _controller_session_scope() as session:
            await session.execute(
                text(
                    """
                    SELECT public.fn_count_ingestion_admin_runs_v2(
                        NULL, NULL, NULL, NULL, NULL, 2161, false
                    )
                    """
                )
            )
