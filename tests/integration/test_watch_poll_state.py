"""PostgreSQL watch-poll cursor contracts (ADR-008/NFR-17)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.adapters.postgres.watch_polls import PostgresWatchPollRepository
from events_concierge.composition import build_container
from events_concierge.config import get_settings
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import Source
from events_concierge.domain.events import CandidateEvent
from events_concierge.infra.db import system_session_scope
from events_concierge.ports.change_detection import WatchedEvent

pytestmark = pytest.mark.integration


class _OwnerWatchPollStateRow(Protocol):
    """Private owner-only projection used to prove stale mutations are true no-ops."""

    first_seen_at: datetime
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    consecutive_failures: int
    next_due_at: datetime
    attempt_count: int
    lease_token: str | None
    lease_expires_at: datetime | None
    last_error_type: str | None


@dataclass(frozen=True, slots=True)
class _OwnerWatchPollSnapshot:
    """Every durable watch-poll cursor field mutable by a terminal capability."""

    first_seen_at: datetime
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    consecutive_failures: int
    next_due_at: datetime
    attempt_count: int
    lease_token: str | None
    lease_expires_at: datetime | None
    last_error_type: str | None


async def test_watch_poll_cursor_is_capability_only_for_the_app_role(db: None) -> None:
    """The app has five fixed operations, not arbitrary public cursor read/write authority."""
    async with system_session_scope() as session:
        privileges = dict(
            (
                await session.execute(
                    text(
                        """
                        SELECT has_table_privilege(
                                   current_user,
                                   'public.watch_poll_state',
                                   'SELECT,INSERT,UPDATE,DELETE'
                               ) AS table_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_claim_watch_poll(uuid,text,timestamptz,integer,text)',
                                   'EXECUTE'
                               ) AS claim_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_has_live_watch_poll_lease(uuid,text,text)',
                                   'EXECUTE'
                               ) AS live_lease_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_mark_watch_poll_succeeded(uuid,text,text,timestamptz,timestamptz)',
                                   'EXECUTE'
                               ) AS success_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_release_watch_poll(uuid,text,text,timestamptz,timestamptz,text)',
                                   'EXECUTE'
                               ) AS release_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_list_watch_poll_states()',
                                   'EXECUTE'
                               ) AS list_access
                        """
                    )
                )
            )
            .mappings()
            .one()
        )

    assert privileges == {
        "table_access": False,
        "claim_access": True,
        "live_lease_access": True,
        "success_access": True,
        "release_access": True,
        "list_access": True,
    }

    for statement in (
        text("SELECT * FROM public.watch_poll_state"),
        text(
            """
            INSERT INTO public.watch_poll_state (canonical_event_id, source)
            VALUES (:canonical_event_id, 'luma')
            """
        ),
        text("UPDATE public.watch_poll_state SET attempt_count = attempt_count + 1"),
        text("DELETE FROM public.watch_poll_state"),
    ):
        with pytest.raises(Exception, match="permission denied"):
            async with system_session_scope() as session:
                await session.execute(statement, {"canonical_event_id": uuid4()})

    async with system_session_scope() as session:
        malformed_claim = (
            await session.execute(
                text(
                    """
                    SELECT *
                    FROM public.fn_claim_watch_poll(
                        NULL::uuid, 'invalid', NULL::timestamptz, 0, ''
                    )
                    """
                )
            )
        ).all()
        malformed_live_lease = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_has_live_watch_poll_lease(NULL::uuid, 'invalid', '')
                    """
                )
            )
        ).scalar_one()
        malformed_success = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_mark_watch_poll_succeeded(
                        NULL::uuid, 'invalid', '', NULL::timestamptz, NULL::timestamptz
                    )
                    """
                )
            )
        ).scalar_one()
        malformed_release = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_release_watch_poll(
                        NULL::uuid, 'invalid', '', NULL::timestamptz, NULL::timestamptz, ''
                    )
                    """
                )
            )
        ).scalar_one()
        oversized_claim = (
            await session.execute(
                text(
                    """
                    SELECT *
                    FROM public.fn_claim_watch_poll(
                        :canonical_event_id, 'luma', clock_timestamp(), 3601, 'oversized'
                    )
                    """
                ),
                {"canonical_event_id": uuid4()},
            )
        ).all()

    assert malformed_claim == []
    assert malformed_live_lease is False
    assert malformed_success is False
    assert malformed_release is False
    assert oversized_claim == []


