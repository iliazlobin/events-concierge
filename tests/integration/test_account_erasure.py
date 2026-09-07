"""Transactional PostgreSQL account-erasure, fencing, replay, and isolation coverage."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from events_concierge.adapters.mock.notification_secrets import (
    DevelopmentNotificationSecretProtector,
)
from events_concierge.adapters.postgres.account_erasure import (
    PostgresAccountErasureRepository,
)
from events_concierge.adapters.postgres.tenant_effects import PostgresTenantEffectAuthority
from events_concierge.adapters.postgres.tenant_repos import (
    PostgresOutboxRepository,
    PostgresRequestRepository,
    PostgresTenantRepository,
)
from events_concierge.application.outbox import OutboxRelay, RelayStats
from events_concierge.domain.account_erasure import (
    AccountErasureFailureStage,
    AccountErasureSnapshot,
    AccountErasureStage,
    AccountErasureStatus,
)
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.request import EventRequest, RequestConstraints
from events_concierge.infra.db import system_session_scope
from events_concierge.ports.account_erasure import AccountErasureConflictError
from events_concierge.ports.notifications import Notification
from events_concierge.ports.repositories import OutboxRecord
from events_concierge.ports.tenant_effects import (
    TenantEffectFencedError,
    TenantEffectKind,
    TenantEffectRequest,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _AccountSatelliteCase:
    """One tenant-owned account relation that must obey the erasure write fence."""

    table: str
    insert_sql: str
    update_sql: str
    delete_sql: str


_ACCOUNT_SATELLITE_CASES = (
    _AccountSatelliteCase(
        table="tenant_profiles",
        insert_sql="""INSERT INTO public.tenant_profiles (
                          tenant_id, display_name, time_zone, revision
                      ) VALUES (:tenant_id, :display_name, 'UTC', 1)""",
        update_sql="""UPDATE public.tenant_profiles
                      SET display_name = :display_name,
                          revision = revision + 1,
                          updated_at = clock_timestamp()
                      WHERE tenant_id = :tenant_id
                      RETURNING tenant_id""",
        delete_sql="""DELETE FROM public.tenant_profiles
                      WHERE tenant_id = :tenant_id
                      RETURNING tenant_id""",
    ),
    _AccountSatelliteCase(
        table="tenant_profile_avatars",
        insert_sql="""INSERT INTO public.tenant_profile_avatars (
                          tenant_id, storage_key, content_type, byte_size,
                          width_px, height_px, checksum_sha256
                      ) VALUES (
                          :tenant_id, :storage_key, 'image/webp', 128,
                          16, 16, :digest
                      )""",
        update_sql="""UPDATE public.tenant_profile_avatars
                      SET storage_key = :storage_key,
                          checksum_sha256 = :digest,
                          created_at = clock_timestamp()
                      WHERE tenant_id = :tenant_id
                      RETURNING tenant_id""",
        delete_sql="""DELETE FROM public.tenant_profile_avatars
                      WHERE tenant_id = :tenant_id
                      RETURNING tenant_id""",
    ),
    _AccountSatelliteCase(
        table="tenant_roles",
        insert_sql="""INSERT INTO public.tenant_roles (
                          tenant_id, role, granted_by
                      ) VALUES (:tenant_id, :role, :granted_by)""",
        update_sql="""UPDATE public.tenant_roles
                      SET role = :role, granted_by = :granted_by
                      WHERE tenant_id = :tenant_id
                      RETURNING tenant_id""",
        delete_sql="""DELETE FROM public.tenant_roles
                      WHERE tenant_id = :tenant_id
                      RETURNING tenant_id""",
    ),
    _AccountSatelliteCase(
        table="tenant_api_keys",
        insert_sql="""INSERT INTO public.tenant_api_keys (
                          key_id, tenant_id, name, key_prefix, key_hash
                      ) VALUES (
                          :key_id, :tenant_id, :key_name, :key_prefix, :digest
                      )""",
        update_sql="""UPDATE public.tenant_api_keys
                      SET name = :key_name, last_used_at = clock_timestamp()
                      WHERE tenant_id = :tenant_id
                      RETURNING tenant_id""",
        delete_sql="""DELETE FROM public.tenant_api_keys
                      WHERE tenant_id = :tenant_id
                      RETURNING tenant_id""",
    ),
)


def _tenant(label: str) -> Tenant:
    tenant_id = uuid4()
    tag = f"erasure-{label}-{tenant_id.hex}"
    return Tenant(
        tenant_id=tenant_id,
        oidc_subject=f"oidc|{tag}",
        notify_email=f"{tag}@example.test",
        relay_inbox=f"{tag}@u.example.test",
    )


def _owner_url() -> str:
    url = os.environ.get("EC_MIGRATION_URL")
    if not url:
        pytest.skip("EC_MIGRATION_URL not set; run make test-integration")
    return url


def _account_satellite_params(
    tenant_id: UUID,
    label: str,
    *,
    role: str = "admin",
) -> dict[str, object]:
    """Build valid, unique values shared by the four account-satellite fixtures."""
    nonce = uuid4().hex
    digest = nonce * 2
    return {
        "tenant_id": tenant_id,
        "display_name": f"{label}-{nonce[:8]}",
        "storage_key": f"{digest}.webp",
        "digest": digest,
        "role": role,
        "granted_by": f"erasure-test:{label}",
        "key_id": uuid4(),
        "key_name": f"{label}-{nonce[:8]}",
        "key_prefix": f"ec_{nonce[:8]}",
    }


async def _seed_account_satellites(
    connection: AsyncConnection,
    tenant_id: UUID,
    *,
    label: str,
) -> None:
    """Seed every account satellite that must cascade with the tenant identity."""
    parameters = _account_satellite_params(tenant_id, label)
    for case in _ACCOUNT_SATELLITE_CASES:
        await connection.execute(text(case.insert_sql), parameters)


async def _assert_fenced_write(
    owner: AsyncEngine,
    *,
    case: _AccountSatelliteCase,
    operation: str,
    statement: str,
    parameters: dict[str, object],
) -> None:
    """Assert the shared trigger, rather than an incidental constraint, rejected a write."""
    with pytest.raises(DBAPIError, match="tenant account is fenced for erasure") as denied:
        async with owner.begin() as connection:
            await connection.execute(text(statement), parameters)
    assert getattr(denied.value.orig, "sqlstate", None) == "55000", (
        f"{case.table} {operation} was not rejected by the account-erasure fence"
    )


class _BlockingNotifier:
    """Model a cancellation-resistant provider request behind the real relay boundary."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.completed = False
        self.calls = 0

    async def send(self, notification: Notification) -> None:
        del notification
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        self.completed = True


