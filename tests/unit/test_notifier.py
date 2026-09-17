"""Focused operational tests for the ADR-009 push-wakeup notifier control loop."""

from __future__ import annotations

from datetime import UTC, datetime

from events_concierge.application.notifier import NotifierWakeupMode, NotifierWorker
from events_concierge.application.outbox import NotificationDeliveryStats
from events_concierge.ports.outbox import OutboxWakeupResult, OutboxWakeupStatus
from events_concierge.ports.repositories import OutboxQueueSnapshot


class FakeOutbox:
    """Queue-probe seam; worker behavior is isolated behind ``FakeDeliveryWorker`` in these tests."""

    def __init__(self, outcomes: list[OutboxQueueSnapshot | Exception], events: list[str]) -> None:
        self._outcomes = outcomes
        self._events = events

    async def queue_snapshot(self) -> OutboxQueueSnapshot:
        self._events.append("snapshot")
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeDeliveryWorker:
    """Bounded worker seam that records whether an idle cycle was incorrectly drained or waited."""

    def __init__(
        self, outcomes: list[NotificationDeliveryStats | Exception], events: list[str]
    ) -> None:
        self._outcomes = outcomes
        self._events = events
        self.limits: list[int] = []

    async def run_once(self, *, limit: int = 50) -> NotificationDeliveryStats:
        self._events.append("worker")
        self.limits.append(limit)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeWakeup:
    """Listener seam with scripted start/wait outcomes and no implicit clock advancement."""

    def __init__(
        self,
        starts: list[OutboxWakeupResult],
        waits: list[OutboxWakeupResult],
        events: list[str],
    ) -> None:
        self._starts = starts
        self._waits = waits
        self._events = events
        self.wait_timeouts: list[float] = []
        self.closed = False

    async def start(self) -> OutboxWakeupResult:
        self._events.append("start")
        return self._starts.pop(0)

    async def wait(self, timeout_seconds: float) -> OutboxWakeupResult:
        self._events.append("wait")
        self.wait_timeouts.append(timeout_seconds)
        return self._waits.pop(0)

    async def aclose(self) -> None:
        self.closed = True


def _snapshot(*, pending: int = 0, ready: int = 0, leased: int = 0) -> OutboxQueueSnapshot:
    return OutboxQueueSnapshot(
        pending=pending,
        ready=ready,
        leased=leased,
        oldest_ready_at=None,
    )


def _worker(
    *,
    outbox: FakeOutbox,
    delivery: FakeDeliveryWorker,
    wakeup: FakeWakeup,
    sleeps: list[float],
) -> NotifierWorker:
    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return NotifierWorker(
        delivery,
        outbox,
        wakeup,
        batch_size=7,
        poll_seconds=2.0,
        now=lambda: datetime(2026, 7, 17, 12, tzinfo=UTC),
        monotonic=lambda: 0.0,
        sleep=sleep,
    )


async def test_listener_starts_before_first_drain_and_backlog_does_not_idle_wait() -> None:
    events: list[str] = []
    wakeup = FakeWakeup([OutboxWakeupResult(OutboxWakeupStatus.LISTENING)], [], events)
    outbox = FakeOutbox([_snapshot(pending=7, ready=7)], events)
    delivery = FakeDeliveryWorker(
        [NotificationDeliveryStats(claimed=7, acknowledged=7, sent=7)], events
    )
    sleeps: list[float] = []

    cycle = await _worker(
        outbox=outbox, delivery=delivery, wakeup=wakeup, sleeps=sleeps
    ).run_cycle()

    assert events == ["start", "snapshot", "worker"]
    assert delivery.limits == [7]
    assert cycle.delivery.claimed == 7
    assert cycle.wakeup is None
    assert cycle.health.ready is True
    assert cycle.health.wakeup_mode is NotifierWakeupMode.LISTENING
    assert wakeup.wait_timeouts == []
    assert sleeps == []


async def test_empty_queue_waits_for_signal_without_claiming_provider_delivery() -> None:
    events: list[str] = []
    wakeup = FakeWakeup(
        [OutboxWakeupResult(OutboxWakeupStatus.LISTENING)],
        [OutboxWakeupResult(OutboxWakeupStatus.NOTIFIED)],
        events,
    )
    outbox = FakeOutbox([_snapshot()], events)
    delivery = FakeDeliveryWorker([NotificationDeliveryStats()], events)
    sleeps: list[float] = []

    cycle = await _worker(
        outbox=outbox, delivery=delivery, wakeup=wakeup, sleeps=sleeps
    ).run_cycle()

    assert events == ["start", "snapshot", "worker", "wait"]
    assert wakeup.wait_timeouts == [2.0]
    assert cycle.wakeup == OutboxWakeupResult(OutboxWakeupStatus.NOTIFIED)
    assert cycle.delivery == NotificationDeliveryStats()
    assert cycle.health.wakeups == 1
    assert cycle.health.ready is True
    assert sleeps == []


async def test_listener_start_failure_stays_visible_while_backlog_drains() -> None:
    events: list[str] = []
    wakeup = FakeWakeup(
        [OutboxWakeupResult(OutboxWakeupStatus.POLL_FALLBACK, "connect refused")], [], events
    )
    outbox = FakeOutbox([_snapshot(pending=1, ready=1)], events)
    delivery = FakeDeliveryWorker([NotificationDeliveryStats(claimed=1)], events)
    sleeps: list[float] = []

    cycle = await _worker(
        outbox=outbox, delivery=delivery, wakeup=wakeup, sleeps=sleeps
    ).run_cycle()

    assert events == ["start", "snapshot", "worker"]
    assert cycle.health.ready is True
    assert cycle.health.wakeup_mode is NotifierWakeupMode.POLL_FALLBACK
    assert cycle.health.last_error == "connect refused"
    assert wakeup.wait_timeouts == []
    assert sleeps == []


