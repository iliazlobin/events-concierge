"""Fixture-only, durable scheduling and staleness control for central change detection.

This service knows only distinct public watch keys, caller-supplied cadence values, and normalized
change observations. It has no provider client, webhook, RelayInbox, Temporal Schedule, or source
default; those remain separate owner-gated integrations (FR-8.7a, NFR-17, ADR-008).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ..ports.change_detection import (
    DetectedEventChange,
    WatchedEvent,
    WatchPollCadence,
    WatchPollDetectorPort,
    WatchPollHealthReport,
    WatchPollLease,
    WatchPollPlan,
    WatchPollState,
    WatchPollStateRepository,
    WatchRegistryPort,
    assess_watch_poll_health,
)
from .change_detection import ChangeDetectionService, DetectorRunResult, FanoutResult


@dataclass(slots=True)
class _PollTally:
    """Mutable local counters for one bounded, sequential fixture polling pass."""

    observed: int = 0
    inserted: int = 0
    queued_deliveries: int = 0
    polled: int = 0
    succeeded: int = 0
    failed: int = 0
    lost_leases: int = 0
    ingest_error_type: str | None = None


@dataclass(frozen=True, slots=True)
class WatchPollCycleResult:
    """PII-free outcome of one bounded central watch-poll pass."""

    watched: int
    planned: int
    claimed: int
    polled: int
    succeeded: int
    failed: int
    lost_leases: int
    ingest_error_type: str | None
    detector: DetectorRunResult
    preflight_health: WatchPollHealthReport
    health: WatchPollHealthReport


class WatchPollScheduler:
    """Run explicit source/event cadence plans through lease-safe fixture polling.

    The poll cursor is marked successful only after normalized changes have reached the durable
    change ledger. A crash or ledger failure therefore leaves the cursor retryable, and any
    repeated public observation converges on ADR-008's fingerprint before fanout.
    """

    def __init__(
        self,
        watches: WatchRegistryPort,
        poll_state: WatchPollStateRepository,
        detector: WatchPollDetectorPort,
        changes: ChangeDetectionService,
        plan: WatchPollPlan,
        *,
        batch_size: int = 100,
        lease_seconds: int = 60,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("watch poll batch_size must be positive")
        if lease_seconds <= 0:
            raise ValueError("watch poll lease_seconds must be positive")
        self._watches = watches
        self._poll_state = poll_state
        self._detector = detector
        self._changes = changes
        self._plan = plan
        self._batch_size = batch_size
        self._lease_seconds = lease_seconds
        self._now = now or (lambda: datetime.now(UTC))

    async def run_once(self) -> WatchPollCycleResult:
        """Sequentially claim/persist/ack one bounded planned-watch pass, then report health."""
        watches = await self._watches.list_watches()
        states = await self._poll_state.list_watch_poll_states()
        preflight_health = assess_watch_poll_health(
            watches,
            states,
            self._plan,
            now=_utc(self._now()),
        )
        planned = 0
        claimed = 0
        tally = _PollTally()
        for watch in _ordered_watches(watches, states):
            cadence = self._plan.cadence_for(watch.source)
            if cadence is None:
                continue
            planned += 1
            if claimed >= self._batch_size:
                continue
            lease = await self._poll_state.claim_watch_poll(
                watch,
                now=_utc(self._now()),
                lease_seconds=self._lease_seconds,
            )
            if lease is None:
                continue
            claimed += 1
            await self._poll_and_record(watch, cadence, lease, tally)

        completed_at = _utc(self._now())
        health = assess_watch_poll_health(
            watches,
            await self._poll_state.list_watch_poll_states(),
            self._plan,
            now=completed_at,
        )
        return WatchPollCycleResult(
            watched=len(watches),
            planned=planned,
            claimed=claimed,
            polled=tally.polled,
            succeeded=tally.succeeded,
            failed=tally.failed,
            lost_leases=tally.lost_leases,
            ingest_error_type=tally.ingest_error_type,
            detector=DetectorRunResult(
                observed=tally.observed,
                inserted=tally.inserted,
                queued_deliveries=tally.queued_deliveries,
                fanout=FanoutResult(),
            ),
            preflight_health=preflight_health,
            health=health,
        )

    async def _poll_and_record(
        self,
        watch: WatchedEvent,
        cadence: WatchPollCadence,
        lease: WatchPollLease,
        tally: _PollTally,
    ) -> None:
        """Finish one lease before the next source poll, avoiding cross-batch lease expiry."""
        if not await self._poll_state.has_live_watch_poll_lease(lease):
            # The exact token may have expired or been reclaimed after the scheduler claim. Do
            # not make a detector/provider call under stale authority; the current/future worker
            # owns recovery from the unchanged cursor (FR-8.7a, NFR-8, ADR-008).
            tally.lost_leases += 1
            return
        try:
            observations = await self._detector.poll(watch)
            _validate_observations(watch, observations)
        except Exception as error:
            await self._release_or_lose(lease, cadence, type(error).__name__, tally)
            return
        tally.polled += 1
        try:
            recorded = await self._changes.record_observations(observations)
        except Exception as error:
            if tally.ingest_error_type is None:
                tally.ingest_error_type = type(error).__name__
            await self._release_or_lose(lease, cadence, type(error).__name__, tally)
            return
        tally.observed += recorded.observed
        tally.inserted += recorded.inserted
        tally.queued_deliveries += recorded.queued_deliveries
        completed_at = _utc(self._now())
        if await self._poll_state.mark_watch_poll_succeeded(
            lease,
            completed_at=completed_at,
            next_due_at=completed_at + cadence.poll_interval,
        ):
            tally.succeeded += 1
        else:
            tally.lost_leases += 1

    async def _release_or_lose(
        self,
        lease: WatchPollLease,
        cadence: WatchPollCadence,
        error_type: str,
        tally: _PollTally,
    ) -> None:
        """Count one failed attempt only if its exact lease is still current."""
        if await self._release_failure(lease, cadence, error_type):
            tally.failed += 1
        else:
            tally.lost_leases += 1

    async def _release_failure(
        self,
        lease: WatchPollLease,
        cadence: WatchPollCadence,
        error_type: str,
    ) -> bool:
        """Release a cursor at its caller-approved next cadence slot without raw error storage."""
        completed_at = _utc(self._now())
        return await self._poll_state.release_watch_poll(
            lease,
            completed_at=completed_at,
            next_due_at=completed_at + cadence.poll_interval,
            error_type=error_type,
        )


def _validate_observations(watch: WatchedEvent, observations: list[DetectedEventChange]) -> None:
    """Reject a detector result that crosses a public watch boundary before ledger persistence."""
    for observation in observations:
        if (
            observation.canonical_event_id != watch.canonical_event_id
            or observation.source is not watch.source
        ):
            raise ValueError("watch poll detector returned an observation for a different watch")


def _ordered_watches(
    watches: list[WatchedEvent], states: list[WatchPollState]
) -> list[WatchedEvent]:
    """Prioritize oldest due cursors so a bounded pass cannot permanently favor UUID order."""
    states_by_key = {(state.canonical_event_id, state.source): state for state in states}

    def sort_key(watch: WatchedEvent) -> tuple[datetime, datetime, str, str]:
        state = states_by_key.get((watch.canonical_event_id, watch.source))
        due_at = state.next_due_at if state is not None else watch.created_at
        return due_at, watch.created_at, str(watch.canonical_event_id), watch.source.value

    return sorted(watches, key=sort_key)


def _utc(value: datetime) -> datetime:
    """Require an explicit UTC-convertible clock for durable multi-process staleness arithmetic."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("watch poll clock must return a timezone-aware datetime")
    return value.astimezone(UTC)