async def test_unsubscribed_public_registry_key_cannot_create_a_poll_cursor(db: None) -> None:
    """The claim capability cannot manufacture work for a zero-subscriber registry key."""
    now = datetime.now(UTC).replace(microsecond=0)
    tag = uuid4().hex
    candidate = CandidateEvent(
        source=Source.LUMA,
        source_event_id=f"watch-poll-orphan-{tag}",
        title=f"Watch poll orphan fixture {tag}",
        start_at=now + timedelta(days=30),
        registration_url=f"https://luma.com/watch-poll-orphan-{tag}",
        city="san_francisco",
        is_free=True,
    )
    container = build_container(get_settings())
    canonical_event_id = (await container.catalog.upsert_candidates([candidate]))[
        0
    ].canonical_event_id
    await _seed_owner_registry(canonical_event_id, Source.LUMA)
    try:
        watch = WatchedEvent(canonical_event_id, Source.LUMA, 0, now)
        assert (
            await PostgresWatchPollRepository().claim_watch_poll(
                watch,
                now=now,
                lease_seconds=60,
            )
            is None
        )
    finally:
        await _delete_owner_watch(canonical_event_id, Source.LUMA)


async def test_public_watch_poll_cursor_leases_retries_and_advances_only_after_success(
    db: None,
) -> None:
    """The non-superuser app role owns a restart-safe public timing cursor, not source payloads."""
    candidate_now = datetime.now(UTC).replace(microsecond=0)
    tag = uuid4().hex
    container = build_container(get_settings())
    candidate = CandidateEvent(
        source=Source.LUMA,
        source_event_id=f"watch-poll-{tag}",
        title=f"Watch poll fixture {tag}",
        start_at=candidate_now + timedelta(days=30),
        registration_url=f"https://luma.com/watch-poll-{tag}",
        city="san_francisco",
        is_free=True,
    )
    canonical_event_id = (await container.catalog.upsert_candidates([candidate]))[
        0
    ].canonical_event_id
    await _seed_owner_watch(canonical_event_id, Source.LUMA)
    now = datetime.now(UTC).replace(microsecond=0)
    try:
        repository = PostgresWatchPollRepository()
        watch = WatchedEvent(
            canonical_event_id,
            Source.LUMA,
            subscriber_count=1,
            created_at=now,
        )
        first = await repository.claim_watch_poll(watch, now=now, lease_seconds=60)

        assert first is not None
        assert first.attempt_count == 1
        state_while_leased = next(
            item
            for item in await repository.list_watch_poll_states()
            if item.canonical_event_id == canonical_event_id and item.source is Source.LUMA
        )
        assert state_while_leased.lease_token is None
        assert state_while_leased.lease_expires_at is None
        assert await repository.claim_watch_poll(watch, now=now, lease_seconds=60) is None
        reclaimed_at = now + timedelta(seconds=61)
        reclaimed = await repository.claim_watch_poll(watch, now=reclaimed_at, lease_seconds=60)
        assert reclaimed is not None
        assert reclaimed.attempt_count == 2
        assert (
            await repository.mark_watch_poll_succeeded(
                first,
                completed_at=reclaimed_at,
                next_due_at=reclaimed_at + timedelta(hours=1),
            )
            is False
        )
        assert (
            await repository.release_watch_poll(
                first,
                completed_at=reclaimed_at,
                next_due_at=reclaimed_at + timedelta(hours=1),
                error_type="RuntimeError",
            )
            is False
        )
        assert (
            await repository.mark_watch_poll_succeeded(
                reclaimed,
                completed_at=reclaimed_at,
                next_due_at=reclaimed_at + timedelta(hours=1),
            )
            is True
        )
        assert (
            await repository.claim_watch_poll(
                watch,
                now=reclaimed_at + timedelta(minutes=59),
                lease_seconds=60,
            )
            is None
        )

        retry = await repository.claim_watch_poll(
            watch,
            now=reclaimed_at + timedelta(hours=1),
            lease_seconds=60,
        )
        assert retry is not None
        assert retry.attempt_count == 3
        assert (
            await repository.release_watch_poll(
                retry,
                completed_at=reclaimed_at + timedelta(hours=1),
                next_due_at=reclaimed_at + timedelta(hours=2),
                error_type="RuntimeError",
            )
            is True
        )
        state = next(
            item
            for item in await repository.list_watch_poll_states()
            if item.canonical_event_id == canonical_event_id and item.source is Source.LUMA
        )
        assert state.last_success_at == reclaimed_at
        assert state.consecutive_failures == 1
        assert state.last_error_type == "RuntimeError"
        assert state.next_due_at == reclaimed_at + timedelta(hours=2)
    finally:
        await _delete_owner_watch(canonical_event_id, Source.LUMA)


