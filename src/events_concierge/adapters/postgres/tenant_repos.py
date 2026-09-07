"""RLS-scoped tenant repositories and guarded lifecycle persistence.

Tenant reads always open a session bound to the tenant context so RLS filters and fails closed
without it. New identity rows use PostgreSQL's narrow idempotent provisioning capability rather
than direct app-role DML; lifecycle transitions remain guarded and atomic (FR-1.2--FR-1.4,
ADR-007).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.credentials import Tenant
from ...domain.enums import (
    HandoffCompletionOutcome,
    HandoffReminderKind,
    HandoffReminderStatus,
    HandoffState,
    Lane,
    LifecycleState,
    Source,
)
from ...domain.ids import registration_workflow_id
from ...domain.lifecycle import (
    HandoffCompletionReceipt,
    HandoffCompletionTarget,
    HandoffReminderResult,
    HandoffTask,
    IllegalTransitionError,
    Lifecycle,
)
from ...domain.request import EventRequest, RequestConstraints, TimeWindow
from ...infra.db import system_session_scope, tenant_session_scope
from ...ports.repositories import (
    NotificationClaim,
    OutboxQueueSnapshot,
    OutboxRecord,
    RequestStartRecord,
)


class _OutboxRow(Protocol):
    """Named fields returned by the relay's explicit ``RETURNING`` projection."""

    id: int
    tenant_id: UUID
    topic: str
    payload: dict[str, object] | str
    attempt_count: int
    lease_token: str


class _OutboxQueueSnapshotRow(Protocol):
    """Named aggregate projection used only for ADR-009 queue observability."""

    pending: int
    ready: int
    leased: int
    oldest_ready_at: datetime | None


class _RequestStartRow(Protocol):
    """Named fields returned by the request-start relay's lease projection."""

    request_id: UUID
    tenant_id: UUID
    attempt_count: int
    lease_token: str


class _RequestStartLeaseRow(Protocol):
    """The exact opaque lease facts checked after acquiring one start-outbox row lock."""

    started_at: datetime | None
    lease_token: str | None
    lease_expires_at: datetime | None


async def _guarded_transition(
    session: AsyncSession,
    lifecycle: Lifecycle,
    to_state: LifecycleState,
    transition_id: str,
    outbox_payload: dict[str, object],
    *,
    handoff_task: HandoffTask | None = None,
) -> bool:
    """Call the ADR-007 database guard; it owns lifecycle, ledger, and outbox consistency."""
    payload = {
        **outbox_payload,
        "workflow_id": lifecycle.workflow_id,
        "transition_id": transition_id,
        "lifecycle_state": to_state.value,
    }
    handoff_payload = (
        {
            "task_id": handoff_task.task_id,
            "tenant_id": str(handoff_task.tenant_id),
            "workflow_id": handoff_task.workflow_id,
            "canonical_event_id": str(handoff_task.canonical_event_id),
            "reason": handoff_task.reason.value,
            "deep_link": handoff_task.deep_link,
            "event_summary": handoff_task.event_summary,
            "ttl_expires_at": handoff_task.ttl_expires_at.isoformat(),
            "state": handoff_task.state.value,
            "metadata": handoff_task.metadata,
            "expiry_transition_id": handoff_task.resolved_expiry_transition_id(),
        }
        if handoff_task is not None
        else None
    )
    result = await session.execute(
        text(
            """SELECT public.fn_transition(
                   :lifecycle_id,
                   :expected_state,
                   :to_state,
                   :transition_id,
                   :lane,
                   :conflict_warning,
                   CAST(:payload AS jsonb),
                   CAST(:handoff_task AS jsonb)
               ) AS applied"""
        ),
        {
            "lifecycle_id": lifecycle.lifecycle_id,
            "expected_state": lifecycle.state.value,
            "to_state": to_state.value,
            "transition_id": transition_id,
            "lane": lifecycle.lane.value if lifecycle.lane is not None else None,
            "conflict_warning": lifecycle.conflict_warning,
            "payload": json.dumps(payload),
            "handoff_task": json.dumps(handoff_payload) if handoff_payload is not None else None,
        },
    )
    applied = bool(result.scalar_one())
    if handoff_task is not None and handoff_task.completion_token is not None:
        await _attach_handoff_completion_token(session, handoff_task)
    return applied


async def _insert_handoff_task_and_enqueue_expiry(session: AsyncSession, task: HandoffTask) -> bool:
    """Persist one RLS task and its opaque TTL repair record in the caller's transaction.

    The SECURITY DEFINER boundary validates the deterministic lifecycle identity, supersedes an
    older active task for the same lifecycle, and lets its task-insert trigger enqueue the opaque
    record.  It becomes claimable five minutes after the Temporal-owned TTL (FR-6.6, ADR-007).
    """
    task_payload = {
        "task_id": task.task_id,
        "tenant_id": str(task.tenant_id),
        "workflow_id": task.workflow_id,
        "canonical_event_id": str(task.canonical_event_id),
        "reason": task.reason.value,
        "deep_link": task.deep_link,
        "event_summary": task.event_summary,
        "ttl_expires_at": task.ttl_expires_at.isoformat(),
        "state": task.state.value,
        "metadata": task.metadata,
        "expiry_transition_id": task.resolved_expiry_transition_id(),
    }
    result = await session.execute(
        text("""SELECT public.fn_create_handoff_task(CAST(:task AS jsonb)) AS inserted"""),
        {"task": json.dumps(task_payload)},
    )
    inserted = bool(result.scalar_one())
    if task.completion_token is not None:
        await _attach_handoff_completion_token(session, task)
    return inserted


