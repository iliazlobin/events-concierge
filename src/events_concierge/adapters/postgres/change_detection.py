"""PostgreSQL control-plane storage for fixture-backed central change detection.

``watch_registry`` and ``event_changes`` contain public canonical-event data only.  The companion
subscription/delivery tables contain opaque tenant UUIDs and deterministic workflow ids solely to
fan one public event change out to its active lifecycle owners; they intentionally carry no email,
credentials, raw request text, or tenant event payload (ADR-008).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import text

from ...domain.enums import EventStatus, Source
from ...infra.db import system_session_scope, tenant_session_scope
from ...ports.change_detection import (
    DetectedEventChange,
    EventChangeInsert,
    LifecycleWatch,
    OrganizerChangeDelivery,
    WatchedEvent,
)

_MAX_CLAIM_LIMIT = 1000
_MAX_LEASE_SECONDS = 3600


class _WatchRow(Protocol):
    canonical_event_id: UUID
    source: str
    subscriber_count: int
    created_at: datetime


class _DeliveryRow(Protocol):
    fingerprint: str
    canonical_event_id: UUID
    source: str
    event_status: str
    start_at: datetime | None
    end_at: datetime | None
    time_zone: str | None
    title: str | None
    venue_name: str | None
    tenant_id: UUID
    workflow_id: str
    attempt_count: int
    lease_token: str


class _EventChangeInsertRow(Protocol):
    """Named result returned by the guarded public-change recording capability."""

    inserted: bool
    queued_deliveries: int


class PostgresChangeDetectionRepository:
    """Global public change ledger plus opaque, leased per-workflow fanout queue (ADR-008)."""

    async def register(self, watch: LifecycleWatch) -> bool:
        """Register only a locked active lifecycle and matching public source link (ADR-007/008).

        The SECURITY DEFINER function explicitly rechecks the caller's RLS tenant GUC, then locks
        the lifecycle row before it creates the opaque subscription.  The non-superuser app role
        cannot take that lock directly because ADR-007 correctly revokes lifecycle UPDATE rights.
        A stale/inactive or mismatched projection is an idempotent ``False`` rather than a worker
        error, so the durable projection worker can acknowledge it safely.
        """
        async with tenant_session_scope(watch.tenant_id) as session:
            registered = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_register_change_watch(
                            :tenant_id, :workflow_id, :canonical_event_id, :source, :active_since
                        ) AS registered
                        """
                    ),
                    {
                        "tenant_id": watch.tenant_id,
                        "workflow_id": watch.workflow_id,
                        "canonical_event_id": watch.canonical_event_id,
                        "source": watch.source.value,
                        "active_since": watch.active_since,
                    },
                )
            ).scalar_one()
        return bool(registered)

    async def unregister(self, watch: LifecycleWatch) -> bool:
        """Remove only a terminal current-tenant subscription and retire its pending fanout work.

        The 0049 replacement of this guarded function rejects active/mismatched lifecycle rows,
        so a stale projection is acknowledged as ``False`` without exposing a direct watch-delete
        capability to the app role (ADR-007/008).
        """
        async with tenant_session_scope(watch.tenant_id) as session:
            unregistered = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_unregister_change_watch(
                            :tenant_id, :workflow_id, :canonical_event_id, :source
                        ) AS unregistered
                        """
                    ),
                    {
                        "canonical_event_id": watch.canonical_event_id,
                        "source": watch.source.value,
                        "tenant_id": watch.tenant_id,
                        "workflow_id": watch.workflow_id,
                    },
                )
            ).scalar_one()
        return bool(unregistered)

    async def list_watches(self) -> list[WatchedEvent]:
        """Read only global public keys plus opaque subscription counts (ADR-008)."""
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT * FROM public.fn_list_active_change_watches()
                        """
                    )
                )
            ).all()
        return [self._watched_event_from_row(cast(_WatchRow, row)) for row in rows]

    async def record(self, change: DetectedEventChange) -> EventChangeInsert:
        """Insert a public fingerprint and fan it out to every canonical-event subscription.

        The persisted source is feeder provenance only.  It cannot filter subscriptions because
        ADR-008 fingerprints intentionally collapse identical changes from different sources.
        """
        async with system_session_scope() as session:
            row = cast(
                _EventChangeInsertRow,
                (
                    await session.execute(
                        text(
                            """
                            SELECT *
                            FROM public.fn_record_event_change(
                                :fingerprint, :canonical_event_id, :source, :event_status, :start_at,
                                :end_at, :time_zone, :title, :venue_name
                            )
                            """
                        ),
                        self._change_params(change),
                    )
                ).one(),
            )
        return EventChangeInsert(
            inserted=bool(row.inserted), queued_deliveries=int(row.queued_deliveries)
        )

    async def claim_deliveries(
        self, limit: int, lease_seconds: int
    ) -> list[OrganizerChangeDelivery]:
        """Lease globally pending fanout rows with SKIP LOCKED (ADR-008, NFR-8)."""
        if limit < 1:
            raise ValueError("delivery claim limit must be positive")
        if limit > _MAX_CLAIM_LIMIT:
            raise ValueError(f"delivery claim limit must not exceed {_MAX_CLAIM_LIMIT}")
        if lease_seconds < 1:
            raise ValueError("delivery lease_seconds must be positive")
        if lease_seconds > _MAX_LEASE_SECONDS:
            raise ValueError(f"delivery lease_seconds must not exceed {_MAX_LEASE_SECONDS}")
        lease_token = uuid4().hex
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT *
                        FROM public.fn_claim_event_change_deliveries(
                            :limit, :lease_seconds, :lease_token
                        )
                        """
                    ),
                    {
                        "limit": limit,
                        "lease_token": lease_token,
                        "lease_seconds": lease_seconds,
                    },
                )
            ).all()
        return [self._delivery_from_row(cast(_DeliveryRow, row)) for row in rows]

    async def has_live_delivery_lease(self, delivery: OrganizerChangeDelivery) -> bool:
        """Read the final pre-signal lease fence without exposing global queue rows (NFR-8)."""
        async with system_session_scope() as session:
            live = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_has_live_event_change_delivery_lease(
                            :fingerprint, :tenant_id, :workflow_id, :lease_token
                        ) AS live
                        """
                    ),
                    self._delivery_params(delivery),
                )
            ).scalar_one()
        return bool(live)

    async def mark_delivered(self, delivery: OrganizerChangeDelivery) -> bool:
        """Acknowledge delivery only while the same worker still holds its lease."""
        async with system_session_scope() as session:
            marked = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_mark_event_change_delivery(
                            :fingerprint, :tenant_id, :workflow_id, :lease_token
                        ) AS marked
                        """
                    ),
                    self._delivery_params(delivery),
                )
            ).scalar_one()
        return bool(marked)

    async def release_delivery(self, delivery: OrganizerChangeDelivery, *, error: str) -> bool:
        """Release a failed signal with bounded exponential backoff and durable at-least-once work."""
        async with system_session_scope() as session:
            released = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_release_event_change_delivery(
                            :fingerprint, :tenant_id, :workflow_id, :lease_token, :error
                        ) AS released
                        """
                    ),
                    {
                        **self._delivery_params(delivery),
                        "error": error[:1000],
                    },
                )
            ).scalar_one()
        return bool(released)

    @staticmethod
    def _change_params(change: DetectedEventChange) -> dict[str, object]:
        return {
            "fingerprint": change.fingerprint,
            "canonical_event_id": change.canonical_event_id,
            "source": change.source.value,
            "event_status": change.event_status.value,
            "start_at": change.start_at,
            "end_at": change.end_at,
            "time_zone": change.time_zone,
            "title": change.title,
            "venue_name": change.venue_name,
        }

    @staticmethod
    def _delivery_params(delivery: OrganizerChangeDelivery) -> dict[str, object]:
        return {
            "fingerprint": delivery.change.fingerprint,
            "tenant_id": delivery.tenant_id,
            "workflow_id": delivery.workflow_id,
            "lease_token": delivery.lease_token,
        }

    @staticmethod
    def _watched_event_from_row(row: _WatchRow) -> WatchedEvent:
        return WatchedEvent(
            canonical_event_id=row.canonical_event_id,
            source=Source(row.source),
            subscriber_count=int(row.subscriber_count),
            created_at=row.created_at,
        )

    @staticmethod
    def _delivery_from_row(row: _DeliveryRow) -> OrganizerChangeDelivery:
        return OrganizerChangeDelivery(
            change=DetectedEventChange(
                fingerprint=row.fingerprint,
                canonical_event_id=row.canonical_event_id,
                source=Source(row.source),
                event_status=EventStatus(row.event_status),
                start_at=row.start_at,
                end_at=row.end_at,
                time_zone=row.time_zone,
                title=row.title,
                venue_name=row.venue_name,
            ),
            tenant_id=row.tenant_id,
            workflow_id=row.workflow_id,
            attempt_count=int(row.attempt_count),
            lease_token=row.lease_token,
        )
