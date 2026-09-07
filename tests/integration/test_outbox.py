"""Postgres-backed ADR-009 worker coverage for atomic outbox delivery and deduplication."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from events_concierge.adapters.mock.notification_secrets import (
    DevelopmentNotificationSecretProtector,
)
from events_concierge.adapters.postgres.tenant_repos import PostgresOutboxRepository
from events_concierge.application.outbox import NotificationDeliveryWorker
from events_concierge.composition import Container, build_container
from events_concierge.config import get_settings
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import Lane, Source
from events_concierge.domain.events import CandidateEvent
from events_concierge.domain.ids import registration_workflow_id
from events_concierge.infra.db import system_session_scope
from events_concierge.ports.notifications import Notification
from events_concierge.ports.repositories import NotificationClaim, OutboxRecord

pytestmark = pytest.mark.integration


class TenantCountingNotifier:
    """Count target-tenant port invocations without inheriting MockNotifier's in-memory dedup seam."""

    def __init__(self, tenant_id: UUID) -> None:
        self._tenant_id = tenant_id
        self.calls = 0
        self.notifications: list[Notification] = []

    async def send(self, notification: Notification) -> None:
        if notification.tenant_id == self._tenant_id:
            self.calls += 1
            self.notifications.append(notification)


@dataclass(frozen=True, slots=True)
class _OutboxFailureState:
    """Owner-visible complete state used to prove a stale failure write is a no-op."""

    outbox_id: int
    tenant_id: UUID
    topic: str
    payload: str
    created_at: datetime
    delivered_at: datetime | None
    failed_at: datetime | None
    attempt_count: int
    next_attempt_at: datetime
    lease_token: str | None
    lease_expires_at: datetime | None
    lease_expired: bool | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class _NotificationLedgerState:
    """Owner-visible ledger state used to prove a stale release leaves durable dedup intact."""

    dedup_key: str
    outbox_id: int
    tenant_id: UUID
    state: str
    lease_token: str | None
    lease_expires_at: datetime | None
    lease_expired: bool | None
    delivered_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _isolated_outbox_ids(count: int) -> list[int]:
    """Reserve highly negative IDs so a bounded real worker claim touches only this fixture."""
    start = -9_223_000_000_000_000_000 + (uuid4().int % 1_000_000_000)
    return [start + index for index in range(count)]