async def _attach_handoff_completion_token(session: AsyncSession, task: HandoffTask) -> None:
    """Persist only the capability digest in the same transaction that creates its task."""
    token = task.completion_token
    if token is None:
        return
    await session.execute(
        text(
            """SELECT public.fn_attach_handoff_completion_token(
                   :task_id,
                   :token_hash
               )"""
        ),
        {
            "task_id": task.task_id,
            "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        },
    )


class PostgresTenantRepository:
    """Read tenant identity under RLS and provision it through its sole narrow capability."""

    async def get(self, tenant_id: UUID) -> Tenant | None:
        """Read only the tenant identity visible to its established RLS context."""
        async with tenant_session_scope(tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        """SELECT tenant_id, oidc_subject, notify_email, relay_inbox
                           FROM public.tenants
                           WHERE tenant_id = :tenant_id"""
                    ),
                    {"tenant_id": tenant_id},
                )
            ).first()
        if row is None:
            return None
        return Tenant(row.tenant_id, row.oidc_subject, row.notify_email, row.relay_inbox)

    async def add(self, tenant: Tenant) -> None:
        """Provision one immutable identity through the app-role's idempotent SQL capability."""
        async with system_session_scope() as s:
            provisioned = (
                await s.execute(
                    text(
                        """SELECT public.fn_provision_tenant(
                               :tenant_id,
                               :oidc_subject,
                               :notify_email,
                               :relay_inbox
                           ) AS provisioned"""
                    ),
                    {
                        "tenant_id": tenant.tenant_id,
                        "oidc_subject": tenant.oidc_subject,
                        "notify_email": tenant.notify_email,
                        "relay_inbox": tenant.relay_inbox,
                    },
                )
            ).scalar_one()
            if not isinstance(provisioned, bool):
                raise RuntimeError("tenant provisioning capability returned an invalid result")


