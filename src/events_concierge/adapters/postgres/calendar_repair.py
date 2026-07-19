"""PostgreSQL control-plane adapter for ADR-008 closed-workflow calendar repairs."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import text

from ...domain.enums import EventStatus, Source
from ...infra.db import system_session_scope
from ...ports.calendar_repair import CalendarRepairRecord
from ...ports.change_detection import (
    DetectedEventChange,
    OrganizerChangeDelivery,
)

_MAX_CLAIM_LIMIT = 1000
_MAX_LEASE_SECONDS = 3600


class _CalendarRepairRow(Protocol):
    """Named SQL row returned by the joined repair-queue claim."""

    repair_id: int
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


class PostgresClosedWorkflowCalendarRepairRepository:
    """Lease the delayed, idempotent safety-net queue independently from Temporal (ADR-008)."""

    async def enqueue(self, delivery: OrganizerChangeDelivery, *, error: str) -> bool:
        """Atomically retire a closed-workflow fanout lease and enqueue exactly one 15-minute repair."""
        async with system_session_scope() as session:
            retired = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_enqueue_closed_workflow_calendar_repair(
                            :fingerprint, :tenant_id, :workflow_id, :lease_token, :error
                        ) AS retired
                        """
                    ),
                    {
                        "fingerprint": delivery.change.fingerprint,
                        "tenant_id": delivery.tenant_id,
                        "workflow_id": delivery.workflow_id,
                        "lease_token": delivery.lease_token,
                        "error": error[:1000],
                    },
                )
            ).scalar_one()
        return bool(retired)

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[CalendarRepairRecord]:
        """Claim ready delayed repairs with a recoverable global lease."""
        if limit < 1:
            raise ValueError("calendar repair claim limit must be positive")
        if limit > _MAX_CLAIM_LIMIT:
            raise ValueError(f"calendar repair claim limit must not exceed {_MAX_CLAIM_LIMIT}")
        if lease_seconds < 1:
            raise ValueError("calendar repair lease_seconds must be positive")
        if lease_seconds > _MAX_LEASE_SECONDS:
            raise ValueError(f"calendar repair lease_seconds must not exceed {_MAX_LEASE_SECONDS}")
        lease_token = uuid4().hex
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT *
                        FROM public.fn_claim_calendar_repairs(
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
        return [self._record(cast(_CalendarRepairRow, row)) for row in rows]

    async def mark_repaired(self, record: CalendarRepairRecord) -> bool:
        """Acknowledge only the repair worker that still owns the exact lease."""
        async with system_session_scope() as session:
            acknowledged = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_mark_calendar_repair(
                            :repair_id, :lease_token
                        ) AS acknowledged
                        """
                    ),
                    {"repair_id": record.repair_id, "lease_token": record.lease_token},
                )
            ).scalar_one()
        return bool(acknowledged)

    async def reschedule(self, record: CalendarRepairRecord, *, error: str) -> bool:
        """Release a failed repair with the detector's bounded exponential recovery cadence."""
        async with system_session_scope() as session:
            released = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_reschedule_calendar_repair(
                            :repair_id, :lease_token, :error
                        ) AS released
                        """
                    ),
                    {
                        "repair_id": record.repair_id,
                        "lease_token": record.lease_token,
                        "error": error[:1000],
                    },
                )
            ).scalar_one()
        return bool(released)

    async def organizer_change_applied(self, record: CalendarRepairRecord) -> bool:
        """Check a held repair lease's durable change identity without a global ledger read (ADR-008)."""
        async with system_session_scope() as session:
            found = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_calendar_repair_change_applied(
                            :repair_id, :lease_token
                        ) AS found
                        """
                    ),
                    {
                        "repair_id": record.repair_id,
                        "lease_token": record.lease_token,
                    },
                )
            ).scalar_one()
        return bool(found)

    async def has_live_repair_lease(self, record: CalendarRepairRecord) -> bool:
        """Fence direct reconciliation on a database-clock current repair lease (ADR-008/NFR-8)."""
        async with system_session_scope() as session:
            live = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_has_live_calendar_repair_lease(
                            :repair_id, :lease_token
                        ) AS live
                        """
                    ),
                    {
                        "repair_id": record.repair_id,
                        "lease_token": record.lease_token,
                    },
                )
            ).scalar_one()
        return bool(live)

    @staticmethod
    def _record(row: _CalendarRepairRow) -> CalendarRepairRecord:
        """Decode the joined public change and opaque repair control record."""
        return CalendarRepairRecord(
            repair_id=int(row.repair_id),
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