class _TenantScopedOutboxRepository(PostgresOutboxRepository):
    """Claim only one fixture row while retaining the production repository behavior afterward."""

    def __init__(self, tenant_id: UUID, outbox_id: int) -> None:
        self._target_tenant_id = tenant_id
        self._target_outbox_id = outbox_id

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[OutboxRecord]:
        """Lease the test's exact row without consuming unrelated module-fixture notifications."""
        if limit < 1:
            return []
        lease_token = uuid4().hex
        async with system_session_scope() as session:
            row = (
                await session.execute(
                    text(
                        """UPDATE public.outbox
                           SET lease_token = :lease_token,
                               lease_expires_at =
                                   now() + (:lease_seconds * INTERVAL '1 second')
                           WHERE id = :outbox_id
                             AND tenant_id = :tenant_id
                             AND delivered_at IS NULL
                             AND failed_at IS NULL
                             AND next_attempt_at <= now()
                             AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                           RETURNING id, tenant_id, topic, payload, attempt_count, lease_token"""
                    ),
                    {
                        "outbox_id": self._target_outbox_id,
                        "tenant_id": self._target_tenant_id,
                        "lease_token": lease_token,
                        "lease_seconds": lease_seconds,
                    },
                )
            ).one_or_none()
        if row is None:
            return []
        if not isinstance(row.payload, dict):
            raise AssertionError("fixture outbox payload must decode as a JSON object")
        return [
            OutboxRecord(
                outbox_id=int(row.id),
                tenant_id=row.tenant_id,
                topic=str(row.topic),
                payload=row.payload,
                attempt_count=int(row.attempt_count),
                lease_token=str(row.lease_token),
            )
        ]


async def _insert_notification_outbox(owner: AsyncEngine, tenant_id: UUID) -> int:
    """Insert and identify the exact row used by the notification/erasure race fixture."""
    async with owner.begin() as connection:
        return int(
            (
                await connection.execute(
                    text(
                        """INSERT INTO public.outbox (tenant_id, topic, payload)
                           VALUES (:tenant_id, 'request.failed_no_candidate', '{}'::jsonb)
                           RETURNING id"""
                    ),
                    {"tenant_id": tenant_id},
                )
            ).scalar_one()
        )


async def _release_and_drain_notification_race_tasks(
    notifier: _BlockingNotifier,
    *tasks: asyncio.Task[object] | None,
) -> None:
    """Make a failed race assertion unable to strand the simulated provider or database lock."""
    notifier.release.set()
    started_tasks = tuple(task for task in tasks if task is not None)
    for task in started_tasks:
        if not task.done():
            task.cancel()
    if started_tasks:
        await asyncio.wait_for(asyncio.gather(*started_tasks, return_exceptions=True), timeout=5)