async def test_watch_poll_detector_lease_check_reads_the_database_clock_after_a_row_wait(
    db: None,
) -> None:
    """A stale pre-detector watch lease cannot remain authorized after a blocked row wait (NFR-8)."""
    now = datetime.now(UTC).replace(microsecond=0)
    tag = uuid4().hex
    candidate = CandidateEvent(
        source=Source.LUMA,
        source_event_id=f"watch-poll-pre-detector-{tag}",
        title=f"Watch poll pre-detector fixture {tag}",
        start_at=now + timedelta(days=30),
        registration_url=f"https://luma.com/watch-poll-pre-detector-{tag}",
        city="san_francisco",
        is_free=True,
    )
    container = build_container(get_settings())
    canonical_event_id = (await container.catalog.upsert_candidates([candidate]))[
        0
    ].canonical_event_id
    await _seed_owner_watch(canonical_event_id, Source.LUMA)
    watch = WatchedEvent(canonical_event_id, Source.LUMA, 1, now)
    try:
        repository = PostgresWatchPollRepository()
        stale_lease = await repository.claim_watch_poll(watch, now=now, lease_seconds=2)
        assert stale_lease is not None

        async with _owner_watch_poll_state_lock(canonical_event_id, Source.LUMA) as owner:
            stale_check = asyncio.create_task(repository.has_live_watch_poll_lease(stale_lease))
            await asyncio.sleep(0.1)
            assert not stale_check.done()
            await _wait_for_watch_poll_lease_expiry(owner, canonical_event_id, Source.LUMA)

        assert await asyncio.wait_for(stale_check, timeout=5) is False
        stale_state = await _owner_watch_poll_snapshot(canonical_event_id, Source.LUMA)
        assert stale_state.lease_token == stale_lease.lease_token
        fresh_lease = await repository.claim_watch_poll(
            watch,
            now=datetime.now(UTC),
            lease_seconds=60,
        )
        assert fresh_lease is not None
        assert fresh_lease.lease_token != stale_lease.lease_token
        assert await repository.has_live_watch_poll_lease(stale_lease) is False
        assert await repository.has_live_watch_poll_lease(fresh_lease) is True
        completed_at = datetime.now(UTC)
        assert (
            await repository.release_watch_poll(
                fresh_lease,
                completed_at=completed_at,
                next_due_at=completed_at + timedelta(hours=1),
                error_type="P35FixtureCleanup",
            )
            is True
        )
    finally:
        await _delete_owner_watch(canonical_event_id, Source.LUMA)