class PostgresRequestRepository:
    async def add(self, request: EventRequest) -> None:
        async with tenant_session_scope(request.tenant_id) as s:
            await s.execute(
                text(
                    """INSERT INTO event_requests (request_id, tenant_id, raw_text, constraints)
                       VALUES (:rid, :tid, :raw, :cons)
                       ON CONFLICT (request_id) DO NOTHING"""
                ),
                {
                    "rid": request.request_id,
                    "tid": request.tenant_id,
                    "raw": request.raw_text,
                    "cons": json.dumps(_constraints_to_json(request.constraints)),
                },
            )

    async def add_and_enqueue_start(self, request: EventRequest, dedup_key: str) -> None:
        """Commit the RLS-protected request and opaque start instruction in one transaction.

        The cross-tenant relay row holds no raw text; it is safe for the worker to lease globally
        and then re-read the actual request under this tenant's RLS context.  A duplicate key must
        bind the exact same request identity or the transaction fails closed (ADR-003, AC-48).
        """
        async with tenant_session_scope(request.tenant_id) as s:
            await s.execute(
                text(
                    """INSERT INTO event_requests (request_id, tenant_id, raw_text, constraints)
                       VALUES (:rid, :tid, :raw, :cons)
                       ON CONFLICT (request_id) DO NOTHING"""
                ),
                {
                    "rid": request.request_id,
                    "tid": request.tenant_id,
                    "raw": request.raw_text,
                    "cons": json.dumps(_constraints_to_json(request.constraints)),
                },
            )
            inserted = await s.execute(
                text(
                    """INSERT INTO request_start_outbox (request_id, tenant_id, dedup_key)
                       VALUES (:rid, :tid, :dedup_key)
                       ON CONFLICT (dedup_key) DO NOTHING
                       RETURNING request_id, tenant_id"""
                ),
                {
                    "rid": request.request_id,
                    "tid": request.tenant_id,
                    "dedup_key": dedup_key,
                },
            )
            if inserted.first() is not None:
                return
            existing = (
                await s.execute(
                    text(
                        """SELECT request_id, tenant_id FROM request_start_outbox
                           WHERE dedup_key = :dedup_key"""
                    ),
                    {"dedup_key": dedup_key},
                )
            ).one_or_none()
            if (
                existing is None
                or existing.request_id != request.request_id
                or existing.tenant_id != request.tenant_id
            ):
                raise RuntimeError("request start dedup key is bound to a different request")

    async def get(self, tenant_id: UUID, request_id: UUID) -> EventRequest | None:
        """Read a request only through the caller's RLS tenant context (FR-1.3/1.4)."""
        async with tenant_session_scope(tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        """SELECT * FROM event_requests
                           WHERE tenant_id = :tenant_id AND request_id = :request_id"""
                    ),
                    {"tenant_id": tenant_id, "request_id": request_id},
                )
            ).first()
        if row is None:
            return None
        return EventRequest(
            request_id=row.request_id,
            tenant_id=row.tenant_id,
            raw_text=row.raw_text,
            constraints=_constraints_from_json(row.constraints),
        )

    async def register_workflow_targets(
        self,
        tenant_id: UUID,
        workflow_ids: tuple[str, ...],
    ) -> None:
        """Commit the full child set before a parent can emit its first start-child command."""
        if not workflow_ids:
            return
        expected_prefix = f"{tenant_id}:"
        for workflow_id in workflow_ids:
            if not workflow_id.startswith(expected_prefix):
                raise ValueError("registered child workflow must use the tenant identity prefix")
            try:
                canonical_event_id = UUID(workflow_id.removeprefix(expected_prefix))
            except ValueError as error:
                raise ValueError("registered child workflow identity is malformed") from error
            if registration_workflow_id(tenant_id, canonical_event_id) != workflow_id:
                raise ValueError("registered child workflow identity is not deterministic")
        async with tenant_session_scope(tenant_id) as session:
            await session.execute(
                text(
                    """INSERT INTO public.tenant_workflow_registry (tenant_id, workflow_id)
                       SELECT :tenant_id, target.workflow_id
                       FROM unnest(CAST(:workflow_ids AS text[])) AS target(workflow_id)
                       ON CONFLICT (tenant_id, workflow_id) DO NOTHING"""
                ),
                {"tenant_id": tenant_id, "workflow_ids": list(workflow_ids)},
            )

    async def claim_start(
        self, tenant_id: UUID, request_id: UUID, lease_seconds: int
    ) -> RequestStartRecord | None:
        """Lease one exact start instruction without racing the background starter worker."""
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        lease_token = uuid4().hex
        async with system_session_scope() as s:
            row = (
                await s.execute(
                    text(
                        """UPDATE request_start_outbox
                           SET lease_token = :lease_token,
                               lease_expires_at = now() + (:lease_seconds * INTERVAL '1 second')
                           WHERE request_id = :request_id
                             AND tenant_id = :tenant_id
                             AND started_at IS NULL
                             AND next_attempt_at <= now()
                             AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                           RETURNING request_id, tenant_id, attempt_count, lease_token"""
                    ),
                    {
                        "request_id": request_id,
                        "tenant_id": tenant_id,
                        "lease_token": lease_token,
                        "lease_seconds": lease_seconds,
                    },
                )
            ).first()
        return self._start_record_from_row(cast(_RequestStartRow, row)) if row is not None else None

    async def claim_start_batch(self, limit: int, lease_seconds: int) -> list[RequestStartRecord]:
        """Lease ready starts globally with SKIP LOCKED; the queue itself contains opaque IDs only."""
        if limit < 1:
            raise ValueError("limit must be positive")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        lease_token = uuid4().hex
        async with system_session_scope() as s:
            rows = (
                await s.execute(
                    text(
                        """WITH candidates AS (
                               SELECT request_id FROM request_start_outbox
                               WHERE started_at IS NULL
                                 AND next_attempt_at <= now()
                                 AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                               ORDER BY request_id
                               FOR UPDATE SKIP LOCKED
                               LIMIT :limit
                           )
                           UPDATE request_start_outbox AS start_outbox
                           SET lease_token = :lease_token,
                               lease_expires_at = now() + (:lease_seconds * INTERVAL '1 second')
                           FROM candidates
                           WHERE start_outbox.request_id = candidates.request_id
                           RETURNING start_outbox.request_id, start_outbox.tenant_id,
                                     start_outbox.attempt_count, start_outbox.lease_token"""
                    ),
                    {
                        "limit": limit,
                        "lease_token": lease_token,
                        "lease_seconds": lease_seconds,
                    },
                )
            ).all()
        return [self._start_record_from_row(cast(_RequestStartRow, row)) for row in rows]

    async def has_live_start_lease(self, record: RequestStartRecord) -> bool:
        """Fence an observed stale start lease before Temporal egress with the database clock.

        ``request_start_outbox`` is ADR-003's intentionally opaque direct-DML control queue. The
        app role already has its claim/ack/retry row surface, so this uses the existing system
        transaction rather than adding a redundant SECURITY DEFINER capability. The second query
        intentionally reads ``clock_timestamp()`` only after the exact row lock can no longer
        wait, avoiding a stale pre-wait timestamp (FR-6.8, NFR-8, ADR-003).
        """
        async with tenant_session_scope(record.tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT started_at, lease_token, lease_expires_at
                        FROM request_start_outbox
                        WHERE request_id = :request_id
                          AND tenant_id = :tenant_id
                        FOR UPDATE
                        """
                    ),
                    {
                        "request_id": record.request_id,
                        "tenant_id": record.tenant_id,
                    },
                )
            ).one_or_none()
            if row is None:
                return False
            lease = cast(_RequestStartLeaseRow, row)
            if (
                lease.started_at is not None
                or lease.lease_token != record.lease_token
                or lease.lease_expires_at is None
            ):
                return False
            live = (
                await session.execute(
                    text("SELECT :lease_expires_at > pg_catalog.clock_timestamp() AS live"),
                    {"lease_expires_at": lease.lease_expires_at},
                )
            ).scalar_one()
            permitted = (
                await session.execute(
                    text(
                        """SELECT
                               NOT EXISTS (
                                   SELECT 1 FROM public.account_erasure_requests
                                   WHERE tenant_id = :tenant_id
                               )
                               AND NOT COALESCE((
                                   SELECT kill_switch FROM public.tenant_policy_control
                                   WHERE tenant_id = :tenant_id
                               ), false) AS permitted"""
                    ),
                    {"tenant_id": record.tenant_id},
                )
            ).scalar_one()
        return bool(live and permitted)

    async def account_erasure_fenced(self, tenant_id: UUID) -> bool:
        """Pair a completed Temporal start with either exact cancel or begin's later snapshot."""
        async with tenant_session_scope(tenant_id) as session:
            return bool(
                (
                    await session.execute(
                        text(
                            """SELECT EXISTS (
                                   SELECT 1 FROM public.account_erasure_requests
                                   WHERE tenant_id = :tenant_id
                               )"""
                        ),
                        {"tenant_id": tenant_id},
                    )
                ).scalar_one()
            )

    @asynccontextmanager
    async def request_start_guard(self, record: RequestStartRecord) -> AsyncIterator[bool]:
        """Order one Temporal parent start wholly before or after account-erasure begin."""
        async with tenant_session_scope(record.tenant_id) as session:
            await session.execute(
                text(
                    """SELECT pg_advisory_xact_lock(
                           hashtextextended(
                               'account-erasure:' || CAST(:tenant_id AS text), 0
                           )
                       )"""
                ),
                {"tenant_id": record.tenant_id},
            )
            permitted = (
                await session.execute(
                    text(
                        """SELECT EXISTS (
                               SELECT 1
                               FROM public.request_start_outbox AS queue
                               WHERE queue.tenant_id = :tenant_id
                                 AND queue.request_id = :request_id
                                 AND queue.lease_token = :lease_token
                                 AND queue.lease_expires_at > clock_timestamp()
                                 AND queue.started_at IS NULL
                                 AND NOT EXISTS (
                                     SELECT 1
                                     FROM public.account_erasure_requests AS erasure
                                     WHERE erasure.tenant_id = queue.tenant_id
                                 )
                                 AND NOT COALESCE((
                                     SELECT control.kill_switch
                                     FROM public.tenant_policy_control AS control
                                     WHERE control.tenant_id = queue.tenant_id
                                 ), false)
                           )"""
                    ),
                    {
                        "tenant_id": record.tenant_id,
                        "request_id": record.request_id,
                        "lease_token": record.lease_token,
                    },
                )
            ).scalar_one()
            yield bool(permitted)

    async def mark_start_started(self, record: RequestStartRecord) -> bool:
        """Acknowledge a start and request state together, only for the active relay lease."""
        async with tenant_session_scope(record.tenant_id) as s:
            marked = (
                await s.execute(
                    text(
                        """UPDATE request_start_outbox
                           SET started_at = now(), lease_token = NULL, lease_expires_at = NULL,
                               last_error = NULL
                           WHERE request_id = :request_id
                             AND tenant_id = :tenant_id
                             AND lease_token = :lease_token
                             AND started_at IS NULL
                             AND lease_expires_at > pg_catalog.clock_timestamp()
                           RETURNING request_id"""
                    ),
                    {
                        "request_id": record.request_id,
                        "tenant_id": record.tenant_id,
                        "lease_token": record.lease_token,
                    },
                )
            ).first()
            if marked is None:
                return False
            persisted = (
                await s.execute(
                    text(
                        """UPDATE event_requests
                           SET state = 'started'
                           WHERE request_id = :request_id AND tenant_id = :tenant_id
                           RETURNING request_id"""
                    ),
                    {"request_id": record.request_id, "tenant_id": record.tenant_id},
                )
            ).first()
            if persisted is None:
                raise RuntimeError("leased request start cannot read its RLS-protected request")
        return True

    async def reschedule_start(
        self, record: RequestStartRecord, *, retry_at: datetime, error: str
    ) -> bool:
        """Release a failed Temporal-start attempt without a terminal-drop path (ADR-003)."""
        async with system_session_scope() as s:
            result = await s.execute(
                text(
                    """UPDATE request_start_outbox
                       SET attempt_count = attempt_count + 1,
                           next_attempt_at = :retry_at,
                           last_error = :error,
                           lease_token = NULL,
                           lease_expires_at = NULL
                       WHERE request_id = :request_id
                         AND tenant_id = :tenant_id
                         AND lease_token = :lease_token
                         AND started_at IS NULL
                         AND lease_expires_at > pg_catalog.clock_timestamp()"""
                ),
                {
                    "request_id": record.request_id,
                    "tenant_id": record.tenant_id,
                    "lease_token": record.lease_token,
                    "retry_at": retry_at,
                    "error": error[:1000],
                },
            )
        return bool(getattr(result, "rowcount", 0))

    async def start_has_started(self, tenant_id: UUID, request_id: UUID) -> bool:
        """Return a tenant-filtered start acknowledgement without exposing another tenant's row."""
        async with system_session_scope() as s:
            row = (
                await s.execute(
                    text(
                        """SELECT started_at FROM request_start_outbox
                           WHERE request_id = :request_id AND tenant_id = :tenant_id"""
                    ),
                    {"request_id": request_id, "tenant_id": tenant_id},
                )
            ).first()
        return row is not None and row.started_at is not None

    async def mark_failed_no_candidate(
        self, tenant_id: UUID, request_id: UUID, transition_id: str
    ) -> bool:
        """Guard one request-level no-result terminal effect behind its SQL function.

        A request can exhaust discovery without ever minting a canonical event/lifecycle.  The
        SECURITY DEFINER function therefore owns the otherwise separate request-state, terminal
        ledger, and outbox transaction (FR-5.0/6.6, ADR-003/007).
        """
        async with tenant_session_scope(tenant_id) as s:
            result = await s.execute(
                text(
                    """SELECT public.fn_terminalize_request_no_candidate(
                           :request_id, :transition_id
                       ) AS applied"""
                ),
                {"request_id": request_id, "transition_id": transition_id},
            )
        return bool(result.scalar_one())

    @staticmethod
    def _start_record_from_row(row: _RequestStartRow) -> RequestStartRecord:
        """Normalize an explicit SQL projection into the port's opaque relay record."""
        return RequestStartRecord(
            request_id=row.request_id,
            tenant_id=row.tenant_id,
            attempt_count=int(row.attempt_count),
            lease_token=str(row.lease_token),
        )


class PostgresLifecycleRepository:
    async def get_or_create(
        self, tenant_id: UUID, canonical_event_id: UUID, workflow_id: str
    ) -> Lifecycle:
        """Open only the ADR-003 deterministic child identity for this tenant/event pair."""
        expected_workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
        if workflow_id != expected_workflow_id:
            raise ValueError(
                "lifecycle workflow id must equal the deterministic tenant:event identity"
            )
        async with tenant_session_scope(tenant_id) as s:
            await s.execute(
                text(
                    """INSERT INTO lifecycle (lifecycle_id, tenant_id, canonical_event_id, workflow_id)
                       VALUES (:lid, :tid, :cid, :wid)
                       ON CONFLICT (workflow_id) DO NOTHING"""
                ),
                {"lid": uuid4(), "tid": tenant_id, "cid": canonical_event_id, "wid": workflow_id},
            )
            row = (
                await s.execute(
                    text("SELECT * FROM lifecycle WHERE workflow_id = :wid"), {"wid": workflow_id}
                )
            ).one()
        return Lifecycle(
            lifecycle_id=row.lifecycle_id,
            tenant_id=row.tenant_id,
            canonical_event_id=row.canonical_event_id,
            workflow_id=row.workflow_id,
            state=LifecycleState(row.state),
            lane=Lane(row.lane) if row.lane else None,
            registration_source=(
                Source(row.registration_source) if row.registration_source else None
            ),
            conflict_warning=row.conflict_warning,
        )

    async def find_by_workflow_id(
        self, tenant_id: UUID, workflow_id: str
    ) -> Lifecycle | None:
        """Read the exact deterministic lifecycle even after it becomes terminal."""
        async with tenant_session_scope(tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        """SELECT * FROM lifecycle
                           WHERE tenant_id = :tenant_id
                             AND workflow_id = :workflow_id"""
                    ),
                    {"tenant_id": tenant_id, "workflow_id": workflow_id},
                )
            ).first()
        if row is None:
            return None
        return Lifecycle(
            lifecycle_id=row.lifecycle_id,
            tenant_id=row.tenant_id,
            canonical_event_id=row.canonical_event_id,
            workflow_id=row.workflow_id,
            state=LifecycleState(row.state),
            lane=Lane(row.lane) if row.lane else None,
            registration_source=(
                Source(row.registration_source) if row.registration_source else None
            ),
            conflict_warning=row.conflict_warning,
        )

    async def find_active(self, tenant_id: UUID, canonical_event_id: UUID) -> Lifecycle | None:
        """Find one RLS-visible non-terminal lifecycle without minting a workflow row (FR-8.8)."""
        async with tenant_session_scope(tenant_id) as s:
            row = (
                await s.execute(
                    text(
                        """SELECT * FROM lifecycle
                           WHERE tenant_id = :tenant_id
                             AND canonical_event_id = :canonical_event_id
                             AND state NOT IN ('completed', 'cancelled', 'expired', 'failed_no_candidate')
                           ORDER BY updated_at DESC
                           LIMIT 1"""
                    ),
                    {"tenant_id": tenant_id, "canonical_event_id": canonical_event_id},
                )
            ).first()
        if row is None:
            return None
        return Lifecycle(
            lifecycle_id=row.lifecycle_id,
            tenant_id=row.tenant_id,
            canonical_event_id=row.canonical_event_id,
            workflow_id=row.workflow_id,
            state=LifecycleState(row.state),
            lane=Lane(row.lane) if row.lane else None,
            registration_source=(
                Source(row.registration_source) if row.registration_source else None
            ),
            conflict_warning=row.conflict_warning,
        )

    async def transition(
        self,
        lifecycle: Lifecycle,
        to_state: LifecycleState,
        transition_id: str,
        outbox_payload: dict[str, object],
    ) -> None:
        if not lifecycle.can_transition(to_state):
            raise IllegalTransitionError(f"{lifecycle.state} -> {to_state} illegal")
        async with tenant_session_scope(lifecycle.tenant_id) as s:
            await _guarded_transition(s, lifecycle, to_state, transition_id, outbox_payload)
        lifecycle.state = to_state
        if to_state is LifecycleState.REGISTERED:
            source = outbox_payload.get("registration_source")
            if isinstance(source, str):
                lifecycle.registration_source = Source(source)


