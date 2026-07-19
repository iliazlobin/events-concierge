"""Postgres/Temporal coverage for the ADR-003 EventRequest start-outbox."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from temporalio.testing import WorkflowEnvironment

from events_concierge.adapters.postgres.tenant_repos import (
    PostgresRequestRepository,
    PostgresTenantRepository,
)
from events_concierge.application.request_start import RequestStartRelay
from events_concierge.config import get_settings
from events_concierge.domain import ids
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.request import EventRequest, RequestConstraints
from events_concierge.infra.db import system_session_scope, tenant_session_scope
from events_concierge.ports.repositories import RequestStartRecord
from events_concierge.workflows.start import TemporalRequestWorkflowStarter

pytestmark = pytest.mark.integration


class LostAcknowledgementStarter:
    """Models a Temporal start that succeeded remotely before the client lost its acknowledgement."""

    def __init__(self) -> None:
        self.calls = 0
        self.effects: set[tuple[UUID, UUID]] = set()

    async def start(self, tenant_id: UUID, request_id: UUID) -> None:
        self.calls += 1
        identity = (tenant_id, request_id)
        if identity in self.effects:
            return
        self.effects.add(identity)
        raise RuntimeError("simulated Temporal acknowledgement loss")


class _ExpireLeaseAfterVisibleTemporalStart:
    """Expire the first relay lease only after the real Temporal parent has been created."""

    def __init__(self, delegate: TemporalRequestWorkflowStarter) -> None:
        self._delegate = delegate
        self.records: list[RequestStartRecord] = []

    async def start(self, tenant_id: UUID, request_id: UUID) -> None:
        """Create/replay the deterministic parent, then lose only the first DB ACK authority."""
        probe = RequestStartRecord(
            request_id=request_id,
            tenant_id=tenant_id,
            attempt_count=0,
            lease_token="probe",
        )
        state = await _owner_start_outbox_state(probe)
        if state.lease_token is None:
            raise RuntimeError("request-start fixture expected an active relay lease")
        record = RequestStartRecord(
            request_id=request_id,
            tenant_id=tenant_id,
            attempt_count=state.attempt_count,
            lease_token=state.lease_token,
        )
        self.records.append(record)
        await self._delegate.start(tenant_id, request_id)
        if len(self.records) == 1:
            await _owner_expire_start_lease(record)


def _request(tenant_id: UUID, request_id: UUID, raw_text: str) -> EventRequest:
    return EventRequest(
        request_id=request_id,
        tenant_id=tenant_id,
        raw_text=raw_text,
        constraints=RequestConstraints(categories=("music",)),
    )


async def test_start_outbox_dedup_is_atomic_and_keeps_request_text_under_rls(db: None) -> None:
    """A duplicate key cannot leave a request without its one durable start instruction (AC-48)."""
    repository = PostgresRequestRepository()
    tenant_id = uuid4()
    dedup_key = ids.request_dedup_key(tenant_id, "jazz after work", "2026-07-17T19")
    request_id = ids.intake_request_id(tenant_id, "jazz after work", "2026-07-17T19")
    request = _request(tenant_id, request_id, "jazz after work")

    await repository.add_and_enqueue_start(request, dedup_key)
    await repository.add_and_enqueue_start(request, dedup_key)

    # A malformed collision is rejected as one transaction: the attempted second EventRequest rolls
    # back instead of becoming an orphaned request that no worker will ever start.
    conflicting_id = uuid4()
    with pytest.raises(RuntimeError, match="dedup key"):
        await repository.add_and_enqueue_start(
            _request(tenant_id, conflicting_id, "different wording"), dedup_key
        )

    async with tenant_session_scope(tenant_id) as session:
        request_rows = (
            await session.execute(
                text(
                    "SELECT request_id, raw_text, state FROM event_requests WHERE tenant_id = :tenant_id"
                ),
                {"tenant_id": tenant_id},
            )
        ).all()
    assert [(row.request_id, row.raw_text, row.state) for row in request_rows] == [
        (request_id, "jazz after work", "received")
    ]

    async with system_session_scope() as session:
        start_rows = (
            await session.execute(
                text(
                    """SELECT request_id, tenant_id, dedup_key, started_at
                       FROM request_start_outbox WHERE request_id = :request_id"""
                ),
                {"request_id": request_id},
            )
        ).all()
        columns = (
            await session.execute(
                text(
                    """SELECT column_name FROM information_schema.columns
                       WHERE table_name = 'request_start_outbox'"""
                )
            )
        ).all()
    assert [
        (row.request_id, row.tenant_id, row.dedup_key, row.started_at) for row in start_rows
    ] == [(request_id, tenant_id, dedup_key, None)]
    assert "raw_text" not in {str(row.column_name) for row in columns}


async def test_start_outbox_recovers_lost_ack_without_second_parent_effect(db: None) -> None:
    """A DB retry after a remote start converges through the stable parent workflow identity (AC-48)."""
    repository = PostgresRequestRepository()
    tenant_id, request_id = uuid4(), uuid4()
    await repository.add_and_enqueue_start(
        _request(tenant_id, request_id, "find a music event"),
        ids.request_dedup_key(tenant_id, "find a music event", "2026-07-17T19"),
    )
    starter = LostAcknowledgementStarter()
    relay = RequestStartRelay(repository, starter, now=lambda: datetime.now(UTC))

    assert await relay.relay_request(tenant_id, request_id) is False
    async with system_session_scope() as session:
        failed = (
            await session.execute(
                text(
                    """SELECT attempt_count, started_at, lease_token, last_error
                       FROM request_start_outbox WHERE request_id = :request_id"""
                ),
                {"request_id": request_id},
            )
        ).one()
        await session.execute(
            text(
                """UPDATE request_start_outbox SET next_attempt_at = now()
                   WHERE request_id = :request_id"""
            ),
            {"request_id": request_id},
        )
    assert int(failed.attempt_count) == 1
    assert failed.started_at is None
    assert failed.lease_token is None
    assert failed.last_error == "simulated Temporal acknowledgement loss"

    assert await relay.relay_request(tenant_id, request_id) is True
    assert starter.calls == 2
    assert starter.effects == {(tenant_id, request_id)}

    async with tenant_session_scope(tenant_id) as session:
        request_state = (
            (
                await session.execute(
                    text("SELECT state FROM event_requests WHERE request_id = :request_id"),
                    {"request_id": request_id},
                )
            )
            .one()
            .state
        )
    async with system_session_scope() as session:
        started = (
            await session.execute(
                text(
                    """SELECT started_at, attempt_count, lease_token FROM request_start_outbox
                       WHERE request_id = :request_id"""
                ),
                {"request_id": request_id},
            )
        ).one()
    assert request_state == "started"
    assert started.started_at is not None
    assert int(started.attempt_count) == 1
    assert started.lease_token is None


async def test_start_outbox_reclaims_stale_post_start_ack_lease_without_second_parent_effect(
    db: None,
) -> None:
    """A real Temporal duplicate converges after a stale exact DB ACK lease (AC-48, ADR-003)."""
    repository = PostgresRequestRepository()
    tenant_id, request_id = uuid4(), uuid4()
    await repository.add_and_enqueue_start(
        _request(tenant_id, request_id, "find one stale-ack event"),
        ids.request_dedup_key(tenant_id, "find one stale-ack event", "2026-07-18T19"),
    )
    settings = get_settings().model_copy(
        update={"temporal_task_queue": f"request-start-stale-ack-{uuid4().hex}"}
    )

    async with await WorkflowEnvironment.start_time_skipping() as environment:
        starter = _ExpireLeaseAfterVisibleTemporalStart(
            TemporalRequestWorkflowStarter(environment.client, settings)
        )
        relay = RequestStartRelay(repository, starter)

        assert await relay.relay_request(tenant_id, request_id) is False
        assert len(starter.records) == 1
        first_state = await _owner_start_outbox_state(starter.records[0])
        assert first_state.started_at is None
        assert first_state.lease_expired is True
        assert first_state.attempt_count == 0
        async with tenant_session_scope(tenant_id) as session:
            assert (
                await session.execute(
                    text("SELECT state FROM event_requests WHERE request_id = :request_id"),
                    {"request_id": request_id},
                )
            ).scalar_one() == "received"

        assert await relay.relay_request(tenant_id, request_id) is True

    assert len(starter.records) == 2
    assert starter.records[1].lease_token != starter.records[0].lease_token
    assert all(record.request_id == request_id for record in starter.records)
    assert all(record.tenant_id == tenant_id for record in starter.records)
    final_state = await _owner_start_outbox_state(starter.records[1])
    assert final_state.started_at is not None
    assert final_state.lease_token is None
    assert final_state.attempt_count == 0
    async with tenant_session_scope(tenant_id) as session:
        assert (
            await session.execute(
                text("SELECT state FROM event_requests WHERE request_id = :request_id"),
                {"request_id": request_id},
            )
        ).scalar_one() == "started"


async def test_start_outbox_pre_temporal_lease_check_reads_database_clock_after_row_wait(
    db: None,
) -> None:
    """A stale start lease cannot remain authorized after a blocked final Temporal-entry fence."""
    repository = PostgresRequestRepository()
    tenant_id, request_id = uuid4(), uuid4()
    await repository.add_and_enqueue_start(
        _request(tenant_id, request_id, "find one pre-start lease event"),
        ids.request_dedup_key(tenant_id, "find one pre-start lease event", "2026-07-18T20"),
    )
    stale = await repository.claim_start(tenant_id, request_id, lease_seconds=2)
    assert stale is not None
    before = await _owner_start_outbox_state(stale)

    async with _owner_start_outbox_lock(stale) as owner:
        stale_check = asyncio.create_task(repository.has_live_start_lease(stale))
        await asyncio.sleep(0.1)
        assert not stale_check.done()
        await _wait_for_start_lease_expiry(owner, stale)

    assert await asyncio.wait_for(stale_check, timeout=5) is False
    after = await _owner_start_outbox_state(stale)
    assert after.started_at is None
    assert after.lease_token == stale.lease_token
    assert after.attempt_count == before.attempt_count
    assert after.next_attempt_at == before.next_attempt_at
    assert after.last_error == before.last_error

    fresh = await repository.claim_start(tenant_id, request_id, lease_seconds=60)
    assert fresh is not None
    assert fresh.lease_token != stale.lease_token
    # Start attempts are counted only when a failed Temporal call is rescheduled, not at reclaim.
    assert fresh.attempt_count == stale.attempt_count
    assert await repository.has_live_start_lease(stale) is False
    assert await repository.has_live_start_lease(fresh) is True
    assert (
        await repository.has_live_start_lease(
            RequestStartRecord(
                request_id=fresh.request_id,
                tenant_id=fresh.tenant_id,
                attempt_count=fresh.attempt_count,
                lease_token="wrong-start-lease-token",
            )
        )
        is False
    )
    assert (
        await repository.has_live_start_lease(
            RequestStartRecord(
                request_id=fresh.request_id,
                tenant_id=uuid4(),
                attempt_count=fresh.attempt_count,
                lease_token=fresh.lease_token,
            )
        )
        is False
    )


async def test_start_outbox_terminal_writes_require_live_leases_before_reclaim(db: None) -> None:
    """Expired request-start leases cannot acknowledge or retry before a fresh owner recovers them."""
    repository = PostgresRequestRepository()
    tenant_id, mark_request_id, reschedule_request_id = uuid4(), uuid4(), uuid4()
    await repository.add_and_enqueue_start(
        _request(tenant_id, mark_request_id, "start lease mark fixture"),
        f"request-start-lease-mark:{tenant_id}:{mark_request_id}",
    )
    await repository.add_and_enqueue_start(
        _request(tenant_id, reschedule_request_id, "start lease retry fixture"),
        f"request-start-lease-retry:{tenant_id}:{reschedule_request_id}",
    )
    mark_record = await repository.claim_start(tenant_id, mark_request_id, lease_seconds=300)
    reschedule_record = await repository.claim_start(
        tenant_id,
        reschedule_request_id,
        lease_seconds=300,
    )
    assert mark_record is not None
    assert reschedule_record is not None

    await _owner_expire_start_lease(mark_record)
    expired_mark_state = await _owner_start_outbox_state(mark_record)
    assert expired_mark_state.lease_expired is True
    assert await repository.mark_start_started(mark_record) is False
    assert await _owner_start_outbox_state(mark_record) == expired_mark_state
    async with tenant_session_scope(tenant_id) as session:
        stale_mark_request_state = (
            await session.execute(
                text("SELECT state FROM event_requests WHERE request_id = :request_id"),
                {"request_id": mark_request_id},
            )
        ).scalar_one()
    assert stale_mark_request_state == "received"

    await _owner_expire_start_lease(reschedule_record)
    expired_reschedule_state = await _owner_start_outbox_state(reschedule_record)
    assert expired_reschedule_state.lease_expired is True
    assert (
        await repository.reschedule_start(
            reschedule_record,
            retry_at=datetime(2099, 1, 1, tzinfo=UTC),
            error="expired request-start lease before reclaim",
        )
        is False
    )
    assert await _owner_start_outbox_state(reschedule_record) == expired_reschedule_state

    fresh_mark_record = await repository.claim_start(tenant_id, mark_request_id, lease_seconds=300)
    fresh_reschedule_record = await repository.claim_start(
        tenant_id,
        reschedule_request_id,
        lease_seconds=300,
    )
    assert fresh_mark_record is not None
    assert fresh_reschedule_record is not None
    assert fresh_mark_record.lease_token != mark_record.lease_token
    assert fresh_reschedule_record.lease_token != reschedule_record.lease_token
    assert await repository.mark_start_started(fresh_mark_record) is True
    assert (
        await repository.reschedule_start(
            fresh_reschedule_record,
            retry_at=datetime(2099, 1, 2, tzinfo=UTC),
            error="fresh request-start retry after reclaim",
        )
        is True
    )


async def test_request_no_result_terminalization_is_atomic_and_replay_safe(db: None) -> None:
    """An empty parent result leaves one request state/outbox effect across an ACK-loss replay."""
    repository = PostgresRequestRepository()
    tenant_id, request_id = uuid4(), uuid4()
    transition_id = f"req:{tenant_id}:{request_id}:failed-no-candidate:1"
    await PostgresTenantRepository().add(
        Tenant(
            tenant_id,
            f"oidc|request-no-result-{tenant_id}",
            f"request-no-result-{tenant_id}@example.test",
            f"request-no-result-{tenant_id}@u.example.test",
        )
    )
    await repository.add(_request(tenant_id, request_id, "nothing matches this fixture"))

    first = await repository.mark_failed_no_candidate(tenant_id, request_id, transition_id)
    replayed = await repository.mark_failed_no_candidate(tenant_id, request_id, transition_id)

    async with tenant_session_scope(tenant_id) as session:
        state = (
            await session.execute(
                text("SELECT state FROM event_requests WHERE request_id = :request_id"),
                {"request_id": request_id},
            )
        ).scalar_one()
        outbox_rows = (
            await session.execute(
                text(
                    """SELECT topic, payload ->> 'transition_id' AS transition_id
                       FROM outbox
                       WHERE tenant_id = :tenant_id
                         AND topic = 'request.failed_no_candidate'"""
                ),
                {"tenant_id": tenant_id},
            )
        ).all()

    assert first is True
    assert replayed is False
    assert state == "failed_no_candidate"
    assert [(row.topic, row.transition_id) for row in outbox_rows] == [
        ("request.failed_no_candidate", transition_id)
    ]


async def test_temporal_start_adapter_treats_parent_reject_duplicate_as_success() -> None:
    """The real Temporal client closes the lost-ack retry loop with REJECT_DUPLICATE (ADR-003)."""
    settings = get_settings().model_copy(
        update={"temporal_task_queue": f"request-start-{uuid4().hex}"}
    )
    tenant_id, request_id = uuid4(), uuid4()
    async with await WorkflowEnvironment.start_time_skipping() as environment:
        starter = TemporalRequestWorkflowStarter(environment.client, settings)
        await starter.start(tenant_id, request_id)
        await starter.start(tenant_id, request_id)


@dataclass(frozen=True, slots=True)
class _RequestStartOutboxState:
    """Owner-visible durable facts used to prove an expired start mutation is a true no-op."""

    request_id: UUID
    tenant_id: UUID
    dedup_key: str
    created_at: datetime
    started_at: datetime | None
    attempt_count: int
    next_attempt_at: datetime
    lease_token: str | None
    lease_expires_at: datetime | None
    lease_expired: bool | None
    last_error: str | None


@asynccontextmanager
async def _owner_start_outbox_lock(record: RequestStartRecord) -> AsyncIterator[AsyncConnection]:
    """Hold one start row while the app-role final lease check waits on PostgreSQL's clock."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.connect() as connection, connection.begin():
            found = (
                await connection.execute(
                    text(
                        """
                        SELECT request_id
                        FROM public.request_start_outbox
                        WHERE request_id = :request_id
                          AND tenant_id = :tenant_id
                        FOR UPDATE
                        """
                    ),
                    {"request_id": record.request_id, "tenant_id": record.tenant_id},
                )
            ).scalar_one_or_none()
            if found is None:
                raise RuntimeError("request-start fixture owner lock did not find its row")
            yield connection
    finally:
        await owner.dispose()


