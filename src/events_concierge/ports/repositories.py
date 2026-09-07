"""Repository ports. All tenant-scoped reads/writes run under an established tenant context so RLS
fails closed to zero rows without it (FR-1.3/1.4). The catalog is tenant-neutral (no tenant column)."""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from ..domain.catalog_browse import (
    CatalogBrowseCity,
    CatalogBrowseCursor,
    CatalogBrowseDay,
    CatalogBrowseEvent,
    CatalogBrowseProvider,
    CatalogBrowseSort,
    CatalogBrowseTopic,
)
from ..domain.credentials import Tenant
from ..domain.enums import HandoffReminderKind, LifecycleState, Source
from ..domain.events import CandidateEvent, CanonicalEvent
from ..domain.lifecycle import (
    HandoffCompletionReceipt,
    HandoffCompletionTarget,
    HandoffReminderResult,
    HandoffTask,
    Lifecycle,
)
from ..domain.request import EventRequest, RequestConstraints


@dataclass(frozen=True, slots=True)
class OutboxRecord:
    """A leased outbox row handed to one relay attempt (FR-8.9, ADR-009)."""

    outbox_id: int
    tenant_id: UUID
    topic: str
    payload: dict[str, object]
    attempt_count: int
    lease_token: str


@dataclass(frozen=True, slots=True)
class OutboxQueueSnapshot:
    """A point-in-time, transport-agnostic view of relay backlog (FR-8.9, ADR-009).

    ``pending`` excludes terminal rows, ``ready`` is immediately claimable, and ``leased`` is
    actively owned by another relay. ``oldest_ready_at`` supports lag observability without
    exposing any tenant payload from the global control queue.
    """

    pending: int
    ready: int
    leased: int
    oldest_ready_at: datetime | None


@dataclass(frozen=True, slots=True)
class RequestStartRecord:
    """A leased, opaque request-start instruction for the ADR-003 start-outbox.

    Raw request text deliberately remains in the RLS-protected ``event_requests`` row.  The
    cross-tenant starter receives only opaque IDs here and reopens that row under its recorded
    tenant context before it calls Temporal.
    """

    request_id: UUID
    tenant_id: UUID
    attempt_count: int
    lease_token: str


class NotificationClaim(StrEnum):
    """The durable notification-ledger result for a stable deduplication key."""

    ACQUIRED = "acquired"
    DELIVERED = "delivered"
    BUSY = "busy"
    LEASE_LOST = "lease_lost"


class CatalogRepository(Protocol):
    """Tenant-neutral canonical event catalog (pgvector + tsvector). No tenant column (ADR-001)."""

    async def upsert_candidates(self, candidates: list[CandidateEvent]) -> list[CanonicalEvent]:
        """Normalize + dedup candidates into canonical events, retaining all source links."""
        ...

    async def retrieve(
        self, constraints: RequestConstraints, intent_embedding: list[float] | None, limit: int
    ) -> list[CanonicalEvent]:
        """Hybrid retrieval (dense ANN + sparse) fused to a candidate set for ranking (FR-4.1)."""
        ...

    async def get(self, canonical_event_id: UUID) -> CanonicalEvent | None: ...

    async def browse_current(
        self,
        *,
        source_keys: tuple[str, ...] = (),
        after: CatalogBrowseCursor | None,
        limit: int,
        starts_after: datetime | None = None,
        starts_before: datetime | None = None,
        date_ranges: tuple[tuple[datetime, datetime], ...] = (),
        query: str | None = None,
        city: str | None = None,
        cities: tuple[str, ...] | None = None,
        location_scopes: tuple[str, ...] = (),
        price: str | None = None,
        price_max_cents: int | None = None,
        price_min_cents: int | None = None,
        topics: tuple[str, ...] = (),
        availability: str | None = None,
        sort: CatalogBrowseSort = "soonest",
        include_providers: bool = True,
    ) -> tuple[list[CatalogBrowseEvent], list[CatalogBrowseProvider]]:
        """Filter latest-success or explicitly retained past observations before keyset paging.

        ``include_providers`` is the escape hatch for callers that page events and discard the
        source inventory: the provider rollup is a whole-catalog aggregation that costs about as
        much as the page itself, so a caller that never reads it should not pay for it.
        """
        ...

    async def list_topic_facets(
        self,
        *,
        source_keys: tuple[str, ...] = (),
        starts_after: datetime | None,
        starts_before: datetime | None,
        date_ranges: tuple[tuple[datetime, datetime], ...] = (),
        query: str | None,
        cities: tuple[str, ...],
        location_scopes: tuple[str, ...],
        price: str | None,
        price_max_cents: int | None,
        price_min_cents: int | None = None,
        availability: str | None = None,
    ) -> list[CatalogBrowseTopic]:
        """Count stable topics for the non-topic portion of the current browse filter."""
        ...

    async def list_day_facets(
        self,
        *,
        source_keys: tuple[str, ...] = (),
        starts_after: datetime | None,
        starts_before: datetime | None,
        date_ranges: tuple[tuple[datetime, datetime], ...] = (),
        query: str | None,
        cities: tuple[str, ...],
        location_scopes: tuple[str, ...],
        price: str | None,
        price_max_cents: int | None,
        price_min_cents: int | None = None,
        topics: tuple[str, ...] = (),
        availability: str | None = None,
        time_zone: str,
    ) -> list[CatalogBrowseDay]:
        """Count the current browse filter into local calendar days instead of paging it.

        Unlike :meth:`list_topic_facets`, this applies the topic selection, because the calendar
        grid must agree with the agenda the same filters produce.
        """
        ...

    async def list_city_facets(self) -> list[CatalogBrowseCity]:
        """List normalized cities from the admitted upcoming catalog projection."""
        ...