class PostgresHandoffRepository:
    async def create(self, task: HandoffTask) -> None:
        async with tenant_session_scope(task.tenant_id) as s:
            await _insert_handoff_task_and_enqueue_expiry(s, task)

    async def create_and_transition(
        self,
        task: HandoffTask,
        lifecycle: Lifecycle,
        to_state: LifecycleState,
        transition_id: str,
        outbox_payload: dict[str, object],
    ) -> None:
        """Commit a handoff task with its guarded lifecycle/outbox transition (ADR-007)."""
        if task.tenant_id != lifecycle.tenant_id:
            raise ValueError("handoff task tenant does not match lifecycle tenant")
        if task.workflow_id != lifecycle.workflow_id:
            raise ValueError("handoff task workflow does not match lifecycle workflow")
        if task.canonical_event_id != lifecycle.canonical_event_id:
            raise ValueError("handoff task event does not match lifecycle event")
        if not lifecycle.can_transition(to_state):
            raise IllegalTransitionError(f"{lifecycle.state} -> {to_state} illegal")
        async with tenant_session_scope(task.tenant_id) as session:
            await _guarded_transition(
                session,
                lifecycle,
                to_state,
                transition_id,
                outbox_payload,
                handoff_task=task,
            )
        lifecycle.state = to_state

    async def create_calendar_recovery(
        self, task: HandoffTask, outbox_payload: dict[str, object]
    ) -> None:
        """Insert the recovery task and one relay record in the same transaction (ADR-007)."""
        async with tenant_session_scope(task.tenant_id) as s:
            if not await _insert_handoff_task_and_enqueue_expiry(s, task):
                return
            await s.execute(
                text(
                    """INSERT INTO outbox (tenant_id, topic, payload)
                       VALUES (:tid, 'calendar_recovery_required', :payload)"""
                ),
                {"tid": task.tenant_id, "payload": json.dumps(outbox_payload)},
            )

    async def create_withdrawal_handoff(
        self, task: HandoffTask, outbox_payload: dict[str, object]
    ) -> None:
        """Persist one manual withdrawal task and one relay row without terminalizing the lifecycle.

        The task id is workflow-minted.  ``ON CONFLICT`` makes a crash between the task and caller
        acknowledgement converge to one task/outbox pair while the lifecycle remains ``withdrawing``
        until a human confirms the provider-side result (FR-8.8, ADR-007).
        """
        async with tenant_session_scope(task.tenant_id) as s:
            if not await _insert_handoff_task_and_enqueue_expiry(s, task):
                return
            await s.execute(
                text(
                    """INSERT INTO outbox (tenant_id, topic, payload)
                       VALUES (:tid, 'withdrawal_handoff_required', :payload)"""
                ),
                {"tid": task.tenant_id, "payload": json.dumps(outbox_payload)},
            )

    async def get(self, tenant_id: UUID, task_id: str) -> HandoffTask | None:
        """Read only the caller's task; an empty RLS context must never masquerade as global access."""
        async with tenant_session_scope(tenant_id) as s:
            row = (
                await s.execute(
                    text("SELECT * FROM handoff_tasks WHERE task_id = :id"), {"id": task_id}
                )
            ).first()
        if row is None:
            return None
        from ...domain.enums import HandoffReason

        return HandoffTask(
            task_id=row.task_id,
            tenant_id=row.tenant_id,
            workflow_id=row.workflow_id,
            canonical_event_id=row.canonical_event_id,
            reason=HandoffReason(row.reason),
            deep_link=row.deep_link,
            event_summary=row.event_summary,
            ttl_expires_at=row.ttl_expires_at,
            state=HandoffState(row.state),
            metadata=dict(row.metadata),
            expiry_transition_id=row.expiry_transition_id,
            created_at=row.created_at,
        )

    async def resolve_completion_token(self, token: str) -> HandoffCompletionTarget | None:
        """Resolve one digest through the sole cross-tenant capability lookup.

        The raw capability never reaches PostgreSQL. The SECURITY DEFINER function returns only
        opaque routing identities and a closed status, never task text, URLs, metadata, or contact
        information (FR-1.3, FR-6.3, FR-16).
        """
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        async with system_session_scope() as session:
            row = (
                await session.execute(
                    text(
                        """SELECT *
                           FROM public.fn_resolve_handoff_completion_token(:token_hash)"""
                    ),
                    {"token_hash": token_hash},
                )
            ).first()
        if row is None:
            return None
        return HandoffCompletionTarget(
            task_id=str(row.task_id),
            tenant_id=row.tenant_id,
            workflow_id=str(row.workflow_id),
            canonical_event_id=row.canonical_event_id,
            status=str(row.capability_status),
        )

    async def get_completion_attempt(
        self,
        tenant_id: UUID,
        task_id: str,
        completion_id: str,
    ) -> HandoffCompletionReceipt | None:
        """Recover an exact completion activity result through ordinary tenant RLS."""
        async with tenant_session_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        """SELECT completion_id, outcome, detail,
                                  registration_source, conflict_warning
                           FROM public.handoff_completion_attempts
                           WHERE tenant_id = :tenant_id
                             AND task_id = :task_id
                             AND completion_id = :completion_id"""
                    ),
                    {
                        "tenant_id": tenant_id,
                        "task_id": task_id,
                        "completion_id": completion_id,
                    },
                )
            ).first()
        if row is None:
            return None
        return HandoffCompletionReceipt(
            completion_id=str(row.completion_id),
            outcome=HandoffCompletionOutcome(str(row.outcome)),
            detail=str(row.detail),
            registration_source=(
                Source(str(row.registration_source))
                if row.registration_source is not None
                else None
            ),
            conflict_warning=(
                bool(row.conflict_warning) if row.conflict_warning is not None else None
            ),
        )

    async def complete_verified(
        self,
        task: HandoffTask,
        lifecycle: Lifecycle,
        *,
        transition_id: str,
        completion_id: str,
        registration_source: Source,
        conflict_warning: bool,
        outbox_payload: dict[str, object],
    ) -> bool:
        """Atomically consume one capability and advance its matching handoff lifecycle."""
        if task.tenant_id != lifecycle.tenant_id:
            raise ValueError("handoff completion tenant does not match lifecycle tenant")
        if task.workflow_id != lifecycle.workflow_id:
            raise ValueError("handoff completion workflow does not match lifecycle workflow")
        if task.canonical_event_id != lifecycle.canonical_event_id:
            raise ValueError("handoff completion event does not match lifecycle event")
        async with tenant_session_scope(task.tenant_id) as session:
            applied = bool(
                (
                    await session.execute(
                        text(
                            """SELECT public.fn_complete_verified_handoff(
                                   :task_id,
                                   :lifecycle_id,
                                   :transition_id,
                                   :completion_id,
                                   :registration_source,
                                   :conflict_warning,
                                   CAST(:payload AS jsonb)
                               ) AS applied"""
                        ),
                        {
                            "task_id": task.task_id,
                            "lifecycle_id": lifecycle.lifecycle_id,
                            "transition_id": transition_id,
                            "completion_id": completion_id,
                            "registration_source": registration_source.value,
                            "conflict_warning": conflict_warning,
                            "payload": json.dumps(outbox_payload),
                        },
                    )
                ).scalar_one()
            )
        lifecycle.state = LifecycleState.REGISTERED
        lifecycle.lane = Lane.HANDOFF
        lifecycle.registration_source = registration_source
        lifecycle.conflict_warning = conflict_warning
        task.state = HandoffState.COMPLETED
        return applied

    async def record_completion_review(
        self,
        task: HandoffTask,
        *,
        completion_id: str,
        detail: str,
    ) -> bool:
        """Consume one unverified self-report and emit a replay-safe review instruction."""
        async with tenant_session_scope(task.tenant_id) as session:
            return bool(
                (
                    await session.execute(
                        text(
                            """SELECT public.fn_record_handoff_completion_review(
                                   :task_id,
                                   :completion_id,
                                   :detail
                               ) AS inserted"""
                        ),
                        {
                            "task_id": task.task_id,
                            "completion_id": completion_id,
                            "detail": detail[:1000],
                        },
                    )
                ).scalar_one()
            )

    async def enqueue_reminder(
        self,
        tenant_id: UUID,
        task_id: str,
        workflow_id: str,
        canonical_event_id: UUID,
        expiry_transition_id: str,
        reminder_kind: HandoffReminderKind,
        reminder_id: str,
    ) -> HandoffReminderResult:
        """Call the task-bound reminder guard under its RLS tenant context (ADR-007/009)."""
        async with tenant_session_scope(tenant_id) as session:
            result = await session.execute(
                text(
                    """SELECT public.fn_enqueue_handoff_reminder(
                           :task_id,
                           :workflow_id,
                           :canonical_event_id,
                           :expiry_transition_id,
                           :reminder_kind,
                           :reminder_id
                       ) AS status"""
                ),
                {
                    "task_id": task_id,
                    "workflow_id": workflow_id,
                    "canonical_event_id": canonical_event_id,
                    "expiry_transition_id": expiry_transition_id,
                    "reminder_kind": reminder_kind.value,
                    "reminder_id": reminder_id,
                },
            )
            status = HandoffReminderStatus(str(result.scalar_one()))
        return HandoffReminderResult(status)


