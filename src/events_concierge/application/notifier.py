"""Operational control loop for the durable notification outbox (FR-6.6/FR-8.9, ADR-009).

``LISTEN/NOTIFY`` is a latency optimization, never the source of truth.  This controller starts
the listener before its first durable drain, drains every immediately available batch, and waits
only after an empty claim.  A listener failure therefore degrades to bounded polling rather than
dropping an outbox row or asserting that a transport call reached a user.
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from ..ports.outbox import OutboxWakeupPort, OutboxWakeupResult, OutboxWakeupStatus
from ..ports.repositories import OutboxQueueSnapshot, OutboxRepository
from .outbox import OutboxRelay, RelayStats


class NotifierWakeupMode(StrEnum):
    """Current listener posture, distinct from an individual idle-wait result (ADR-009)."""

    LISTENING = "listening"
    POLL_FALLBACK = "poll_fallback"


@dataclass(frozen=True, slots=True)
class NotifierHealth:
    """Secret-free notifier operational state, not an assertion of provider delivery.

    ``ready`` reports whether the most recent durable queue probe succeeded.  A failed listener
    remains ready in poll-fallback mode because PostgreSQL outbox rows, rather than notifications,
    are the delivery source of truth (FR-8.9, ADR-009).
    """

    ready: bool
    wakeup_mode: NotifierWakeupMode
    cycles: int
    listener_failures: int
    wakeups: int
    poll_fallbacks: int
    last_success_at: datetime | None
    last_error: str | None
    queue_snapshot: OutboxQueueSnapshot | None
    last_wakeup_status: OutboxWakeupStatus | None


@dataclass(frozen=True, slots=True)
class NotifierCycle:
    """One bounded worker iteration for logs, readiness probes, and deterministic tests.

    ``relay`` counts accepted hand-offs to ``NotificationPort``; it does not prove a provider's
    downstream delivery to a person.  Provider delivery events remain an adapter concern.
    """

    relay: RelayStats
    queue_snapshot: OutboxQueueSnapshot | None
    wakeup: OutboxWakeupResult | None
    duration_seconds: float
    health: NotifierHealth


class NotifierWorker:
    """Run a loss-proof push-wakeup/poll loop around ``OutboxRelay`` (ADR-009).

    The caller owns the outer process lifetime and calls :meth:`run_cycle` repeatedly.  A cycle
    that claimed rows returns without an idle wait, so the caller drains backlog immediately.  An
    empty cycle waits at most ``poll_seconds`` through the wake-up port, which must preserve the
    durable polling fallback when PostgreSQL's listener connection is unavailable.
    """

    def __init__(
        self,
        relay: OutboxRelay,
        outbox: OutboxRepository,
        wakeup: OutboxWakeupPort,
        *,
        batch_size: int = 50,
        poll_seconds: float = 2.0,
        now: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        if not math.isfinite(poll_seconds) or poll_seconds <= 0:
            raise ValueError("poll_seconds must be finite and positive")
        self._relay = relay
        self._outbox = outbox
        self._wakeup = wakeup
        self._batch_size = batch_size
        self._poll_seconds = poll_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or asyncio.sleep

        self._started = False
        self._ready = False
        self._wakeup_mode = NotifierWakeupMode.POLL_FALLBACK
        self._cycles = 0
        self._listener_failures = 0
        self._wakeups = 0
        self._poll_fallbacks = 0
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._listener_error: str | None = None
        self._queue_snapshot: OutboxQueueSnapshot | None = None
        self._last_wakeup_status: OutboxWakeupStatus | None = None

    async def start(self) -> NotifierHealth:
        """Establish the listener before the first durable drain, degrading safely on failure."""
        if self._started:
            return self.health
        self._started = True
        result = await self._start_listener()
        self._record_wakeup(result)
        return self.health

    @property
    def health(self) -> NotifierHealth:
        """Return an immutable operational snapshot with no provider-delivery implication."""
        return NotifierHealth(
            ready=self._ready,
            wakeup_mode=self._wakeup_mode,
            cycles=self._cycles,
            listener_failures=self._listener_failures,
            wakeups=self._wakeups,
            poll_fallbacks=self._poll_fallbacks,
            last_success_at=self._last_success_at,
            last_error=self._last_error,
            queue_snapshot=self._queue_snapshot,
            last_wakeup_status=self._last_wakeup_status,
        )

    async def run_cycle(self) -> NotifierCycle:
        """Probe and drain once, waiting only after a durable empty claim (FR-6.6, ADR-009)."""
        if not self._started:
            await self.start()
        started_at = self._monotonic()
        snapshot = await self._queue_snapshot_or_wait()
        if snapshot is None:
            return self._cycle(
                relay=RelayStats(),
                queue_snapshot=None,
                wakeup=None,
                started_at=started_at,
            )

        try:
            relay = await self._relay.relay_once(limit=self._batch_size)
        except Exception as exc:
            self._last_error = _error_detail(exc)
            # The durable queue probe is still a successful readiness check.  Back off one bounded
            # interval before retrying the relay instead of terminating and leaving a lease storm.
            await self._sleep(self._poll_seconds)
            return self._cycle(
                relay=RelayStats(),
                queue_snapshot=snapshot,
                wakeup=None,
                started_at=started_at,
            )

        if relay.claimed > 0:
            return self._cycle(
                relay=relay,
                queue_snapshot=snapshot,
                wakeup=None,
                started_at=started_at,
            )

        wakeup = await self._wait_for_wakeup()
        return self._cycle(
            relay=relay,
            queue_snapshot=snapshot,
            wakeup=wakeup,
            started_at=started_at,
        )

    async def aclose(self) -> None:
        """Close the optional listener without changing durable queue state (ADR-009)."""
        await self._wakeup.aclose()

    async def _queue_snapshot_or_wait(self) -> OutboxQueueSnapshot | None:
        """Use a durable queue query as readiness; transient database errors get a bounded retry."""
        try:
            snapshot = await self._outbox.queue_snapshot()
        except Exception as exc:
            self._ready = False
            self._last_error = _error_detail(exc)
            self._queue_snapshot = None
            await self._sleep(self._poll_seconds)
            return None
        self._ready = True
        self._last_success_at = self._now()
        # A queryable durable queue is ready even when the listener is degraded.  Keep the latter
        # visibly represented until a later wait confirms the dedicated connection recovered.
        self._last_error = self._listener_error
        self._queue_snapshot = snapshot
        return snapshot

    async def _start_listener(self) -> OutboxWakeupResult:
        """Turn unexpected listener errors into one explicit poll-fallback result."""
        try:
            return await self._wakeup.start()
        except Exception as exc:
            return OutboxWakeupResult(
                OutboxWakeupStatus.POLL_FALLBACK,
                detail=_error_detail(exc),
            )

    async def _wait_for_wakeup(self) -> OutboxWakeupResult:
        """Bound an idle wait even if a port violates its fallback contract (ADR-009)."""
        started_at = self._monotonic()
        try:
            result = await self._wakeup.wait(self._poll_seconds)
        except Exception as exc:
            result = OutboxWakeupResult(
                OutboxWakeupStatus.POLL_FALLBACK,
                detail=_error_detail(exc),
            )
        if result.status is OutboxWakeupStatus.POLL_FALLBACK:
            await self._sleep_remaining(started_at)
        self._record_wakeup(result)
        return result

    async def _sleep_remaining(self, started_at: float) -> None:
        """Maintain the configured poll interval when a failed listener returns immediately."""
        remaining = self._poll_seconds - (self._monotonic() - started_at)
        if remaining > 0:
            await self._sleep(remaining)

    def _record_wakeup(self, result: OutboxWakeupResult) -> None:
        """Fold one listener outcome into counters without making it a readiness prerequisite."""
        self._last_wakeup_status = result.status
        if result.status is OutboxWakeupStatus.NOTIFIED:
            self._wakeups += 1
        if result.listening:
            self._wakeup_mode = NotifierWakeupMode.LISTENING
            self._listener_error = None
            self._last_error = None
            return
        self._wakeup_mode = NotifierWakeupMode.POLL_FALLBACK
        self._listener_failures += 1
        self._poll_fallbacks += 1
        self._listener_error = result.detail or "outbox listener unavailable"
        self._last_error = self._listener_error

    def _cycle(
        self,
        *,
        relay: RelayStats,
        queue_snapshot: OutboxQueueSnapshot | None,
        wakeup: OutboxWakeupResult | None,
        started_at: float,
    ) -> NotifierCycle:
        """Build one immutable cycle result after all bounded work and any idle wait complete."""
        self._cycles += 1
        duration_seconds = max(0.0, self._monotonic() - started_at)
        return NotifierCycle(
            relay=relay,
            queue_snapshot=queue_snapshot,
            wakeup=wakeup,
            duration_seconds=duration_seconds,
            health=self.health,
        )


def _error_detail(error: Exception) -> str:
    """Keep operational error state bounded and avoid exposing exception objects to callers."""
    message = str(error).strip()
    if not message:
        return type(error).__name__
    return f"{type(error).__name__}: {message[:500]}"
