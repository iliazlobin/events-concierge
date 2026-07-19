"""Fixture-only central organizer-change ingestion and fanout.

This service has no provider polling, webhook endpoint, Temporal client, or notification dependency.
It turns an already-normalized public observation into one fingerprinted durable change and fans it
out through an injected port.  A fanout crash leaves the lease recoverable; the recipient workflow
deduplicates its at-least-once signal by the same fingerprint (FR-8.7/8.7a/8.9, ADR-008).
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ports.calendar_repair import (
    ClosedWorkflowCalendarRepairPort,
    ClosedWorkflowSignalError,
)
from ..ports.change_detection import (
    DetectedEventChange,
    EventChangeRepository,
    LifecycleWatch,
    OrganizerChangeDetectorPort,
    OrganizerChangeFanoutPort,
    WatchRegistryPort,
)


@dataclass(frozen=True, slots=True)
class FanoutResult:
    """One bounded fanout drain's observable outcome (ADR-008)."""

    claimed: int = 0
    delivered: int = 0
    deferred: int = 0
    repair_queued: int = 0
    lost_leases: int = 0


@dataclass(frozen=True, slots=True)
class ChangeIngestResult:
    """The idempotent result of persisting one public normalized organizer change."""

    inserted: bool
    queued_deliveries: int
    fanout: FanoutResult


@dataclass(frozen=True, slots=True)
class DetectorRunResult:
    """Aggregate result for one fixture-detector pass (FR-8.7a, ADR-008)."""

    observed: int
    inserted: int
    queued_deliveries: int
    fanout: FanoutResult


class ChangeDetectionService:
    """Persist global public change observations and signal affected workflows once-or-more.

    Lifecycle entry/exit callers register/unregister opaque workflow subscriptions.  The service
    deliberately does not infer subscriptions by polling tenant lifecycle rows: doing so would make
    an RLS-scoped lifecycle table into the central detector's cross-tenant data plane (ADR-008).
    """

    def __init__(
        self,
        watches: WatchRegistryPort,
        changes: EventChangeRepository,
        fanout: OrganizerChangeFanoutPort,
        *,
        closed_workflow_repairs: ClosedWorkflowCalendarRepairPort | None = None,
        fanout_batch_size: int = 100,
        lease_seconds: int = 60,
    ) -> None:
        if fanout_batch_size < 1:
            raise ValueError("fanout_batch_size must be positive")
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._watches = watches
        self._changes = changes
        self._fanout = fanout
        self._closed_workflow_repairs = closed_workflow_repairs
        self._fanout_batch_size = fanout_batch_size
        self._lease_seconds = lease_seconds

    async def register_watch(self, watch: LifecycleWatch) -> bool:
        """Project one registered/scheduled lifecycle into the central watch registry (ADR-008)."""
        return await self._watches.register(watch)

    async def unregister_watch(self, watch: LifecycleWatch) -> bool:
        """Project lifecycle exit so terminal workflows no longer consume source detection capacity."""
        return await self._watches.unregister(watch)

    async def ingest(self, change: DetectedEventChange) -> ChangeIngestResult:
        """Record one event change, then make a bounded best-effort durable fanout pass.

        The record remains authoritative even when the injected signal port fails.  A subsequent
        worker pass calls :meth:`fanout_pending`; duplicate detector observations cannot create a
        second change row or a second delivery row for the same tenant workflow (ADR-008).
        """
        recorded = await self._changes.record(change)
        return ChangeIngestResult(
            inserted=recorded.inserted,
            queued_deliveries=recorded.queued_deliveries,
            fanout=await self.fanout_pending(),
        )

    async def run_detector(self, detector: OrganizerChangeDetectorPort) -> DetectorRunResult:
        """Run an injected fixture detector once; no source-specific network logic belongs here."""
        observations = await detector.collect()
        return await self.ingest_observations(observations)

    async def ingest_observations(
        self, observations: list[DetectedEventChange]
    ) -> DetectorRunResult:
        """Persist one bounded normalized observation batch, then drain its durable fanout work.

        Fixture-detector callers retain this opportunistic drain behavior. Source polling instead
        uses :meth:`record_observations`, so a later fanout/Temporal outage cannot turn a durable
        public observation into a false poll failure (FR-8.7a/8.9, ADR-008).
        """
        recorded = await self.record_observations(observations)
        return DetectorRunResult(
            observed=recorded.observed,
            inserted=recorded.inserted,
            queued_deliveries=recorded.queued_deliveries,
            fanout=await self.fanout_pending(),
        )

    async def record_observations(
        self, observations: list[DetectedEventChange]
    ) -> DetectorRunResult:
        """Persist a normalized batch without coupling its durability to workflow signaling.

        A source-poll cursor may advance after this method returns: ``event_changes`` and the
        opaque delivery rows are durable, while the separately leased fanout worker can recover a
        later Temporal/client outage without re-polling a public source (ADR-008).
        """
        inserted = 0
        queued = 0
        for change in observations:
            recorded = await self._changes.record(change)
            inserted += int(recorded.inserted)
            queued += recorded.queued_deliveries
        return DetectorRunResult(
            observed=len(observations),
            inserted=inserted,
            queued_deliveries=queued,
            fanout=FanoutResult(),
        )

    async def fanout_pending(self) -> FanoutResult:
        """Lease and signal one bounded batch, preserving at-least-once delivery after crashes.

        An exact database-clock lease projection immediately before signaling avoids a signal for
        authority already observed as stale.  It cannot eliminate the residual check-to-signal
        race, so the recipient still deduplicates the same fingerprint. A port exception releases
        only that delivery. The already-recorded public change and all other deliveries remain
        intact, so a caller can retry without re-polling or re-normalizing a provider response
        (FR-8.7/8.9, NFR-8, ADR-008).
        """
        deliveries = await self._changes.claim_deliveries(
            self._fanout_batch_size, self._lease_seconds
        )
        delivered = 0
        deferred = 0
        repair_queued = 0
        lost_leases = 0
        for delivery in deliveries:
            try:
                if not await self._changes.has_live_delivery_lease(delivery):
                    # A fresh claimant may now own this row. Do not signal, acknowledge, release,
                    # or enqueue a repair under the stale token; recovery remains queue-owned.
                    lost_leases += 1
                    continue
                await self._fanout.signal_organizer_change(delivery)
            except ClosedWorkflowSignalError as error:
                # A closed execution is not a transient signal failure.  Retire this exact fanout
                # lease and defer the same normalized command to ADR-008's 15-minute calendar
                # repair queue.  If this worker has not been configured with that queue (legacy
                # fixture use), retain the ordinary retry behavior instead of silently dropping it.
                if (
                    self._closed_workflow_repairs is not None
                    and await self._closed_workflow_repairs.enqueue(delivery, error=str(error))
                ):
                    repair_queued += 1
                elif await self._changes.release_delivery(delivery, error=str(error)):
                    deferred += 1
                else:
                    lost_leases += 1
                continue
            except Exception as error:
                if await self._changes.release_delivery(delivery, error=str(error)):
                    deferred += 1
                else:
                    lost_leases += 1
                continue
            if await self._changes.mark_delivered(delivery):
                delivered += 1
            else:
                # The signal may already have arrived, so never send it again in this drain.  A
                # lease recovery later is safe because the workflow stores the same fingerprint.
                lost_leases += 1
        return FanoutResult(
            claimed=len(deliveries),
            delivered=delivered,
            deferred=deferred,
            repair_queued=repair_queued,
            lost_leases=lost_leases,
        )