async def _wait_for_start_lease_expiry(
    connection: AsyncConnection, record: RequestStartRecord
) -> None:
    """Wait on PostgreSQL's clock while the app-role pre-Temporal check is lock-blocked."""
    for _ in range(240):
        expired = (
            await connection.execute(
                text(
                    """
                    SELECT lease_expires_at <= pg_catalog.clock_timestamp()
                    FROM public.request_start_outbox
                    WHERE request_id = :request_id
                      AND tenant_id = :tenant_id
                    """
                ),
                {"request_id": record.request_id, "tenant_id": record.tenant_id},
            )
        ).scalar_one()
        if bool(expired):
            return
        await asyncio.sleep(0.025)
    raise RuntimeError("request-start fixture lease did not expire while the app check was blocked")


async def _owner_expire_start_lease(record: RequestStartRecord) -> None:
    """Force one held request-start lease past its current-authority boundary (ADR-003)."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.request_start_outbox
                    SET lease_expires_at = pg_catalog.clock_timestamp() - INTERVAL '1 second'
                    WHERE request_id = :request_id
                      AND tenant_id = :tenant_id
                      AND lease_token = :lease_token
                      AND started_at IS NULL
                    """
                ),
                {
                    "request_id": record.request_id,
                    "tenant_id": record.tenant_id,
                    "lease_token": record.lease_token,
                },
            )
    finally:
        await owner_engine.dispose()


async def _owner_start_outbox_state(record: RequestStartRecord) -> _RequestStartOutboxState:
    """Read complete request-start queue state through the migration owner for equality assertions."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.connect() as connection:
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT request_id, tenant_id, dedup_key, created_at, started_at, attempt_count,
                               next_attempt_at, lease_token, lease_expires_at,
                               lease_expires_at <= pg_catalog.clock_timestamp() AS lease_expired,
                               last_error
                        FROM public.request_start_outbox
                        WHERE request_id = :request_id
                          AND tenant_id = :tenant_id
                        """
                    ),
                    {"request_id": record.request_id, "tenant_id": record.tenant_id},
                )
            ).one()
    finally:
        await owner_engine.dispose()
    return _RequestStartOutboxState(
        request_id=row.request_id,
        tenant_id=row.tenant_id,
        dedup_key=row.dedup_key,
        created_at=row.created_at,
        started_at=row.started_at,
        attempt_count=int(row.attempt_count),
        next_attempt_at=row.next_attempt_at,
        lease_token=row.lease_token,
        lease_expires_at=row.lease_expires_at,
        lease_expired=row.lease_expired,
        last_error=row.last_error,
    )