async def _owner_execute(statement: str, parameters: dict[str, object]) -> int:
    """Apply an owner-only fixture mutation without granting broad fixture DML to ``ec_app``."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run `make test-integration`")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            result = await connection.execute(text(statement), parameters)
    finally:
        await owner_engine.dispose()
    return int(getattr(result, "rowcount", 0))


async def _owner_seed_outbox(outbox_id: int, tenant_id: UUID) -> None:
    """Create an opaque ready row whose negative ID isolates a bounded claim in ADR-009 tests."""
    inserted = await _owner_execute(
        """
        INSERT INTO public.outbox
            (id, tenant_id, topic, payload, created_at, next_attempt_at, attempt_count)
        VALUES (:outbox_id, :tenant_id, 'p24.notification_failure', CAST(:payload AS jsonb),
                pg_catalog.clock_timestamp() - INTERVAL '1 minute',
                pg_catalog.clock_timestamp() - INTERVAL '1 second', 0)
        """,
        {
            "outbox_id": outbox_id,
            "tenant_id": tenant_id,
            "payload": json.dumps({"fixture": f"p24:{outbox_id}"}),
        },
    )
    assert inserted == 1


async def test_outbox_rejects_plaintext_completion_projection(db: None) -> None:
    """0106 rejects the legacy JSON key even when its value is not a syntactically valid URL."""
    outbox_id = _isolated_outbox_ids(1)[0]
    with pytest.raises(DBAPIError) as denied:
        await _owner_execute(
            """
            INSERT INTO public.outbox (id, tenant_id, topic, payload)
            VALUES (:outbox_id, :tenant_id, 'lifecycle.handoff', CAST(:payload AS jsonb))
            """,
            {
                "outbox_id": outbox_id,
                "tenant_id": uuid4(),
                "payload": json.dumps({"completion_url": "redacted-fixture"}),
            },
        )
    assert getattr(denied.value.orig, "sqlstate", None) == "23514"


async def test_terminal_outbox_failure_scrubs_protected_completion_projection(db: None) -> None:
    """A permanently failed delivery retains audit state but not its encrypted bearer."""
    outbox_id = _isolated_outbox_ids(1)[0]
    tenant_id = uuid4()
    lease_token = uuid4().hex
    protector = DevelopmentNotificationSecretProtector()
    protected = await protector.protect_completion_url(
        tenant_id,
        "http://localhost:8000/v1/tasks/terminal-fixture/done",
    )
    inserted = await _owner_execute(
        """
        INSERT INTO public.outbox
            (id, tenant_id, topic, payload, lease_token, lease_expires_at)
        VALUES (
            :outbox_id,
            :tenant_id,
            'lifecycle.handoff',
            CAST(:payload AS jsonb),
            :lease_token,
            pg_catalog.clock_timestamp() + INTERVAL '5 minutes'
        )
        """,
        {
            "outbox_id": outbox_id,
            "tenant_id": tenant_id,
            "payload": json.dumps(
                {
                    "workflow_id": "terminal-fixture",
                    "protected_completion_url": protected,
                }
            ),
            "lease_token": lease_token,
        },
    )
    assert inserted == 1
    record = OutboxRecord(
        outbox_id=outbox_id,
        tenant_id=tenant_id,
        topic="lifecycle.handoff",
        payload={"protected_completion_url": protected},
        attempt_count=4,
        lease_token=lease_token,
    )

    terminalized = await PostgresOutboxRepository().reschedule(
        record,
        retry_at=None,
        error="notification materialization or delivery failed",
        consume_attempt=True,
    )
    terminal = await _owner_outbox_failure_state(outbox_id)

    assert terminalized is True
    assert terminal.failed_at is not None
    terminal_payload = json.loads(terminal.payload)
    assert "protected_completion_url" not in terminal_payload
    if protected in json.dumps(terminal_payload, sort_keys=True):
        raise AssertionError("terminal outbox row retained its protected completion capability")
    await _owner_execute(
        "DELETE FROM public.outbox WHERE id = :outbox_id",
        {"outbox_id": outbox_id},
    )


async def _claim_fixture_records(
    repository: PostgresOutboxRepository, outbox_ids: set[int]
) -> dict[int, OutboxRecord]:
    """Claim exactly the negative-ID fixture rows, never a shared development-queue record."""
    records = await repository.claim_batch(limit=len(outbox_ids), lease_seconds=300)
    by_id = {record.outbox_id: record for record in records}
    assert set(by_id) == outbox_ids
    return by_id


async def _owner_expire_outbox_lease(record: OutboxRecord) -> None:
    """Force one claimed outbox row past its failure-path authority boundary (ADR-009)."""
    expired = await _owner_execute(
        """
        UPDATE public.outbox
        SET lease_expires_at = pg_catalog.clock_timestamp() - INTERVAL '1 second'
        WHERE id = :outbox_id
          AND tenant_id = :tenant_id
          AND lease_token = :lease_token
          AND delivered_at IS NULL
          AND failed_at IS NULL
        """,
        {
            "outbox_id": record.outbox_id,
            "tenant_id": record.tenant_id,
            "lease_token": record.lease_token,
        },
    )
    assert expired == 1


async def _owner_expire_notification_lease(record: OutboxRecord, dedup_key: str) -> None:
    """Force one matched notification slot past its failure-path authority boundary (ADR-009)."""
    expired = await _owner_execute(
        """
        UPDATE public.notification_ledger
        SET lease_expires_at = pg_catalog.clock_timestamp() - INTERVAL '1 second'
        WHERE dedup_key = :dedup_key
          AND outbox_id = :outbox_id
          AND tenant_id = :tenant_id
          AND lease_token = :lease_token
          AND state = 'sending'
        """,
        {
            "dedup_key": dedup_key,
            "outbox_id": record.outbox_id,
            "tenant_id": record.tenant_id,
            "lease_token": record.lease_token,
        },
    )
    assert expired == 1


async def _owner_outbox_failure_state(outbox_id: int) -> _OutboxFailureState:
    """Read every mutable outbox fact through the migration owner for no-op equality checks."""
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
                        SELECT id, tenant_id, topic, payload::text AS payload, created_at, delivered_at,
                               failed_at, attempt_count, next_attempt_at, lease_token,
                               lease_expires_at,
                               lease_expires_at <= pg_catalog.clock_timestamp() AS lease_expired,
                               last_error
                        FROM public.outbox
                        WHERE id = :outbox_id
                        """
                    ),
                    {"outbox_id": outbox_id},
                )
            ).one()
    finally:
        await owner_engine.dispose()
    return _OutboxFailureState(
        outbox_id=int(row.id),
        tenant_id=row.tenant_id,
        topic=str(row.topic),
        payload=str(row.payload),
        created_at=row.created_at,
        delivered_at=row.delivered_at,
        failed_at=row.failed_at,
        attempt_count=int(row.attempt_count),
        next_attempt_at=row.next_attempt_at,
        lease_token=row.lease_token,
        lease_expires_at=row.lease_expires_at,
        lease_expired=row.lease_expired,
        last_error=row.last_error,
    )