async def _seed_tenant_graph(
    connection: AsyncConnection,
    tenant: Tenant,
    *,
    label: str,
) -> tuple[UUID, UUID, UUID, str, str]:
    """Seed every important tenant-data root and dependent control queue with private values."""
    request_id, lifecycle_id, event_id = uuid4(), uuid4(), uuid4()
    workflow_id = f"{tenant.tenant_id}:{event_id}"
    task_id = f"{workflow_id}:handoff"
    transition_id = f"{workflow_id}:scheduled:1"
    expiry_transition_id = f"{workflow_id}:handoff-expiry:{task_id}"
    consent_id = uuid4()
    signal_id = uuid4()
    change_fingerprint = f"change-{label}-{uuid4().hex}"
    now = datetime.now(UTC)
    ttl = now + timedelta(days=7)

    # Several guarded dependent-row triggers require the same explicit tenant context even for
    # migration-owner fixture writes.
    await connection.execute(
        text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
        {"tenant_id": str(tenant.tenant_id)},
    )

    await connection.execute(
        text(
            """INSERT INTO public.canonical_events (
                   canonical_event_id, title, start_at, end_at, venue_name, city_norm,
                   description, price_status
               ) VALUES (
                   :event_id, :title, :start_at, :end_at, :venue, 'san francisco',
                   :description, 'free'
               )"""
        ),
        {
            "event_id": event_id,
            "title": f"{label} private title",
            "start_at": now + timedelta(days=10),
            "end_at": now + timedelta(days=10, hours=2),
            "venue": f"{label} private venue",
            "description": f"{label} private description",
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.event_source_links (
                   source, source_event_id, canonical_event_id, registration_url, price_status
               ) VALUES ('meetup', :source_event_id, :event_id, :url, 'free')"""
        ),
        {
            "source_event_id": f"erasure-{label}-{event_id}",
            "event_id": event_id,
            "url": f"https://events.example.test/private/{event_id}",
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.event_requests (
                   request_id, tenant_id, raw_text, constraints, state
               ) VALUES (:request_id, :tenant_id, :raw_text, '{}'::jsonb, 'started')"""
        ),
        {
            "request_id": request_id,
            "tenant_id": tenant.tenant_id,
            "raw_text": f"{label} private raw request",
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.lifecycle (
                   lifecycle_id, tenant_id, canonical_event_id, workflow_id, state, lane,
                   registration_source
               ) VALUES (
                   :lifecycle_id, :tenant_id, :event_id, :workflow_id, 'scheduled',
                   'autonomous_sla', 'meetup'
               )"""
        ),
        {
            "lifecycle_id": lifecycle_id,
            "tenant_id": tenant.tenant_id,
            "event_id": event_id,
            "workflow_id": workflow_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.transition_ledger (
                   transition_id, tenant_id, lifecycle_id, to_state
               ) VALUES (:transition_id, :tenant_id, :lifecycle_id, 'scheduled')"""
        ),
        {
            "transition_id": transition_id,
            "tenant_id": tenant.tenant_id,
            "lifecycle_id": lifecycle_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.request_start_outbox (
                   request_id, tenant_id, dedup_key
               ) VALUES (:request_id, :tenant_id, :dedup_key)"""
        ),
        {
            "request_id": request_id,
            "tenant_id": tenant.tenant_id,
            "dedup_key": f"dedup-{label}-{request_id}",
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.request_outcome_links (
                   tenant_id, request_id, lifecycle_id
               ) VALUES (:tenant_id, :request_id, :lifecycle_id)"""
        ),
        {
            "tenant_id": tenant.tenant_id,
            "request_id": request_id,
            "lifecycle_id": lifecycle_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.handoff_tasks (
                   task_id, tenant_id, workflow_id, canonical_event_id, reason, deep_link,
                   event_summary, ttl_expires_at, state, metadata, expiry_transition_id
               ) VALUES (
                   :task_id, :tenant_id, :workflow_id, :event_id, 'no_autonomous_lane',
                   :deep_link, :summary, :ttl, 'open', :metadata, :expiry_transition_id
               )"""
        ),
        {
            "task_id": task_id,
            "tenant_id": tenant.tenant_id,
            "workflow_id": workflow_id,
            "event_id": event_id,
            "deep_link": f"https://events.example.test/private/{event_id}",
            "summary": f"{label} private summary",
            "ttl": ttl,
            "metadata": '{"private":"value"}',
            "expiry_transition_id": expiry_transition_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.handoff_expiry_queue (
                   task_id, tenant_id, workflow_id, canonical_event_id, expiry_transition_id,
                   ttl_expires_at, eligible_at, next_attempt_at
               ) VALUES (
                   :task_id, :tenant_id, :workflow_id, :event_id, :expiry_transition_id,
                   :ttl, :eligible, :eligible
               ) ON CONFLICT (task_id) DO NOTHING"""
        ),
        {
            "task_id": task_id,
            "tenant_id": tenant.tenant_id,
            "workflow_id": workflow_id,
            "event_id": event_id,
            "expiry_transition_id": expiry_transition_id,
            "ttl": ttl,
            "eligible": ttl + timedelta(minutes=5),
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.handoff_reminder_ledger (
                   reminder_id, tenant_id, task_id, workflow_id, canonical_event_id,
                   expiry_transition_id, reminder_kind
               ) VALUES (
                   :reminder_id, :tenant_id, :task_id, :workflow_id, :event_id,
                   :expiry_transition_id, 't24h'
               )"""
        ),
        {
            "reminder_id": f"reminder-{label}-{uuid4().hex}",
            "tenant_id": tenant.tenant_id,
            "task_id": task_id,
            "workflow_id": workflow_id,
            "event_id": event_id,
            "expiry_transition_id": expiry_transition_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.handoff_completion_attempts (
                   task_id, tenant_id, workflow_id, canonical_event_id, completion_id,
                   evidence, outcome, detail
               ) VALUES (
                   :task_id, :tenant_id, :workflow_id, :event_id, :completion_id,
                   'user_mark_done', 'review_required', :detail
               )"""
        ),
        {
            "task_id": task_id,
            "tenant_id": tenant.tenant_id,
            "workflow_id": workflow_id,
            "event_id": event_id,
            "completion_id": f"completion-{label}-{uuid4().hex}",
            "detail": f"{label} private review detail",
        },
    )
    outbox_id = (
        await connection.execute(
            text(
                """INSERT INTO public.outbox (tenant_id, topic, payload)
                   VALUES (:tenant_id, 'lifecycle.scheduled', :payload)
                   RETURNING id"""
            ),
            {
                "tenant_id": tenant.tenant_id,
                "payload": '{"event_summary":"private outbox summary"}',
            },
        )
    ).scalar_one()
    await connection.execute(
        text(
            """INSERT INTO public.notification_ledger (
                   dedup_key, outbox_id, tenant_id, state
               ) VALUES (:dedup_key, :outbox_id, :tenant_id, 'pending')"""
        ),
        {
            "dedup_key": f"notification-{label}-{uuid4().hex}",
            "outbox_id": outbox_id,
            "tenant_id": tenant.tenant_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.calendar_bindings (
                   tenant_id, write_calendar_id, free_busy_calendar_ids
               ) VALUES (:tenant_id, :calendar_id, :busy_ids)"""
        ),
        {
            "tenant_id": tenant.tenant_id,
            "calendar_id": f"private-{label}@calendar.example.test",
            "busy_ids": '["primary"]',
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.google_calendar_sync_state (tenant_id, calendar_id)
               VALUES (:tenant_id, :calendar_id)"""
        ),
        {
            "tenant_id": tenant.tenant_id,
            "calendar_id": f"private-{label}@calendar.example.test",
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.tenant_ranking_profiles (
                   tenant_id, explicit_affinities, implicit_affinities, revision
               ) VALUES (:tenant_id, :explicit, :implicit, 1)"""
        ),
        {
            "tenant_id": tenant.tenant_id,
            "explicit": '{"private-interest":1}',
            "implicit": '{"private-signal":0.5}',
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.tenant_ranking_feedback_receipts (
                   tenant_id, signal_id, canonical_event_id, signal_kind, feature_deltas
               ) VALUES (:tenant_id, :signal_id, :event_id, 'click', :deltas)"""
        ),
        {
            "tenant_id": tenant.tenant_id,
            "signal_id": signal_id,
            "event_id": event_id,
            "deltas": '{"private":0.5}',
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.tenant_source_consents (
                   consent_id, tenant_id, source, modality
               ) VALUES (:consent_id, :tenant_id, 'meetup', 'api')"""
        ),
        {"consent_id": consent_id, "tenant_id": tenant.tenant_id},
    )
    await connection.execute(
        text(
            """INSERT INTO public.registration_action_audit (
                   audit_key, tenant_id, workflow_id, source, modality, phase,
                   policy_decision, consent_ref
               ) VALUES (
                   :audit_key, :tenant_id, :workflow_id, 'meetup', 'api',
                   'policy_precheck', 'allowed', :consent_id
               )"""
        ),
        {
            "audit_key": f"audit-{label}-{uuid4().hex}",
            "tenant_id": tenant.tenant_id,
            "workflow_id": workflow_id,
            "consent_id": consent_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.lifecycle_watch_projection_outbox (
                   lifecycle_id, tenant_id, workflow_id, canonical_event_id, source,
                   action, active_since
               ) VALUES (
                   :lifecycle_id, :tenant_id, :workflow_id, :event_id, 'meetup',
                   'register', :active_since
               )"""
        ),
        {
            "lifecycle_id": lifecycle_id,
            "tenant_id": tenant.tenant_id,
            "workflow_id": workflow_id,
            "event_id": event_id,
            "active_since": now,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.watch_registry (canonical_event_id, source)
               VALUES (:event_id, 'meetup')"""
        ),
        {"event_id": event_id},
    )
    await connection.execute(
        text(
            """INSERT INTO public.watch_subscriptions (
                   canonical_event_id, source, tenant_id, workflow_id
               ) VALUES (:event_id, 'meetup', :tenant_id, :workflow_id)"""
        ),
        {
            "event_id": event_id,
            "tenant_id": tenant.tenant_id,
            "workflow_id": workflow_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.event_changes (
                   fingerprint, canonical_event_id, source, event_status, title
               ) VALUES (:fingerprint, :event_id, 'meetup', 'cancelled', :title)"""
        ),
        {
            "fingerprint": change_fingerprint,
            "event_id": event_id,
            "title": f"{label} private changed title",
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.event_change_deliveries (
                   fingerprint, tenant_id, workflow_id
               ) VALUES (:fingerprint, :tenant_id, :workflow_id)"""
        ),
        {
            "fingerprint": change_fingerprint,
            "tenant_id": tenant.tenant_id,
            "workflow_id": workflow_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.event_change_calendar_repairs (
                   fingerprint, tenant_id, workflow_id
               ) VALUES (:fingerprint, :tenant_id, :workflow_id)"""
        ),
        {
            "fingerprint": change_fingerprint,
            "tenant_id": tenant.tenant_id,
            "workflow_id": workflow_id,
        },
    )
    await connection.execute(
        text(
            """INSERT INTO public.lifecycle_organizer_change_ledger (
                   tenant_id, workflow_id, fingerprint
               ) VALUES (:tenant_id, :workflow_id, :fingerprint)"""
        ),
        {
            "tenant_id": tenant.tenant_id,
            "workflow_id": workflow_id,
            "fingerprint": change_fingerprint,
        },
    )
    await _seed_account_satellites(connection, tenant.tenant_id, label=label)
    return request_id, lifecycle_id, event_id, workflow_id, transition_id