class PostgresOutboxRepository:
    async def queue_snapshot(self) -> OutboxQueueSnapshot:
        """Measure the global relay queue with the exact ADR-009 claim eligibility predicate.

        The queue intentionally has no RLS policy: its rows are opaque cross-tenant control
        records, and this projection contains only aggregate counts/timestamps (FR-8.9).
        """
        async with system_session_scope() as s:
            row = (
                await s.execute(
                    text(
                        """SELECT
                               count(*) FILTER (
                                   WHERE delivered_at IS NULL AND failed_at IS NULL
                               ) AS pending,
                               count(*) FILTER (
                                   WHERE delivered_at IS NULL
                                     AND failed_at IS NULL
                                     AND next_attempt_at <= now()
                                     AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                               ) AS ready,
                               count(*) FILTER (
                                   WHERE delivered_at IS NULL
                                     AND failed_at IS NULL
                                     AND lease_expires_at > now()
                               ) AS leased,
                               min(created_at) FILTER (
                                   WHERE delivered_at IS NULL
                                     AND failed_at IS NULL
                                     AND next_attempt_at <= now()
                                     AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                               ) AS oldest_ready_at
                           FROM outbox"""
                    )
                )
            ).one()
        snapshot = cast(_OutboxQueueSnapshotRow, row)
        return OutboxQueueSnapshot(
            pending=int(snapshot.pending),
            ready=int(snapshot.ready),
            leased=int(snapshot.leased),
            oldest_ready_at=snapshot.oldest_ready_at,
        )

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[OutboxRecord]:
        """Atomically lease ready rows so concurrent relays cannot send the same row (ADR-009)."""
        lease_token = uuid4().hex
        async with system_session_scope() as s:
            rows = (
                await s.execute(
                    text(
                        """WITH candidates AS (
                               SELECT id FROM outbox
                               WHERE delivered_at IS NULL
                                 AND failed_at IS NULL
                                 AND next_attempt_at <= now()
                                 AND (lease_expires_at IS NULL OR lease_expires_at <= now())
                               ORDER BY id
                               FOR UPDATE SKIP LOCKED
                               LIMIT :lim
                           )
                           UPDATE outbox AS o
                           SET lease_token = :lease_token,
                               lease_expires_at = now() + (:lease_seconds * INTERVAL '1 second')
                           FROM candidates
                           WHERE o.id = candidates.id
                           RETURNING o.id, o.tenant_id, o.topic, o.payload, o.attempt_count,
                                     o.lease_token"""
                    ),
                    {
                        "lim": limit,
                        "lease_token": lease_token,
                        "lease_seconds": lease_seconds,
                    },
                )
            ).all()
        return [self._record_from_row(cast(_OutboxRow, row)) for row in rows]

    async def mark_delivered(self, record: OutboxRecord) -> bool:
        """Acknowledge a relay result only while its durable lease is still owned."""
        async with system_session_scope() as s:
            result = await s.execute(
                text(
                    """UPDATE outbox
                       SET delivered_at = now(),
                           payload = payload - 'protected_completion_url',
                           lease_token = NULL,
                           lease_expires_at = NULL
                       WHERE id = :id AND lease_token = :lease_token AND delivered_at IS NULL"""
                ),
                {"id": record.outbox_id, "lease_token": record.lease_token},
            )
        return bool(getattr(result, "rowcount", 0))

    async def reschedule(
        self,
        record: OutboxRecord,
        *,
        retry_at: datetime | None,
        error: str,
        consume_attempt: bool,
    ) -> bool:
        """Retry or terminalize only a currently live relay lease (ADR-009, FR-8.9)."""
        attempt_update = "attempt_count = attempt_count + 1," if consume_attempt else ""
        if retry_at is None:
            sql = f"""UPDATE outbox
                       SET failed_at = now(), last_error = :error,
                           payload = payload - 'protected_completion_url',
                           {attempt_update}
                           lease_token = NULL, lease_expires_at = NULL
                       WHERE id = :id
                         AND lease_token = :lease_token
                         AND delivered_at IS NULL
                         AND failed_at IS NULL
                         AND lease_expires_at > pg_catalog.clock_timestamp()"""
            params: dict[str, object] = {
                "id": record.outbox_id,
                "lease_token": record.lease_token,
                "error": error,
            }
        else:
            sql = f"""UPDATE outbox
                       SET next_attempt_at = :retry_at, last_error = :error,
                           {attempt_update}
                           lease_token = NULL, lease_expires_at = NULL
                       WHERE id = :id
                         AND lease_token = :lease_token
                         AND delivered_at IS NULL
                         AND failed_at IS NULL
                         AND lease_expires_at > pg_catalog.clock_timestamp()"""
            params = {
                "id": record.outbox_id,
                "lease_token": record.lease_token,
                "retry_at": retry_at,
                "error": error,
            }
        async with system_session_scope() as s:
            result = await s.execute(text(sql), params)
        return bool(getattr(result, "rowcount", 0))

    async def claim_notification(
        self,
        record: OutboxRecord,
        *,
        dedup_key: str,
        lease_seconds: int,
    ) -> NotificationClaim:
        """Lease a dedup key only while the exact outbox lease still authorizes a send (ADR-009)."""
        if lease_seconds < 1:
            raise ValueError("notification ledger lease_seconds must be positive")
        async with system_session_scope() as s:
            outbox_lease_expires_at = await _renew_current_outbox_lease(s, record, lease_seconds)
            if outbox_lease_expires_at is None:
                return NotificationClaim.LEASE_LOST
            row = (
                await s.execute(
                    text(
                        """INSERT INTO notification_ledger
                                (dedup_key, outbox_id, tenant_id, state, lease_token, lease_expires_at)
                           VALUES (:dedup_key, :outbox_id, :tenant_id, 'sending', :lease_token,
                                   :outbox_lease_expires_at)
                           ON CONFLICT (dedup_key) DO UPDATE
                           SET outbox_id = EXCLUDED.outbox_id,
                               tenant_id = EXCLUDED.tenant_id,
                               state = 'sending',
                               lease_token = EXCLUDED.lease_token,
                               lease_expires_at = EXCLUDED.lease_expires_at
                           WHERE notification_ledger.state <> 'delivered'
                             AND (notification_ledger.lease_expires_at IS NULL
                                  OR notification_ledger.lease_expires_at <= clock_timestamp())
                           RETURNING state"""
                    ),
                    {
                        "dedup_key": dedup_key,
                        "outbox_id": record.outbox_id,
                        "tenant_id": record.tenant_id,
                        "lease_token": record.lease_token,
                        "outbox_lease_expires_at": outbox_lease_expires_at,
                    },
                )
            ).first()
            if row is not None:
                final_outbox_lease_expires_at = await _renew_current_outbox_lease(
                    s, record, lease_seconds
                )
                if final_outbox_lease_expires_at is None:
                    raise RuntimeError(
                        "outbox lease disappeared while acquiring notification ledger"
                    )
                ledger_result = await s.execute(
                    text(
                        """UPDATE notification_ledger
                           SET lease_expires_at = :lease_expires_at
                           WHERE dedup_key = :dedup_key
                             AND lease_token = :lease_token
                             AND state = 'sending'"""
                    ),
                    {
                        "dedup_key": dedup_key,
                        "lease_token": record.lease_token,
                        "lease_expires_at": final_outbox_lease_expires_at,
                    },
                )
                if not bool(getattr(ledger_result, "rowcount", 0)):
                    raise RuntimeError(
                        "notification ledger disappeared while renewing outbox lease"
                    )
                return NotificationClaim.ACQUIRED
            existing = (
                await s.execute(
                    text("SELECT state FROM notification_ledger WHERE dedup_key = :dedup_key"),
                    {"dedup_key": dedup_key},
                )
            ).first()
        if existing is not None and existing.state == "delivered":
            return NotificationClaim.DELIVERED
        return NotificationClaim.BUSY

    async def has_notification_send_authority(
        self, record: OutboxRecord, *, dedup_key: str
    ) -> bool:
        """Fence provider I/O to one still-current outbox and ledger lease (ADR-009, NFR-8)."""
        async with system_session_scope() as s:
            result = await s.execute(
                text(
                    """SELECT EXISTS (
                           SELECT 1
                           FROM outbox AS outbox_row
                           JOIN notification_ledger AS ledger
                             ON ledger.dedup_key = :dedup_key
                            AND ledger.outbox_id = outbox_row.id
                            AND ledger.tenant_id = outbox_row.tenant_id
                           WHERE outbox_row.id = :outbox_id
                             AND outbox_row.lease_token = :lease_token
                             AND outbox_row.lease_expires_at > clock_timestamp()
                             AND outbox_row.delivered_at IS NULL
                             AND outbox_row.failed_at IS NULL
                             AND ledger.lease_token = :lease_token
                             AND ledger.lease_expires_at > clock_timestamp()
                             AND ledger.state = 'sending'
                       )"""
                ),
                {
                    "dedup_key": dedup_key,
                    "outbox_id": record.outbox_id,
                    "lease_token": record.lease_token,
                },
            )
        return bool(result.scalar_one())

    async def mark_notification_delivered(self, record: OutboxRecord, *, dedup_key: str) -> bool:
        """Close a claimed ledger slot after the port reports a successful send."""
        async with system_session_scope() as s:
            result = await s.execute(
                text(
                    """UPDATE notification_ledger
                       SET state = 'delivered', delivered_at = now(),
                           lease_token = NULL, lease_expires_at = NULL
                       WHERE dedup_key = :dedup_key AND lease_token = :lease_token"""
                ),
                {"dedup_key": dedup_key, "lease_token": record.lease_token},
            )
        return bool(getattr(result, "rowcount", 0))

    async def release_notification(self, record: OutboxRecord, *, dedup_key: str) -> bool:
        """Release only the matched live ledger/outbox lease after a failed send (ADR-009, FR-8.9)."""
        async with system_session_scope() as s:
            result = await s.execute(
                text(
                    """UPDATE notification_ledger AS ledger
                       SET state = 'pending', lease_token = NULL, lease_expires_at = NULL
                       WHERE ledger.dedup_key = :dedup_key
                         AND ledger.outbox_id = :outbox_id
                         AND ledger.tenant_id = :tenant_id
                         AND ledger.lease_token = :lease_token
                         AND ledger.state = 'sending'
                         AND ledger.lease_expires_at > pg_catalog.clock_timestamp()
                         AND EXISTS (
                             SELECT 1
                             FROM outbox AS outbox_row
                             WHERE outbox_row.id = :outbox_id
                               AND outbox_row.tenant_id = :tenant_id
                               AND outbox_row.lease_token = :lease_token
                               AND outbox_row.lease_expires_at > pg_catalog.clock_timestamp()
                               AND outbox_row.delivered_at IS NULL
                               AND outbox_row.failed_at IS NULL
                         )"""
                ),
                {
                    "dedup_key": dedup_key,
                    "outbox_id": record.outbox_id,
                    "tenant_id": record.tenant_id,
                    "lease_token": record.lease_token,
                },
            )
        return bool(getattr(result, "rowcount", 0))

    @staticmethod
    def _record_from_row(row: _OutboxRow) -> OutboxRecord:
        """Normalize psycopg's JSONB return value at the adapter boundary."""
        payload_value = row.payload
        payload = (
            payload_value if isinstance(payload_value, dict) else json.loads(str(payload_value))
        )
        return OutboxRecord(
            outbox_id=int(row.id),
            tenant_id=row.tenant_id,
            topic=row.topic,
            payload=cast("dict[str, object]", payload),
            attempt_count=int(row.attempt_count),
            lease_token=str(row.lease_token),
        )


