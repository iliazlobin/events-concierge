"""At-least-once projection from guarded lifecycle transitions to ADR-008 watches.

The projection does not poll sources or call Temporal.  It only converts a transition's durable
outbox record into a locked register/unregister call on the watch registry, so a process crash
between lifecycle commit and projection cannot permanently miss a later organizer change.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ..ports.change_detection import LifecycleWatch, WatchRegistryPort
from ..ports.watch_projection import (
    LifecycleWatchProjectionOutboxPort,
    WatchProjectionAction,
    WatchProjectionRecord,
)


@dataclass(frozen=True, slots=True)
class WatchProjectionStats:
    """One bounded projection drain's operational result (FR-8.7a, ADR-008)."""

    claimed: int = 0
    applied: int = 0
    acknowledged: int = 0
    retried: int = 0
    lost_leases: int = 0


class _ProjectionOutcome(StrEnum):
    """One projection attempt's result before any queue acknowledgement."""

    APPLIED = "applied"
    STALE = "stale"
    RETRY = "retry"
    LEASE_LOST = "lease_lost"


class LifecycleWatchProjectionRelay:
    """Drain lifecycle watch instructions without coupling notification delivery to detection.

    A delayed register can legitimately find that a later terminal transition already closed the
    lifecycle.  The locked registry function reports that as a stale no-op, which is safely
    acknowledged: the matching unregistration record either already ran or is itself idempotent.
    Other errors retain the record for retry, preserving ADR-008's no-crash-gap invariant.
    """

    def __init__(
        self,
        outbox: LifecycleWatchProjectionOutboxPort,
        watches: WatchRegistryPort,
        *,
        lease_seconds: int = 60,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._outbox = outbox
        self._watches = watches
        self._lease_seconds = lease_seconds

    async def relay_once(self, *, limit: int = 50) -> WatchProjectionStats:
        """Apply one bounded batch and retain failed records for durable retry (ADR-008)."""
        if limit < 1:
            raise ValueError("limit must be positive")
        records = await self._outbox.claim_batch(limit, self._lease_seconds)
        applied = 0
        acknowledged = 0
        retried = 0
        lost_leases = 0
        for record in records:
            outcome = await self._project(record)
            if outcome is _ProjectionOutcome.RETRY:
                retried += 1
                continue
            if outcome is _ProjectionOutcome.LEASE_LOST:
                # A fresh claimant may now own the projection. Do not mutate the registry,
                # acknowledge, or reschedule under this stale token; queue recovery owns it.
                lost_leases += 1
                continue
            applied += int(outcome is _ProjectionOutcome.APPLIED)
            acknowledged += int(await self._outbox.mark_delivered(record))
        return WatchProjectionStats(
            claimed=len(records),
            applied=applied,
            acknowledged=acknowledged,
            retried=retried,
            lost_leases=lost_leases,
        )

    async def _project(self, record: WatchProjectionRecord) -> _ProjectionOutcome:
        """Apply one idempotent register/unregister or return a durable retry instruction."""
        watch = LifecycleWatch(
            tenant_id=record.tenant_id,
            workflow_id=record.workflow_id,
            canonical_event_id=record.canonical_event_id,
            source=record.source,
            active_since=record.active_since,
        )
        try:
            if not await self._outbox.has_live_lease(record):
                # This observed false says only the queue lease is stale/reclaimed. It does not
                # infer that the lifecycle itself is stale, so leave the projection unchanged for
                # its current/future owner to recover (FR-8.7a, NFR-8, ADR-008).
                return _ProjectionOutcome.LEASE_LOST
            if record.action is WatchProjectionAction.REGISTER:
                return (
                    _ProjectionOutcome.APPLIED
                    if await self._watches.register(watch)
                    else _ProjectionOutcome.STALE
                )
            return (
                _ProjectionOutcome.APPLIED
                if await self._watches.unregister(watch)
                else _ProjectionOutcome.STALE
            )
        except Exception as error:
            return (
                _ProjectionOutcome.RETRY
                if await self._outbox.reschedule(record, error=str(error))
                else _ProjectionOutcome.LEASE_LOST
            )