async def test_account_erasure_fences_writes_purges_tenant_graph_and_retains_shredded_audit(  # noqa: PLR0915 -- one end-to-end transactional assertion
    db: None,
) -> None:
    erased, survivor = _tenant("erased"), _tenant("survivor")
    tenant_repository = PostgresTenantRepository()
    await tenant_repository.add(erased)
    await tenant_repository.add(survivor)
    owner = create_async_engine(_owner_url(), pool_pre_ping=True)
    try:
        async with owner.begin() as connection:
            request_id, _, event_id, workflow_id, _ = await _seed_tenant_graph(
                connection,
                erased,
                label="erased",
            )
            survivor_request_id, _, _, _, _ = await _seed_tenant_graph(
                connection,
                survivor,
                label="survivor",
            )
            orphan_workflow_id = f"{erased.tenant_id}:{uuid4()}"
            await connection.execute(
                text(
                    """INSERT INTO public.tenant_workflow_registry (tenant_id, workflow_id)
                       VALUES (:tenant_id, :workflow_id)"""
                ),
                {"tenant_id": erased.tenant_id, "workflow_id": orphan_workflow_id},
            )

        erasure_request_id = uuid4()
        repository = PostgresAccountErasureRepository()
        started = await repository.begin(erased.tenant_id, erasure_request_id)

        assert started.status is AccountErasureStatus.ERASING
        assert started.request_id == erasure_request_id
        assert started.workflow_ids == tuple(
            sorted((f"req:{erased.tenant_id}:{request_id}", workflow_id, orphan_workflow_id))
        )
        assert started.canonical_event_ids == (event_id,)
        assert started.workflow_target_count == 3
        assert started.calendar_target_count == 1
        assert started.calendar_binding_expected is True
        assert started.external_effects_completed is False
        assert started.retained_audit_rows == 1

        replay = await repository.begin(erased.tenant_id, erasure_request_id)
        assert replay == started
        with pytest.raises(AccountErasureConflictError):
            await repository.begin(erased.tenant_id, uuid4())

        # The durable target snapshot, not mutable lifecycle/request rows, owns every later
        # destructive boundary. Deletes serialize on the same fence lock and cannot shrink it.
        async with owner.begin() as connection:
            await connection.execute(
                text("DELETE FROM public.lifecycle WHERE tenant_id = :tenant_id"),
                {"tenant_id": erased.tenant_id},
            )
            await connection.execute(
                text("DELETE FROM public.event_requests WHERE tenant_id = :tenant_id"),
                {"tenant_id": erased.tenant_id},
            )
        after_live_delete = await repository.get(erased.tenant_id)
        assert after_live_delete == started

        # A bounded system lease makes the same tombstone independently resumable. Only an exact
        # live token can release it, and retained retry state is a fixed stage rather than an
        # exception/provider string.
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """UPDATE public.account_erasure_requests
                       SET next_attempt_at = clock_timestamp()
                       WHERE tenant_id = :tenant_id"""
                ),
                {"tenant_id": erased.tenant_id},
            )
        leases = await repository.claim_batch(10, 120)
        lease = next(item for item in leases if item.tenant_id == erased.tenant_id)
        assert lease.request_id == erasure_request_id
        assert lease.attempt_count == 1
        assert await repository.release_lease(
            lease,
            failure_stage=AccountErasureFailureStage.EXTERNAL_EFFECTS,
            retry_after_seconds=30,
        )
        released = await repository.get(erased.tenant_id)
        assert released is not None
        assert released.last_failure_stage is AccountErasureFailureStage.EXTERNAL_EFFECTS
        assert not await repository.release_lease(
            lease,
            failure_stage=AccountErasureFailureStage.EXTERNAL_EFFECTS,
            retry_after_seconds=30,
        )

        # The fence is committed before external cleanup; late API/worker writes fail while a
        # different tenant remains writable.
        with pytest.raises(Exception, match="fenced for erasure"):
            await PostgresRequestRepository().add(
                EventRequest(
                    uuid4(), erased.tenant_id, "late private request", RequestConstraints()
                )
            )
        await PostgresRequestRepository().add(
            EventRequest(
                uuid4(),
                survivor.tenant_id,
                "survivor remains writable",
                RequestConstraints(),
            )
        )

        with pytest.raises(AccountErasureConflictError):
            await repository.complete_stage(
                erased.tenant_id,
                erasure_request_id,
                AccountErasureStage.WORKFLOWS,
                1,
            )
        for stage, count in (
            (AccountErasureStage.EXTERNAL_EFFECTS, 1),
            (AccountErasureStage.WORKFLOWS, 3),
            (AccountErasureStage.CALENDAR, 1),
            (AccountErasureStage.BROWSER_SESSIONS, 1),
            (AccountErasureStage.CREDENTIAL_VAULT, 1),
            (AccountErasureStage.OBJECT_STORE, 1),
        ):
            await repository.complete_stage(
                erased.tenant_id,
                erasure_request_id,
                stage,
                count,
            )
        # Exact stage ACK replays converge without changing the fixed inventory.
        await repository.complete_stage(
            erased.tenant_id,
            erasure_request_id,
            AccountErasureStage.CALENDAR,
            1,
        )

        completed = await repository.finalize(erased.tenant_id, erasure_request_id)
        assert completed.status is AccountErasureStatus.COMPLETED
        assert completed.workflow_ids == ()
        assert completed.canonical_event_ids == ()
        assert completed.external_effects_completed is True
        assert completed.retained_audit_rows == 1
        assert await tenant_repository.get(erased.tenant_id) is None
        assert await tenant_repository.get(survivor.tenant_id) == survivor

        async with owner.connect() as connection:
            erased_counts = (
                await connection.execute(
                    text(
                        """SELECT
                            (SELECT count(*) FROM public.tenants WHERE tenant_id = :tenant_id)
                                AS tenants,
                            (SELECT count(*) FROM public.event_requests
                             WHERE tenant_id = :tenant_id) AS event_requests,
                            (SELECT count(*) FROM public.lifecycle
                             WHERE tenant_id = :tenant_id) AS lifecycle,
                            (SELECT count(*) FROM public.transition_ledger
                             WHERE tenant_id = :tenant_id) AS transitions,
                            (SELECT count(*) FROM public.handoff_tasks
                             WHERE tenant_id = :tenant_id) AS handoffs,
                            (SELECT count(*) FROM public.outbox
                             WHERE tenant_id = :tenant_id) AS outbox,
                            (SELECT count(*) FROM public.notification_ledger
                             WHERE tenant_id = :tenant_id) AS notifications,
                            (SELECT count(*) FROM public.calendar_bindings
                             WHERE tenant_id = :tenant_id) AS calendar_bindings,
                            (SELECT count(*) FROM public.google_calendar_sync_state
                             WHERE tenant_id = :tenant_id) AS calendar_sync,
                            (SELECT count(*) FROM public.watch_subscriptions
                             WHERE tenant_id = :tenant_id) AS watches,
                            (SELECT count(*) FROM public.event_change_deliveries
                             WHERE tenant_id = :tenant_id) AS deliveries,
                            (SELECT count(*) FROM public.event_change_calendar_repairs
                             WHERE tenant_id = :tenant_id) AS repairs,
                            (SELECT count(*) FROM public.tenant_ranking_profiles
                             WHERE tenant_id = :tenant_id) AS profiles,
                            (SELECT count(*) FROM public.tenant_ranking_feedback_receipts
                             WHERE tenant_id = :tenant_id) AS feedback,
                            (SELECT count(*) FROM public.tenant_source_consents
                             WHERE tenant_id = :tenant_id) AS consents,
                            (SELECT count(*) FROM public.request_start_outbox
                             WHERE tenant_id = :tenant_id) AS request_starts,
                            (SELECT count(*) FROM public.request_outcome_links
                             WHERE tenant_id = :tenant_id) AS request_outcomes,
                            (SELECT count(*) FROM public.tenant_profiles
                             WHERE tenant_id = :tenant_id) AS tenant_profiles,
                            (SELECT count(*) FROM public.tenant_profile_avatars
                             WHERE tenant_id = :tenant_id) AS profile_avatars,
                            (SELECT count(*) FROM public.tenant_roles
                             WHERE tenant_id = :tenant_id) AS tenant_roles,
                            (SELECT count(*) FROM public.tenant_api_keys
                             WHERE tenant_id = :tenant_id) AS api_keys"""
                    ),
                    {"tenant_id": erased.tenant_id},
                )
            ).one()
            assert set(erased_counts) == {0}

            audit = (
                await connection.execute(
                    text(
                        """SELECT audit_key, workflow_id, policy_decision, consent_ref,
                                  pii_shredded_at
                           FROM public.registration_action_audit
                           WHERE tenant_id = :tenant_id"""
                    ),
                    {"tenant_id": erased.tenant_id},
                )
            ).one()
            assert audit.audit_key.startswith("audit-erased-")
            assert audit.workflow_id == workflow_id
            assert audit.policy_decision == "allowed"
            assert audit.consent_ref is None
            assert audit.pii_shredded_at is not None

            survivor_rows = (
                await connection.execute(
                    text(
                        """SELECT
                            (SELECT count(*) FROM public.event_requests
                             WHERE tenant_id = :tenant_id
                               AND request_id = :request_id) AS event_requests,
                            (SELECT count(*) FROM public.tenant_profiles
                             WHERE tenant_id = :tenant_id) AS tenant_profiles,
                            (SELECT count(*) FROM public.tenant_profile_avatars
                             WHERE tenant_id = :tenant_id) AS profile_avatars,
                            (SELECT count(*) FROM public.tenant_roles
                             WHERE tenant_id = :tenant_id) AS tenant_roles,
                            (SELECT count(*) FROM public.tenant_api_keys
                             WHERE tenant_id = :tenant_id) AS api_keys"""
                    ),
                    {
                        "tenant_id": survivor.tenant_id,
                        "request_id": survivor_request_id,
                    },
                )
            ).one()
            assert tuple(survivor_rows) == (1, 1, 1, 1, 1)

        # The opaque tombstone makes final and begin ACK-loss replays exact even after identity
        # deletion, and prevents accidental re-provisioning under the erased tenant id.
        assert await repository.finalize(erased.tenant_id, erasure_request_id) == completed
        assert await repository.begin(erased.tenant_id, erasure_request_id) == completed
        with pytest.raises(Exception, match="fenced for erasure"):
            await tenant_repository.add(
                Tenant(
                    tenant_id=erased.tenant_id,
                    oidc_subject=f"oidc|replacement-{uuid4()}",
                    notify_email="replacement@example.test",
                    relay_inbox=f"replacement-{uuid4().hex}@u.example.test",
                )
            )
    finally:
        await owner.dispose()