async def test_watch_poll_terminal_writes_require_live_leases_before_reclaim(db: None) -> None:
    """An expired poll worker cannot alter cursor timing or failure evidence (ADR-008/NFR-8)."""
    candidate_now = datetime.now(UTC).replace(microsecond=0)
    tag = uuid4().hex
    container = build_container(get_settings())
    candidates = (
        CandidateEvent(
            source=Source.LUMA,
            source_event_id=f"watch-poll-live-success-{tag}",
            title=f"Watch poll live success fixture {tag}",
            start_at=candidate_now + timedelta(days=30),
            registration_url=f"https://luma.com/watch-poll-live-success-{tag}",
            city="san_francisco",
            is_free=True,
        ),
        CandidateEvent(
            source=Source.LUMA,
            source_event_id=f"watch-poll-live-failure-{tag}",
            title=f"Watch poll live failure fixture {tag}",
            start_at=candidate_now + timedelta(days=31),
            registration_url=f"https://luma.com/watch-poll-live-failure-{tag}",
            city="san_francisco",
            is_free=True,
        ),
    )
    success_event, failure_event = await container.catalog.upsert_candidates(list(candidates))
    await _seed_owner_watch(success_event.canonical_event_id, Source.LUMA)
    await _seed_owner_watch(failure_event.canonical_event_id, Source.LUMA)
    now = datetime.now(UTC).replace(microsecond=0)
    success_watch = WatchedEvent(success_event.canonical_event_id, Source.LUMA, 1, now)
    failure_watch = WatchedEvent(failure_event.canonical_event_id, Source.LUMA, 1, now)
    try:
        repository = PostgresWatchPollRepository()
        stale_success = await repository.claim_watch_poll(success_watch, now=now, lease_seconds=60)
        stale_failure = await repository.claim_watch_poll(failure_watch, now=now, lease_seconds=60)
        assert stale_success is not None and stale_failure is not None

        await _expire_owner_watch_poll_lease(
            success_watch.canonical_event_id, success_watch.source, stale_success.lease_token
        )
        await _expire_owner_watch_poll_lease(
            failure_watch.canonical_event_id, failure_watch.source, stale_failure.lease_token
        )
        success_before = await _owner_watch_poll_snapshot(
            success_watch.canonical_event_id, success_watch.source
        )
        failure_before = await _owner_watch_poll_snapshot(
            failure_watch.canonical_event_id, failure_watch.source
        )

        stale_completed_at = now + timedelta(minutes=5)
        assert (
            await repository.mark_watch_poll_succeeded(
                stale_success,
                completed_at=stale_completed_at,
                next_due_at=stale_completed_at + timedelta(hours=1),
            )
            is False
        )
        assert (
            await _owner_watch_poll_snapshot(
                success_watch.canonical_event_id, success_watch.source
            )
            == success_before
        )
        assert (
            await repository.release_watch_poll(
                stale_failure,
                completed_at=stale_completed_at,
                next_due_at=stale_completed_at + timedelta(hours=1),
                error_type="RuntimeError",
            )
            is False
        )
        assert (
            await _owner_watch_poll_snapshot(
                failure_watch.canonical_event_id, failure_watch.source
            )
            == failure_before
        )

        reclaim_at = datetime.now(UTC) + timedelta(seconds=1)
        fresh_success = await repository.claim_watch_poll(
            success_watch, now=reclaim_at, lease_seconds=60
        )
        fresh_failure = await repository.claim_watch_poll(
            failure_watch, now=reclaim_at, lease_seconds=60
        )
        assert fresh_success is not None and fresh_failure is not None
        assert fresh_success.lease_token != stale_success.lease_token
        assert fresh_failure.lease_token != stale_failure.lease_token

        success_completed_at = reclaim_at + timedelta(seconds=1)
        assert (
            await repository.mark_watch_poll_succeeded(
                fresh_success,
                completed_at=success_completed_at,
                next_due_at=success_completed_at + timedelta(hours=1),
            )
            is True
        )
        failure_completed_at = reclaim_at + timedelta(seconds=2)
        assert (
            await repository.release_watch_poll(
                fresh_failure,
                completed_at=failure_completed_at,
                next_due_at=failure_completed_at + timedelta(hours=1),
                error_type="RuntimeError",
            )
            is True
        )
        success_after = await _owner_watch_poll_snapshot(
            success_watch.canonical_event_id, success_watch.source
        )
        failure_after = await _owner_watch_poll_snapshot(
            failure_watch.canonical_event_id, failure_watch.source
        )
        assert success_after.last_success_at == success_completed_at
        assert success_after.lease_token is None
        assert success_after.lease_expires_at is None
        assert failure_after.last_failure_at == failure_completed_at
        assert failure_after.consecutive_failures == 1
        assert failure_after.last_error_type == "RuntimeError"
        assert failure_after.lease_token is None
        assert failure_after.lease_expires_at is None
    finally:
        await _delete_owner_watch(success_event.canonical_event_id, Source.LUMA)
        await _delete_owner_watch(failure_event.canonical_event_id, Source.LUMA)