class TenantRepository(Protocol):
    async def get(self, tenant_id: UUID) -> Tenant | None: ...
    async def add(self, tenant: Tenant) -> None: ...


class RequestRepository(Protocol):
    async def add(self, request: EventRequest) -> None: ...
    async def get(self, tenant_id: UUID, request_id: UUID) -> EventRequest | None: ...

    async def register_workflow_targets(
        self,
        tenant_id: UUID,
        workflow_ids: tuple[str, ...],
    ) -> None:
        """Persist child IDs before Temporal may start them; a tenant erasure fence rejects late inserts."""
        ...

    async def add_and_enqueue_start(self, request: EventRequest, dedup_key: str) -> None:
        """Atomically persist an EventRequest and its replay-safe Temporal start instruction.

        Replays with the same deterministic request ID/dedup key converge without a second parent
        workflow (FR-6.8, FR-8.1, AC-48, ADR-003).
        """
        ...

    async def claim_start(
        self, tenant_id: UUID, request_id: UUID, lease_seconds: int
    ) -> RequestStartRecord | None:
        """Lease this exact pending request start for the low-latency intake path."""
        ...

    async def claim_start_batch(self, limit: int, lease_seconds: int) -> list[RequestStartRecord]:
        """Lease ready starts across tenants for the durable background starter worker."""
        ...

    async def has_live_start_lease(self, record: RequestStartRecord) -> bool:
        """Confirm an exact current start lease immediately before Temporal egress (NFR-8, ADR-003)."""
        ...

    def request_start_guard(self, record: RequestStartRecord) -> AbstractAsyncContextManager[bool]:
        """Serialize a bounded Temporal start with erasure begin and yield exact authority."""
        ...

    async def account_erasure_fenced(self, tenant_id: UUID) -> bool:
        """Recheck the durable tenant erasure fence immediately after Temporal start returns."""
        ...

    async def mark_start_started(self, record: RequestStartRecord) -> bool:
        """Acknowledge a Temporal start only while this relay still owns its lease."""
        ...

    async def reschedule_start(
        self, record: RequestStartRecord, *, retry_at: datetime, error: str
    ) -> bool:
        """Release a failed engine-start lease for retry; start rows never drop on retry budget."""
        ...

    async def start_has_started(self, tenant_id: UUID, request_id: UUID) -> bool:
        """Report whether the durable start instruction has already been acknowledged."""
        ...

    async def mark_failed_no_candidate(
        self, tenant_id: UUID, request_id: UUID, transition_id: str
    ) -> bool:
        """Atomically terminalize one request and enqueue its no-result notification (FR-5.0/6.6)."""
        ...


class LifecycleRepository(Protocol):
    """Advances state only through the guarded transition (ADR-007), committing state + ledger +
    outbox atomically. One non-terminal row per (tenant, canonical_event_id)."""

    async def get_or_create(
        self, tenant_id: UUID, canonical_event_id: UUID, workflow_id: str
    ) -> Lifecycle: ...

    async def find_by_workflow_id(self, tenant_id: UUID, workflow_id: str) -> Lifecycle | None:
        """Return this tenant's exact lifecycle, including terminal states, without creating it."""
        ...

    async def find_active(self, tenant_id: UUID, canonical_event_id: UUID) -> Lifecycle | None:
        """Return only this tenant's non-terminal lifecycle, never creating a phantom row (FR-8.8)."""
        ...

    async def transition(
        self,
        lifecycle: Lifecycle,
        to_state: LifecycleState,
        transition_id: str,
        outbox_payload: dict[str, object],
    ) -> None:
        """Ledger check (retry no-op) + guarded UPDATE + ledger insert + outbox insert, one
        transaction (ADR-007). Advances lifecycle.state on success; raises on an illegal transition."""
        ...