@pytest.mark.parametrize(
    "case",
    _ACCOUNT_SATELLITE_CASES,
    ids=lambda case: case.table,
)
async def test_account_satellite_writes_obey_erasure_fence(
    db: None,
    case: _AccountSatelliteCase,
) -> None:
    """Reject post-begin mutation/recreation while preserving delete and tenant isolation."""
    erased = _tenant(f"{case.table}-fenced")
    survivor = _tenant(f"{case.table}-survivor")
    tenants = PostgresTenantRepository()
    await tenants.add(erased)
    await tenants.add(survivor)
    owner = create_async_engine(_owner_url(), pool_pre_ping=True)
    try:
        async with owner.begin() as connection:
            await connection.execute(
                text(case.insert_sql),
                _account_satellite_params(erased.tenant_id, "fenced-seed"),
            )
            await connection.execute(
                text(case.insert_sql),
                _account_satellite_params(survivor.tenant_id, "survivor-seed"),
            )

        await PostgresAccountErasureRepository().begin(erased.tenant_id, uuid4())

        await _assert_fenced_write(
            owner,
            case=case,
            operation="update",
            statement=case.update_sql,
            parameters=_account_satellite_params(
                erased.tenant_id,
                "fenced-update",
                role="operator",
            ),
        )

        # Deletes are intentionally legal after the fence: finalization relies on these same
        # trigger semantics when ON DELETE CASCADE removes tenant satellites.
        async with owner.begin() as connection:
            deleted_tenant_id = (
                await connection.execute(
                    text(case.delete_sql),
                    {"tenant_id": erased.tenant_id},
                )
            ).scalar_one()
        assert deleted_tenant_id == erased.tenant_id

        # Exercise the dangerous ordering directly: once purge removed the old row, a late writer
        # still cannot recreate tenant data behind it.
        await _assert_fenced_write(
            owner,
            case=case,
            operation="insert",
            statement=case.insert_sql,
            parameters=_account_satellite_params(erased.tenant_id, "fenced-recreate"),
        )

        # The trigger is scoped to the erased identity, not a global maintenance switch.
        async with owner.begin() as connection:
            updated_tenant_id = (
                await connection.execute(
                    text(case.update_sql),
                    _account_satellite_params(
                        survivor.tenant_id,
                        "survivor-update",
                        role="operator",
                    ),
                )
            ).scalar_one()
        assert updated_tenant_id == survivor.tenant_id
    finally:
        await owner.dispose()