async def _seed_owner_watch(canonical_event_id: UUID, source: Source) -> None:
    """Seed an active global public watch through owner-only control-plane setup."""
    tenant_id = uuid4()
    tag = f"watch-poll-owner-{tenant_id.hex}"
    await PostgresTenantRepository().add(
        Tenant(tenant_id, tag, f"{tag}@example.test", f"{tag}@u.example.test")
    )
    await _seed_owner_registry(canonical_event_id, source)
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.watch_subscriptions
                        (canonical_event_id, source, tenant_id, workflow_id)
                    VALUES (:canonical_event_id, :source, :tenant_id, :workflow_id)
                    ON CONFLICT (canonical_event_id, source, tenant_id, workflow_id) DO NOTHING
                    """
                ),
                {
                    "canonical_event_id": canonical_event_id,
                    "source": source.value,
                    "tenant_id": tenant_id,
                    "workflow_id": f"{tenant_id}:{canonical_event_id}",
                },
            )
    finally:
        await owner.dispose()


async def _seed_owner_registry(canonical_event_id: UUID, source: Source) -> None:
    """Seed only a public registry key for the active/unsubscribed capability boundary."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.watch_registry (canonical_event_id, source)
                    VALUES (:canonical_event_id, :source)
                    ON CONFLICT (canonical_event_id, source) DO NOTHING
                    """
                ),
                {
                    "canonical_event_id": canonical_event_id,
                    "source": source.value,
                },
            )
    finally:
        await owner.dispose()


async def _delete_owner_watch(canonical_event_id: UUID, source: Source) -> None:
    """Remove this fixture's public row so unrelated integration cases see a clean registry."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    DELETE FROM public.watch_registry
                    WHERE canonical_event_id = :canonical_event_id
                      AND source = :source
                    """
                ),
                {
                    "canonical_event_id": canonical_event_id,
                    "source": source.value,
                },
            )
    finally:
        await owner.dispose()


@asynccontextmanager
async def _owner_watch_poll_state_lock(
    canonical_event_id: UUID,
    source: Source,
) -> AsyncIterator[AsyncConnection]:
    """Hold one public cursor row until an app lease check reads its post-wait database clock."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.connect() as connection, connection.begin():
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT canonical_event_id
                        FROM public.watch_poll_state
                        WHERE canonical_event_id = :canonical_event_id
                          AND source = :source
                        FOR NO KEY UPDATE
                        """
                    ),
                    {
                        "canonical_event_id": canonical_event_id,
                        "source": source.value,
                    },
                )
            ).scalar_one_or_none()
            if row is None:
                raise RuntimeError("watch-poll fixture owner lock did not find its cursor")
            yield connection
    finally:
        await owner.dispose()


async def _wait_for_watch_poll_lease_expiry(
    connection: AsyncConnection,
    canonical_event_id: UUID,
    source: Source,
) -> None:
    """Wait on PostgreSQL's clock while an app lease check is blocked on this exact cursor."""
    for _ in range(240):
        expired = (
            await connection.execute(
                text(
                    """
                    SELECT lease_expires_at <= pg_catalog.clock_timestamp()
                    FROM public.watch_poll_state
                    WHERE canonical_event_id = :canonical_event_id
                      AND source = :source
                    """
                ),
                {
                    "canonical_event_id": canonical_event_id,
                    "source": source.value,
                },
            )
        ).scalar_one()
        if bool(expired):
            return
        await asyncio.sleep(0.025)
    raise RuntimeError("watch-poll fixture lease did not expire while the app check was blocked")


async def _expire_owner_watch_poll_lease(
    canonical_event_id: UUID, source: Source, lease_token: str
) -> None:
    """Expire exactly one held lease without rotating it, modeling a paused worker before reclaim."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.begin() as connection:
            expired = (
                await connection.execute(
                    text(
                        """
                        UPDATE public.watch_poll_state
                        SET lease_expires_at = pg_catalog.clock_timestamp() - INTERVAL '1 second'
                        WHERE canonical_event_id = :canonical_event_id
                          AND source = :source
                          AND lease_token = :lease_token
                        RETURNING canonical_event_id
                        """
                    ),
                    {
                        "canonical_event_id": canonical_event_id,
                        "source": source.value,
                        "lease_token": lease_token,
                    },
                )
            ).scalar_one_or_none()
        assert expired == canonical_event_id
    finally:
        await owner.dispose()


async def _owner_watch_poll_snapshot(
    canonical_event_id: UUID, source: Source
) -> _OwnerWatchPollSnapshot:
    """Read the complete owner-only mutable cursor projection for a stale-write assertion."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.connect() as connection:
            row = cast(
                _OwnerWatchPollStateRow,
                (
                    await connection.execute(
                        text(
                            """
                            SELECT first_seen_at,
                                   last_attempt_at,
                                   last_success_at,
                                   last_failure_at,
                                   consecutive_failures,
                                   next_due_at,
                                   attempt_count,
                                   lease_token,
                                   lease_expires_at,
                                   last_error_type
                            FROM public.watch_poll_state
                            WHERE canonical_event_id = :canonical_event_id
                              AND source = :source
                            """
                        ),
                        {
                            "canonical_event_id": canonical_event_id,
                            "source": source.value,
                        },
                    )
                ).one(),
            )
        return _OwnerWatchPollSnapshot(
            first_seen_at=row.first_seen_at,
            last_attempt_at=row.last_attempt_at,
            last_success_at=row.last_success_at,
            last_failure_at=row.last_failure_at,
            consecutive_failures=int(row.consecutive_failures),
            next_due_at=row.next_due_at,
            attempt_count=int(row.attempt_count),
            lease_token=row.lease_token,
            lease_expires_at=row.lease_expires_at,
            last_error_type=row.last_error_type,
        )
    finally:
        await owner.dispose()