async def _owner_notification_ledger_state(dedup_key: str) -> _NotificationLedgerState:
    """Read every durable notification fact through the owner for no-op equality checks."""
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
                        SELECT dedup_key, outbox_id, tenant_id, state, lease_token, lease_expires_at,
                               lease_expires_at <= pg_catalog.clock_timestamp() AS lease_expired,
                               delivered_at, created_at, updated_at
                        FROM public.notification_ledger
                        WHERE dedup_key = :dedup_key
                        """
                    ),
                    {"dedup_key": dedup_key},
                )
            ).one()
    finally:
        await owner_engine.dispose()
    return _NotificationLedgerState(
        dedup_key=str(row.dedup_key),
        outbox_id=int(row.outbox_id),
        tenant_id=row.tenant_id,
        state=str(row.state),
        lease_token=row.lease_token,
        lease_expires_at=row.lease_expires_at,
        lease_expired=row.lease_expired,
        delivered_at=row.delivered_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


async def _assert_expired_notification_failure_is_a_noop(
    repository: PostgresOutboxRepository,
    record: OutboxRecord,
    dedup_key: str,
    *,
    retry_at: datetime | None,
    error: str,
) -> None:
    """Prove a late provider-failure cleanup cannot consume or terminalize an expired lease."""
    before_outbox = await _owner_outbox_failure_state(record.outbox_id)
    before_ledger = await _owner_notification_ledger_state(dedup_key)
    assert before_outbox.lease_expired is True
    assert before_ledger.lease_expired is True
    assert before_outbox.attempt_count == 0
    assert before_outbox.failed_at is None
    assert await repository.release_notification(record, dedup_key=dedup_key) is False
    assert (
        await repository.reschedule(
            record,
            retry_at=retry_at,
            error=error,
            consume_attempt=True,
        )
        is False
    )
    assert await _owner_outbox_failure_state(record.outbox_id) == before_outbox
    assert await _owner_notification_ledger_state(dedup_key) == before_ledger


async def _recover_notification_failure(
    repository: PostgresOutboxRepository,
    record: OutboxRecord,
    dedup_key: str,
    *,
    retry_at: datetime | None,
    error: str,
) -> _OutboxFailureState:
    """Exercise the normal fresh-owner failure release and retry/terminal path (ADR-009)."""
    assert (
        await repository.claim_notification(record, dedup_key=dedup_key, lease_seconds=300)
        is NotificationClaim.ACQUIRED
    )
    assert await repository.release_notification(record, dedup_key=dedup_key) is True
    assert (
        await repository.reschedule(
            record,
            retry_at=retry_at,
            error=error,
            consume_attempt=True,
        )
        is True
    )
    return await _owner_outbox_failure_state(record.outbox_id)


async def _create_handoff_outbox(container: Container) -> tuple[UUID, str, str]:
    """Persist one uniquely addressable handoff projection for worker integration cases."""
    tag = uuid4().hex
    tenant_id = uuid4()
    await container.tenant_repo.add(
        Tenant(
            tenant_id,
            f"oidc|outbox-{tag}",
            f"outbox-{tag}@example.com",
            f"outbox-{tag}@u.concierge.test",
        )
    )
    canonical = (
        await container.catalog.upsert_candidates(
            [
                CandidateEvent(
                    source=Source.PUBLIC_JSONLD,
                    source_event_id=f"outbox-handoff-{tag}",
                    title=f"Outbox handoff {tag}",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=2),
                    registration_url=f"https://example.test/events/outbox-{tag}",
                    city="New York",
                    description="worker fixture",
                    is_free=True,
                )
            ]
        )
    )[0]
    workflow_id = registration_workflow_id(tenant_id, canonical.canonical_event_id)

    result = await container.registration.register_event(
        tenant_id,
        canonical,
        workflow_id,
        (Lane.HANDOFF,),
    )
    assert result.status.value == "handoff"
    assert result.handoff_task_id is not None
    return tenant_id, workflow_id, tag


async def test_worker_delivers_atomic_handoff_outbox_once_with_durable_ledger(db: None) -> None:
    """A real guarded transition becomes one durable, deduplicated notification (ADR-007/009)."""
    settings = get_settings()
    container = build_container(settings, register_sources={})
    tenant_id, workflow_id, tag = await _create_handoff_outbox(container)

    async with system_session_scope() as session:
        projection = (
            (
                await session.execute(
                    text(
                        """SELECT payload
                       FROM outbox
                       WHERE tenant_id = :tenant_id AND topic = 'lifecycle.handoff'
                       ORDER BY id DESC
                       LIMIT 1"""
                    ),
                    {"tenant_id": tenant_id},
                )
            )
            .one()
            .payload
        )
    assert "completion_url" not in projection
    protected_value = projection.get("protected_completion_url")
    assert isinstance(protected_value, str)
    revealed_url = await container.notification_secret_protector.reveal_completion_url(
        tenant_id,
        protected_value,
    )
    serialized_projection = json.dumps(projection, sort_keys=True)
    if revealed_url in serialized_projection:
        raise AssertionError("outbox projection retained a plaintext completion capability")

    # The development database intentionally persists integration fixtures. A large bounded batch
    # lets this real worker reach this test's fresh row without relying on global outbox ordering.
    delivery = NotificationDeliveryWorker(
        container.outbox_repo,
        container.notifier,
        container.notification_secret_protector,
    )
    await delivery.run_once(limit=10_000)
    first_delivery = [item for item in container.notifier.sent if item.tenant_id == tenant_id]
    assert len(first_delivery) == 1
    assert first_delivery[0].deep_link == f"https://example.test/events/outbox-{tag}"
    assert first_delivery[0].dedup_key.startswith(f"{workflow_id}:handoff_available:")

    # A second poll must not create a second user-visible notification for the same committed row.
    await delivery.run_once(limit=10_000)
    assert [
        item for item in container.notifier.sent if item.tenant_id == tenant_id
    ] == first_delivery

    async with system_session_scope() as session:
        row = (
            await session.execute(
                text(
                    """SELECT o.delivered_at, o.attempt_count, o.payload, l.state, l.dedup_key
                       FROM outbox AS o
                       JOIN notification_ledger AS l ON l.outbox_id = o.id
                       WHERE o.tenant_id = :tenant_id AND o.topic = 'lifecycle.handoff'
                       ORDER BY o.id DESC
                       LIMIT 1"""
                ),
                {"tenant_id": tenant_id},
            )
        ).one()
    assert row.delivered_at is not None
    assert int(row.attempt_count) == 0
    assert row.state == "delivered"
    assert row.dedup_key == first_delivery[0].dedup_key
    assert "protected_completion_url" not in row.payload


async def test_worker_busy_ledger_deferral_does_not_consume_retry_budget(db: None) -> None:
    """A durable ledger lease defers a real row without spending a provider-failure attempt."""
    settings = get_settings()
    container = build_container(settings, register_sources={})
    tenant_id, workflow_id, _ = await _create_handoff_outbox(container)

    async with system_session_scope() as session:
        row = (
            await session.execute(
                text(
                    """SELECT id, payload ->> 'transition_id' AS transition_id
                       FROM outbox
                       WHERE tenant_id = :tenant_id AND topic = 'lifecycle.handoff'
                       ORDER BY id DESC
                       LIMIT 1"""
                ),
                {"tenant_id": tenant_id},
            )
        ).one()
        outbox_id = int(row.id)
        dedup_key = f"{workflow_id}:handoff_available:{row.transition_id}"
        await session.execute(
            text(
                """INSERT INTO notification_ledger
                       (dedup_key, outbox_id, tenant_id, state, lease_token, lease_expires_at)
                   VALUES (:dedup_key, :outbox_id, :tenant_id, 'sending', 'other-worker',
                           now() + INTERVAL '10 minutes')"""
            ),
            {
                "dedup_key": dedup_key,
                "outbox_id": outbox_id,
                "tenant_id": tenant_id,
            },
        )

    # The persistent development database can contain old fixtures; a large bounded batch reliably
    # reaches this test's fresh row without relying on global outbox ordering.
    delivery = NotificationDeliveryWorker(
        container.outbox_repo,
        container.notifier,
        container.notification_secret_protector,
    )
    await delivery.run_once(limit=10_000)

    async with system_session_scope() as session:
        deferred = (
            await session.execute(
                text(
                    """SELECT attempt_count, delivered_at, lease_token, last_error
                       FROM outbox
                       WHERE id = :outbox_id"""
                ),
                {"outbox_id": outbox_id},
            )
        ).one()
    assert int(deferred.attempt_count) == 0
    assert deferred.delivered_at is None
    assert deferred.lease_token is None
    assert deferred.last_error == "notification ledger is leased by another worker"
    assert [item for item in container.notifier.sent if item.tenant_id == tenant_id] == []

    # Do not leave a deliberately deferred row to be re-leased by unrelated integration cases.
    async with system_session_scope() as session:
        await session.execute(
            text("DELETE FROM notification_ledger WHERE outbox_id = :outbox_id"),
            {"outbox_id": outbox_id},
        )
        await session.execute(
            text(
                """UPDATE outbox
                   SET delivered_at = now(), lease_token = NULL, lease_expires_at = NULL
                   WHERE id = :outbox_id"""
            ),
            {"outbox_id": outbox_id},
        )


async def test_worker_reopened_outbox_uses_durable_ledger_without_a_second_send(db: None) -> None:
    """A lost outbox acknowledgement cannot repeat a completed provider notification (FR-6.6, ADR-009)."""
    settings = get_settings()
    container = build_container(settings, register_sources={})
    tenant_id, _, _ = await _create_handoff_outbox(container)
    notifier = TenantCountingNotifier(tenant_id)
    delivery = NotificationDeliveryWorker(
        container.outbox_repo,
        notifier,
        container.notification_secret_protector,
    )

    await delivery.run_once(limit=10_000)
    assert notifier.calls == 1
    assert len(notifier.notifications) == 1
    async with system_session_scope() as session:
        row = (
            await session.execute(
                text(
                    """SELECT o.id, o.delivered_at, l.state, l.dedup_key
                       FROM outbox AS o
                       JOIN notification_ledger AS l ON l.outbox_id = o.id
                       WHERE o.tenant_id = :tenant_id AND o.topic = 'lifecycle.handoff'
                       ORDER BY o.id DESC
                       LIMIT 1"""
                ),
                {"tenant_id": tenant_id},
            )
        ).one()
        outbox_id = int(row.id)
        assert row.delivered_at is not None
        assert row.state == "delivered"
        original_dedup_key = str(row.dedup_key)
        await session.execute(
            text(
                """UPDATE outbox
                   SET delivered_at = NULL,
                       failed_at = NULL,
                       next_attempt_at = now(),
                       lease_token = NULL,
                       lease_expires_at = NULL
                   WHERE id = :outbox_id"""
            ),
            {"outbox_id": outbox_id},
        )

    stats = await delivery.run_once(limit=10_000)

    assert stats.acknowledged >= 1
    assert notifier.calls == 1
    assert len(notifier.notifications) == 1
    async with system_session_scope() as session:
        delivered = (
            await session.execute(
                text(
                    """SELECT o.delivered_at, o.attempt_count, l.state, l.dedup_key
                       FROM outbox AS o
                       JOIN notification_ledger AS l ON l.outbox_id = o.id
                       WHERE o.id = :outbox_id"""
                ),
                {"outbox_id": outbox_id},
            )
        ).one()
    assert delivered.delivered_at is not None
    assert int(delivered.attempt_count) == 0
    assert delivered.state == "delivered"
    assert delivered.dedup_key == original_dedup_key


async def test_stale_outbox_record_cannot_claim_a_notification_slot(db: None) -> None:
    """An expired, reclaimed, or terminal row cannot authorize a late notification send (ADR-009, NFR-8)."""
    settings = get_settings()
    container = build_container(settings, register_sources={})
    tenant_id, workflow_id, _ = await _create_handoff_outbox(container)

    async with system_session_scope() as session:
        row = (
            await session.execute(
                text(
                    """SELECT id, payload ->> 'transition_id' AS transition_id
                       FROM outbox
                       WHERE tenant_id = :tenant_id AND topic = 'lifecycle.handoff'
                       ORDER BY id DESC
                       LIMIT 1"""
                ),
                {"tenant_id": tenant_id},
            )
        ).one()
        outbox_id = int(row.id)
        dedup_key = f"{workflow_id}:handoff_available:{row.transition_id}"
        await session.execute(
            text(
                """UPDATE outbox
                   SET lease_token = 'stale-owner',
                       lease_expires_at = now() - INTERVAL '1 second',
                       delivered_at = NULL,
                       failed_at = NULL
                   WHERE id = :outbox_id"""
            ),
            {"outbox_id": outbox_id},
        )

    stale_record = OutboxRecord(
        outbox_id=outbox_id,
        tenant_id=tenant_id,
        topic="lifecycle.handoff",
        payload={},
        attempt_count=0,
        lease_token="stale-owner",
    )
    expired = await container.outbox_repo.claim_notification(
        stale_record, dedup_key=dedup_key, lease_seconds=60
    )

    async with system_session_scope() as session:
        await session.execute(
            text(
                """UPDATE outbox
                   SET lease_token = 'current-owner',
                       lease_expires_at = now() + INTERVAL '1 minute'
                   WHERE id = :outbox_id"""
            ),
            {"outbox_id": outbox_id},
        )
    reclaimed = await container.outbox_repo.claim_notification(
        stale_record, dedup_key=dedup_key, lease_seconds=60
    )

    async with system_session_scope() as session:
        await session.execute(
            text(
                """UPDATE outbox
                   SET lease_token = 'stale-owner',
                       lease_expires_at = now() + INTERVAL '1 minute',
                       failed_at = now()
                   WHERE id = :outbox_id"""
            ),
            {"outbox_id": outbox_id},
        )
    terminal = await container.outbox_repo.claim_notification(
        stale_record, dedup_key=dedup_key, lease_seconds=60
    )

    assert expired is NotificationClaim.LEASE_LOST
    assert reclaimed is NotificationClaim.LEASE_LOST
    assert terminal is NotificationClaim.LEASE_LOST
    async with system_session_scope() as session:
        ledger_count = await session.execute(
            text("SELECT count(*) FROM notification_ledger WHERE dedup_key = :dedup_key"),
            {"dedup_key": dedup_key},
        )
    assert int(ledger_count.scalar_one()) == 0

    async with system_session_scope() as session:
        await session.execute(
            text(
                """UPDATE outbox
                   SET lease_token = 'current-owner',
                       lease_expires_at = now() + INTERVAL '5 seconds',
                       failed_at = NULL
                   WHERE id = :outbox_id"""
            ),
            {"outbox_id": outbox_id},
        )
    current_record = OutboxRecord(
        outbox_id=outbox_id,
        tenant_id=tenant_id,
        topic="lifecycle.handoff",
        payload={},
        attempt_count=0,
        lease_token="current-owner",
    )
    current = await container.outbox_repo.claim_notification(
        current_record, dedup_key=dedup_key, lease_seconds=60
    )

    assert current is NotificationClaim.ACQUIRED
    assert await container.outbox_repo.has_notification_send_authority(
        current_record, dedup_key=dedup_key
    )
    async with system_session_scope() as session:
        ownership = (
            await session.execute(
                text(
                    """SELECT o.lease_token AS outbox_lease_token,
                                      o.lease_expires_at AS outbox_lease_expires_at,
                                      l.lease_token AS ledger_lease_token,
                                      l.lease_expires_at AS ledger_lease_expires_at,
                                      o.lease_expires_at > clock_timestamp() + INTERVAL '50 seconds'
                                          AS outbox_window_reserved,
                                      l.lease_expires_at > clock_timestamp() + INTERVAL '50 seconds'
                                          AS ledger_window_reserved
                       FROM outbox AS o
                       JOIN notification_ledger AS l ON l.dedup_key = :dedup_key
                       WHERE o.id = :outbox_id"""
                ),
                {"dedup_key": dedup_key, "outbox_id": outbox_id},
            )
        ).one()
    assert ownership.outbox_lease_token == "current-owner"
    assert ownership.ledger_lease_token == "current-owner"
    assert ownership.ledger_lease_expires_at <= ownership.outbox_lease_expires_at
    assert ownership.outbox_window_reserved is True
    assert ownership.ledger_window_reserved is True
    async with system_session_scope() as session:
        await session.execute(
            text(
                """UPDATE outbox
                   SET lease_expires_at = now() - INTERVAL '1 second'
                   WHERE id = :outbox_id"""
            ),
            {"outbox_id": outbox_id},
        )
    assert not await container.outbox_repo.has_notification_send_authority(
        current_record, dedup_key=dedup_key
    )
    async with system_session_scope() as session:
        await session.execute(
            text(
                """UPDATE outbox
                   SET lease_expires_at = now() + INTERVAL '1 minute'
                   WHERE id = :outbox_id"""
            ),
            {"outbox_id": outbox_id},
        )
    assert await container.outbox_repo.mark_notification_delivered(
        current_record, dedup_key=dedup_key
    )
    assert await container.outbox_repo.mark_delivered(current_record)


async def test_expired_notification_failure_writes_require_live_leases_before_reclaim(
    db: None,
) -> None:
    """An expired failed-send owner cannot release, retry, or terminalize before recovery (ADR-009)."""
    repository = PostgresOutboxRepository()
    tenant_id = uuid4()
    retry_outbox_id, terminal_outbox_id = _isolated_outbox_ids(2)
    await _owner_seed_outbox(retry_outbox_id, tenant_id)
    await _owner_seed_outbox(terminal_outbox_id, tenant_id)
    claimed = await _claim_fixture_records(repository, {retry_outbox_id, terminal_outbox_id})
    retry_record = claimed[retry_outbox_id]
    terminal_record = claimed[terminal_outbox_id]
    retry_dedup_key = f"p24:retry:{retry_outbox_id}"
    terminal_dedup_key = f"p24:terminal:{terminal_outbox_id}"

    assert (
        await repository.claim_notification(
            retry_record, dedup_key=retry_dedup_key, lease_seconds=300
        )
        is NotificationClaim.ACQUIRED
    )
    assert (
        await repository.claim_notification(
            terminal_record, dedup_key=terminal_dedup_key, lease_seconds=300
        )
        is NotificationClaim.ACQUIRED
    )
    await _owner_expire_outbox_lease(retry_record)
    await _owner_expire_notification_lease(retry_record, retry_dedup_key)
    await _owner_expire_outbox_lease(terminal_record)
    await _owner_expire_notification_lease(terminal_record, terminal_dedup_key)

    await _assert_expired_notification_failure_is_a_noop(
        repository,
        retry_record,
        retry_dedup_key,
        retry_at=datetime(2099, 1, 1, tzinfo=UTC),
        error="stale provider retry after expired lease",
    )
    await _assert_expired_notification_failure_is_a_noop(
        repository,
        terminal_record,
        terminal_dedup_key,
        retry_at=None,
        error="stale provider terminal failure after expired lease",
    )

    reclaimed = await _claim_fixture_records(repository, {retry_outbox_id, terminal_outbox_id})
    fresh_retry_record = reclaimed[retry_outbox_id]
    fresh_terminal_record = reclaimed[terminal_outbox_id]
    assert fresh_retry_record.lease_token != retry_record.lease_token
    assert fresh_terminal_record.lease_token != terminal_record.lease_token
    retried = await _recover_notification_failure(
        repository,
        fresh_retry_record,
        retry_dedup_key,
        retry_at=datetime(2099, 1, 2, tzinfo=UTC),
        error="fresh provider retry after reclaim",
    )
    terminalized = await _recover_notification_failure(
        repository,
        fresh_terminal_record,
        terminal_dedup_key,
        retry_at=None,
        error="fresh provider terminal failure after reclaim",
    )
    assert retried.attempt_count == 1
    assert retried.failed_at is None
    assert retried.lease_token is None
    assert terminalized.attempt_count == 1
    assert terminalized.failed_at is not None
    assert terminalized.lease_token is None


async def test_stale_ledger_release_cannot_mutate_after_fresh_outbox_claim(
    db: None,
) -> None:
    """An old ledger lease cannot undo a fresh outbox claim before the new ledger claim (ADR-009)."""
    repository = PostgresOutboxRepository()
    tenant_id = uuid4()
    outbox_id = _isolated_outbox_ids(1)[0]
    dedup_key = f"p24:cross-owner:{outbox_id}"
    await _owner_seed_outbox(outbox_id, tenant_id)
    old_record = (await _claim_fixture_records(repository, {outbox_id}))[outbox_id]
    assert (
        await repository.claim_notification(old_record, dedup_key=dedup_key, lease_seconds=300)
        is NotificationClaim.ACQUIRED
    )

    await _owner_expire_outbox_lease(old_record)
    old_ledger = await _owner_notification_ledger_state(dedup_key)
    assert old_ledger.lease_expired is False
    fresh_record = (await _claim_fixture_records(repository, {outbox_id}))[outbox_id]
    assert fresh_record.lease_token != old_record.lease_token
    before_stale_release = await _owner_notification_ledger_state(dedup_key)
    assert before_stale_release == old_ledger

    # No fresh ledger claim has run yet: this isolates the old-ledger/new-outbox binding failure.
    assert await repository.release_notification(old_record, dedup_key=dedup_key) is False
    assert await _owner_notification_ledger_state(dedup_key) == before_stale_release

    await _owner_expire_notification_lease(old_record, dedup_key)
    assert (
        await repository.claim_notification(fresh_record, dedup_key=dedup_key, lease_seconds=300)
        is NotificationClaim.ACQUIRED
    )
    assert await repository.release_notification(fresh_record, dedup_key=dedup_key) is True
    assert (
        await repository.reschedule(
            fresh_record,
            retry_at=None,
            error="fresh provider terminal cleanup after ledger recovery",
            consume_attempt=False,
        )
        is True
    )