async def test_listener_poll_fallback_remains_ready_when_the_durable_queue_is_queryable() -> None:
    events: list[str] = []
    wakeup = FakeWakeup(
        [OutboxWakeupResult(OutboxWakeupStatus.POLL_FALLBACK, "connect refused")],
        [OutboxWakeupResult(OutboxWakeupStatus.POLL_FALLBACK, "reconnect refused")],
        events,
    )
    outbox = FakeOutbox([_snapshot()], events)
    delivery = FakeDeliveryWorker([NotificationDeliveryStats()], events)
    sleeps: list[float] = []

    cycle = await _worker(
        outbox=outbox, delivery=delivery, wakeup=wakeup, sleeps=sleeps
    ).run_cycle()

    assert cycle.health.ready is True
    assert cycle.health.wakeup_mode is NotifierWakeupMode.POLL_FALLBACK
    assert cycle.health.listener_failures == 2
    assert cycle.health.poll_fallbacks == 2
    assert cycle.health.last_error == "reconnect refused"
    assert wakeup.wait_timeouts == [2.0]
    assert sleeps == [2.0]


async def test_listener_recovers_from_poll_fallback_on_the_next_idle_cycle() -> None:
    events: list[str] = []
    wakeup = FakeWakeup(
        [OutboxWakeupResult(OutboxWakeupStatus.POLL_FALLBACK, "connect refused")],
        [
            OutboxWakeupResult(OutboxWakeupStatus.POLL_FALLBACK, "reconnect refused"),
            OutboxWakeupResult(OutboxWakeupStatus.NOTIFIED),
        ],
        events,
    )
    outbox = FakeOutbox([_snapshot(), _snapshot()], events)
    delivery = FakeDeliveryWorker(
        [NotificationDeliveryStats(), NotificationDeliveryStats()], events
    )
    sleeps: list[float] = []
    worker = _worker(outbox=outbox, delivery=delivery, wakeup=wakeup, sleeps=sleeps)

    first = await worker.run_cycle()
    second = await worker.run_cycle()

    assert first.health.ready is True
    assert first.health.wakeup_mode is NotifierWakeupMode.POLL_FALLBACK
    assert first.health.last_error == "reconnect refused"
    assert second.wakeup == OutboxWakeupResult(OutboxWakeupStatus.NOTIFIED)
    assert second.health.ready is True
    assert second.health.wakeup_mode is NotifierWakeupMode.LISTENING
    assert second.health.listener_failures == 2
    assert second.health.poll_fallbacks == 2
    assert second.health.wakeups == 1
    assert second.health.last_error is None
    assert sleeps == [2.0]


async def test_failed_queue_probe_marks_worker_unready_and_backs_off_without_claiming() -> None:
    events: list[str] = []
    wakeup = FakeWakeup([OutboxWakeupResult(OutboxWakeupStatus.LISTENING)], [], events)
    outbox = FakeOutbox([RuntimeError("database unavailable")], events)
    delivery = FakeDeliveryWorker([NotificationDeliveryStats(claimed=1)], events)
    sleeps: list[float] = []

    cycle = await _worker(
        outbox=outbox, delivery=delivery, wakeup=wakeup, sleeps=sleeps
    ).run_cycle()

    assert events == ["start", "snapshot"]
    assert cycle.queue_snapshot is None
    assert cycle.delivery == NotificationDeliveryStats()
    assert cycle.wakeup is None
    assert cycle.health.ready is False
    assert cycle.health.last_error == "RuntimeError: database unavailable"
    assert sleeps == [2.0]


async def test_worker_failure_keeps_a_successful_queue_probe_ready_and_retries_boundedly() -> None:
    events: list[str] = []
    wakeup = FakeWakeup([OutboxWakeupResult(OutboxWakeupStatus.LISTENING)], [], events)
    outbox = FakeOutbox([_snapshot(pending=1, ready=1)], events)
    delivery = FakeDeliveryWorker([RuntimeError("temporary claim failure")], events)
    sleeps: list[float] = []

    cycle = await _worker(
        outbox=outbox, delivery=delivery, wakeup=wakeup, sleeps=sleeps
    ).run_cycle()

    assert events == ["start", "snapshot", "worker"]
    assert cycle.queue_snapshot == _snapshot(pending=1, ready=1)
    assert cycle.health.ready is True
    assert cycle.health.last_error == "RuntimeError: temporary claim failure"
    assert cycle.wakeup is None
    assert sleeps == [2.0]


async def test_close_delegates_to_the_listener_port() -> None:
    events: list[str] = []
    wakeup = FakeWakeup([OutboxWakeupResult(OutboxWakeupStatus.LISTENING)], [], events)
    worker = _worker(
        outbox=FakeOutbox([_snapshot()], events),
        delivery=FakeDeliveryWorker([NotificationDeliveryStats()], events),
        wakeup=wakeup,
        sleeps=[],
    )

    await worker.aclose()

    assert wakeup.closed is True
