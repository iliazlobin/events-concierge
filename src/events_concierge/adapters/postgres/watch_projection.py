"""PostgreSQL lease adapter for the ADR-008 lifecycle watch projection outbox.

The table has no RLS because it is an opaque global control queue, like the existing notification
and request-start outboxes.  The actual watch mutation re-enters tenant scope through the guarded
registry functions, so this worker never reads a tenant's calendar/event payload.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import text

from ...domain.enums import Source
from ...infra.db import system_session_scope
from ...ports.watch_projection import WatchProjectionAction, WatchProjectionRecord

_MAX_CLAIM_LIMIT = 1000
_MAX_LEASE_SECONDS = 3600


class _WatchProjectionRow(Protocol):
    """The named row projection returned from one leased queue claim."""

    projection_id: int
    tenant_id: UUID
    workflow_id: str
    canonical_event_id: UUID
    source: str
    action: str
    active_since: datetime
    attempt_count: int
    lease_token: str


class PostgresLifecycleWatchProjectionOutbox:
    """Claim/release opaque lifecycle watch instructions with at-least-once recovery (ADR-008)."""

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[WatchProjectionRecord]:
        """Lease ready records using SKIP LOCKED so concurrent workers cannot double-apply them."""
        if limit < 1:
            raise ValueError("watch projection claim limit must be positive")
        if limit > _MAX_CLAIM_LIMIT:
            raise ValueError(f"watch projection claim limit must not exceed {_MAX_CLAIM_LIMIT}")
        if lease_seconds < 1:
            raise ValueError("watch projection lease_seconds must be positive")
        if lease_seconds > _MAX_LEASE_SECONDS:
            raise ValueError(f"watch projection lease_seconds must not exceed {_MAX_LEASE_SECONDS}")
        lease_token = uuid4().hex
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT *
                        FROM public.fn_claim_watch_projections(
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
        return [self._record(cast(_WatchProjectionRow, row)) for row in rows]

    async def has_live_lease(self, record: WatchProjectionRecord) -> bool:
        """Read the final registry-entry lease fence without exposing outbox rows (NFR-8)."""
        async with system_session_scope() as session:
            live = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_has_live_watch_projection_lease(
                            :projection_id, :lease_token
                        ) AS live
                        """
                    ),
                    {
                        "projection_id": record.projection_id,
                        "lease_token": record.lease_token,
                    },
                )
            ).scalar_one()
        return bool(live)

    async def mark_delivered(self, record: WatchProjectionRecord) -> bool:
        """Acknowledge only the exact lease holder; lost leases remain safely replayable."""
        async with system_session_scope() as session:
            marked = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_mark_watch_projection_delivered(
                            :projection_id, :lease_token
                        ) AS marked
                        """
                    ),
                    {
                        "projection_id": record.projection_id,
                        "lease_token": record.lease_token,
                    },
                )
            ).scalar_one()
        return bool(marked)

    async def reschedule(self, record: WatchProjectionRecord, *, error: str) -> bool:
        """Release a failed record with bounded exponential backoff, never silently dropping it."""
        async with system_session_scope() as session:
            released = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_watch_projection_reschedule(
                            :projection_id, :lease_token, :error
                        ) AS released
                        """
                    ),
                    {
                        "projection_id": record.projection_id,
                        "lease_token": record.lease_token,
                        "error": error[:1000],
                    },
                )
            ).scalar_one()
        return bool(released)

    @staticmethod
    def _record(row: _WatchProjectionRow) -> WatchProjectionRecord:
        """Decode only validated opaque queue fields into the port value object."""
        return WatchProjectionRecord(
            projection_id=int(row.projection_id),
            tenant_id=row.tenant_id,
            workflow_id=row.workflow_id,
            canonical_event_id=row.canonical_event_id,
            source=Source(row.source),
            action=WatchProjectionAction(row.action),
            active_since=row.active_since,
            attempt_count=int(row.attempt_count),
            lease_token=row.lease_token,
        )