async def _renew_current_outbox_lease(
    session: AsyncSession, record: OutboxRecord, lease_seconds: int
) -> datetime | None:
    """Reserve a full send window while holding the exact current relay lease (ADR-009, NFR-8)."""
    expires_at = (
        await session.execute(
            text(
                """UPDATE outbox
                   SET lease_expires_at = clock_timestamp() + (:lease_seconds * INTERVAL '1 second')
                   WHERE id = :outbox_id
                     AND lease_token = :lease_token
                     AND lease_expires_at > clock_timestamp()
                     AND delivered_at IS NULL
                     AND failed_at IS NULL
                   RETURNING lease_expires_at"""
            ),
            {
                "outbox_id": record.outbox_id,
                "lease_token": record.lease_token,
                "lease_seconds": lease_seconds,
            },
        )
    ).scalar_one_or_none()
    return cast(datetime | None, expires_at)


def _constraints_to_json(c: RequestConstraints) -> dict[str, object]:
    out: dict[str, object] = {
        "budget_free": c.budget_free,
        "categories": list(c.categories),
        "hard_filters": list(c.hard_filters),
    }
    if c.time_window is not None:
        out["time_window"] = [c.time_window.start.isoformat(), c.time_window.end.isoformat()]
    if c.geo is not None:
        out["geo"] = [c.geo.center.lat, c.geo.center.lon, c.geo.radius_km]
    return out


def _constraints_from_json(data: dict[str, object]) -> RequestConstraints:
    from ...domain.events import GeoPoint
    from ...domain.request import GeoConstraint

    tw = None
    if raw := data.get("time_window"):
        pair = cast("list[str]", raw)
        tw = TimeWindow(datetime.fromisoformat(pair[0]), datetime.fromisoformat(pair[1]))
    geo = None
    if graw := data.get("geo"):
        triple = cast("list[float]", graw)
        geo = GeoConstraint(GeoPoint(float(triple[0]), float(triple[1])), float(triple[2]))
    return RequestConstraints(
        time_window=tw,
        geo=geo,
        categories=tuple(cast("list[str]", data.get("categories", []))),
        budget_free=bool(data.get("budget_free", True)),
        hard_filters=tuple(cast("list[str]", data.get("hard_filters", []))),
    )