class HandoffRepository(Protocol):
    async def create(self, task: HandoffTask) -> None:
        """Persist one task and its opaque TTL-recovery instruction atomically (FR-6.6, ADR-007)."""
        ...

    async def create_and_transition(
        self,
        task: HandoffTask,
        lifecycle: Lifecycle,
        to_state: LifecycleState,
        transition_id: str,
        outbox_payload: dict[str, object],
    ) -> None:
        """Atomically create a handoff task, TTL instruction, and lifecycle/outbox transition."""
        ...

    async def create_calendar_recovery(
        self, task: HandoffTask, outbox_payload: dict[str, object]
    ) -> None:
        """Atomically persist a calendar-recovery task, TTL instruction, and outbox projection."""
        ...

    async def create_withdrawal_handoff(
        self, task: HandoffTask, outbox_payload: dict[str, object]
    ) -> None:
        """Atomically create a withdrawal task, TTL instruction, and notification while it withdraws."""
        ...

    async def get(self, tenant_id: UUID, task_id: str) -> HandoffTask | None:
        """Read a handoff task through its required tenant RLS context (ADR-007)."""
        ...

    async def resolve_completion_token(self, token: str) -> HandoffCompletionTarget | None:
        """Resolve one opaque completion capability without accepting caller-selected tenant data.

        The implementation stores and compares only a SHA-256 digest. The narrow privileged lookup
        may return an active, used, expired, or inactive target; it never exposes a general
        cross-tenant task-read primitive (FR-1.3, FR-6.3, FR-16).
        """
        ...

    async def get_completion_attempt(
        self,
        tenant_id: UUID,
        task_id: str,
        completion_id: str,
    ) -> HandoffCompletionReceipt | None:
        """Return only an exact same-command receipt for activity acknowledgement recovery."""
        ...

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
        """Atomically consume a verified capability, complete its task, and register lifecycle."""
        ...

    async def record_completion_review(
        self,
        task: HandoffTask,
        *,
        completion_id: str,
        detail: str,
    ) -> bool:
        """Consume an unverified mark-done into a receipt and review-required notification."""
        ...

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
        """Atomically ledger and enqueue one due active-task reminder (FR-6.6, ADR-007/009)."""
        ...


class OutboxRepository(Protocol):
    """Durable lease + notification-ledger operations for the ADR-009 relay."""

    async def queue_snapshot(self) -> OutboxQueueSnapshot:
        """Report queue pressure using the same eligibility rules as ``claim_batch`` (FR-8.9)."""
        ...

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[OutboxRecord]:
        """Atomically lease eligible rows; a crashed consumer's expired lease is reclaimable."""
        ...

    async def mark_delivered(self, record: OutboxRecord) -> bool:
        """Acknowledge a row only if this relay still owns its lease."""
        ...

    async def reschedule(
        self,
        record: OutboxRecord,
        *,
        retry_at: datetime | None,
        error: str,
        consume_attempt: bool,
    ) -> bool:
        """Release for a bounded retry or terminal failure.

        Only a known ``NotificationPort`` exception consumes the bounded delivery-failure budget.
        Lease contention and recovery deferrals must pass ``consume_attempt=False`` (ADR-009).
        """
        ...

    async def claim_notification(
        self,
        record: OutboxRecord,
        *,
        dedup_key: str,
        lease_seconds: int,
    ) -> NotificationClaim:
        """Acquire a deduplicated send slot only while the matching outbox lease is current (ADR-009)."""
        ...

    async def has_notification_send_authority(
        self, record: OutboxRecord, *, dedup_key: str
    ) -> bool:
        """Immediately before I/O, confirm the outbox and ledger still authorize this send (ADR-009)."""
        ...

    async def mark_notification_delivered(self, record: OutboxRecord, *, dedup_key: str) -> bool:
        """Commit delivery in the ledger after a port send returns successfully."""
        ...

    async def release_notification(self, record: OutboxRecord, *, dedup_key: str) -> bool:
        """Release a failed send slot so the next scheduled outbox retry can reclaim it."""
        ...