async def test_erasure_tombstone_is_rls_scoped(db: None) -> None:
    first, second = _tenant("rls-first"), _tenant("rls-second")
    tenants = PostgresTenantRepository()
    await tenants.add(first)
    await tenants.add(second)
    repository = PostgresAccountErasureRepository()
    request_id = uuid4()

    await repository.begin(first.tenant_id, request_id)

    assert (await repository.get(first.tenant_id)).request_id == request_id  # type: ignore[union-attr]
    assert await repository.get(second.tenant_id) is None


async def test_erasure_begin_waits_for_inflight_notification_and_revokes_all_future_sends(
    db: None,
) -> None:
    """The production relay orders a cancellation-resistant send strictly before erasure begin."""
    tenant = _tenant("notification-drain")
    await PostgresTenantRepository().add(tenant)
    owner = create_async_engine(_owner_url(), pool_pre_ping=True)
    notifier = _BlockingNotifier()
    relay_task: asyncio.Task[RelayStats] | None = None
    begin_task: asyncio.Task[AccountErasureSnapshot] | None = None
    try:
        outbox_id = await _insert_notification_outbox(owner, tenant.tenant_id)
        outbox = _TenantScopedOutboxRepository(tenant.tenant_id, outbox_id)
        authority = PostgresTenantEffectAuthority()
        relay = OutboxRelay(
            outbox,
            notifier,
            DevelopmentNotificationSecretProtector(),
            lease_seconds=60,
            tenant_effect_authority=authority,
            tenant_effect_timeout_seconds=5.0,
        )

        relay_task = asyncio.create_task(relay.relay_once(limit=100))
        await asyncio.wait_for(notifier.entered.wait(), timeout=2)
        assert notifier.calls == 1
        erasure = PostgresAccountErasureRepository()
        begin_task = asyncio.create_task(erasure.begin(tenant.tenant_id, uuid4()))
        await asyncio.sleep(0.05)
        assert begin_task.done() is False

        # Shutdown cancellation must not release the authority while the accepted provider call
        # is still running; account-erasure begin therefore remains blocked on the same lock.
        relay_task.cancel()
        await asyncio.sleep(0.05)
        assert relay_task.done() is False
        assert begin_task.done() is False

        notifier.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(relay_task, timeout=2)
        assert notifier.completed is True
        started = await asyncio.wait_for(begin_task, timeout=2)
        assert started.status is AccountErasureStatus.ERASING

        # Begin commits only after the provider context exits and removes both exact authorities.
        async with owner.connect() as connection:
            remaining = (
                await connection.execute(
                    text(
                        """SELECT
                               (SELECT count(*) FROM public.outbox
                                WHERE tenant_id = :tenant_id),
                               (SELECT count(*) FROM public.notification_ledger
                                WHERE tenant_id = :tenant_id)"""
                    ),
                    {"tenant_id": tenant.tenant_id},
                )
            ).one()
        assert tuple(remaining) == (0, 0)

        # The actual relay has no queue authority after begin, and the shared effect authority
        # independently refuses any attempted post-fence notification mutation.
        assert await relay.relay_once(limit=100) == RelayStats()
        assert notifier.calls == 1
        attempted = False

        async def forbidden_send() -> None:
            nonlocal attempted
            attempted = True

        with pytest.raises(TenantEffectFencedError, match="fenced"):
            await authority.run(
                TenantEffectRequest(
                    tenant_id=tenant.tenant_id,
                    kind=TenantEffectKind.NOTIFICATION,
                    timeout_seconds=1.0,
                ),
                forbidden_send,
            )
        assert attempted is False
    finally:
        # Never strand the provider or the erasure transaction if an assertion above fails.
        try:
            await _release_and_drain_notification_race_tasks(notifier, relay_task, begin_task)
        finally:
            await owner.dispose()


async def test_write_fence_inventory_covers_every_tenant_bearing_relation(db: None) -> None:
    """A new tenant table must make this test fail until erasure fencing is extended."""
    owner = create_async_engine(_owner_url(), pool_pre_ping=True)
    try:
        async with owner.connect() as connection:
            tenant_tables = set(
                (
                    await connection.execute(
                        text(
                            """SELECT table_name
                               FROM information_schema.columns
                               WHERE table_schema = 'public'
                                 AND column_name = 'tenant_id'"""
                        )
                    )
                ).scalars()
            )
            fenced_tables = set(
                (
                    await connection.execute(
                        text(
                            """SELECT event_object_table
                               FROM information_schema.triggers
                               WHERE trigger_schema = 'public'
                                 AND trigger_name = 'tr_account_erasure_write_fence'"""
                        )
                    )
                ).scalars()
            )
    finally:
        await owner.dispose()

    assert tenant_tables == fenced_tables | {
        "account_erasure_requests",
        "account_erasure_calendar_targets",
        "account_erasure_workflow_targets",
    }
