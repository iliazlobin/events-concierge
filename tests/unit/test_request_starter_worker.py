"""Offline cadence and shutdown tests for the durable request-start worker loop."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from pydantic import ValidationError

from events_concierge.application.request_start import RequestStartWorkerStats
from events_concierge.config import Settings
from events_concierge.workers.request_starter import _run_request_start_worker


def test_request_start_cadence_defaults_are_bounded() -> None:
    settings = Settings()

    assert settings.request_start_batch_size == 5
    assert settings.request_start_poll_seconds == 2.0

    for changes in (
        {"request_start_batch_size": 0},
        {"request_start_batch_size": 51},
        {"request_start_poll_seconds": 0.09},
        {"request_start_poll_seconds": 61.0},
    ):
        with pytest.raises(ValidationError):
            Settings.model_validate(changes)


class _StopLoopError(Exception):
    """End the otherwise perpetual worker immediately after an observed test sleep."""


class _FakeDeliveryWorker:
    def __init__(self, outcomes: list[RequestStartWorkerStats | Exception]) -> None:
        self._outcomes = outcomes
        self.limits: list[int] = []

    async def run_once(self, *, limit: int = 50) -> RequestStartWorkerStats:
        self.limits.append(limit)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _scripted_clock(*values: float) -> Callable[[], float]:
    readings = iter(values)
    return lambda: next(readings)


async def _run_one_cycle(
    delivery: _FakeDeliveryWorker,
    *,
    clock: Callable[[], float],
    minimum_cycle_seconds: float = 2.0,
) -> list[float]:
    sleeps: list[float] = []

    async def stop_after_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise _StopLoopError

    with pytest.raises(_StopLoopError):
        await _run_request_start_worker(
            delivery,
            batch_size=5,
            minimum_cycle_seconds=minimum_cycle_seconds,
            clock=clock,
            sleep=stop_after_sleep,
        )
    return sleeps


async def test_busy_backlog_waits_for_the_remaining_cycle_interval() -> None:
    delivery = _FakeDeliveryWorker([RequestStartWorkerStats(claimed=5, started=5)])

    sleeps = await _run_one_cycle(delivery, clock=_scripted_clock(100.0, 100.25))

    assert delivery.limits == [5]
    assert sleeps == [pytest.approx(1.75)]


async def test_slow_backlog_pass_still_yields_before_the_next_claim() -> None:
    delivery = _FakeDeliveryWorker([RequestStartWorkerStats(claimed=5, started=5)])

    sleeps = await _run_one_cycle(delivery, clock=_scripted_clock(100.0, 102.5))

    assert delivery.limits == [5]
    assert sleeps == [0.0]


async def test_failed_pass_uses_the_same_bounded_cadence() -> None:
    delivery = _FakeDeliveryWorker([RuntimeError("Temporal unavailable")])

    sleeps = await _run_one_cycle(delivery, clock=_scripted_clock(20.0, 20.5))

    assert delivery.limits == [5]
    assert sleeps == [pytest.approx(1.5)]


@pytest.mark.parametrize(
    ("batch_size", "minimum_cycle_seconds"),
    [(0, 1.0), (-1, 1.0), (1, 0.0), (1, -0.1)],
)
async def test_invalid_cadence_is_rejected_before_a_queue_claim(
    batch_size: int, minimum_cycle_seconds: float
) -> None:
    delivery = _FakeDeliveryWorker([RequestStartWorkerStats()])

    with pytest.raises(ValueError):
        await _run_request_start_worker(
            delivery,
            batch_size=batch_size,
            minimum_cycle_seconds=minimum_cycle_seconds,
        )

    assert delivery.limits == []


async def test_sleep_is_interruptible_for_fast_worker_shutdown() -> None:
    delivery = _FakeDeliveryWorker([RequestStartWorkerStats(claimed=5, started=5)])
    sleeping = asyncio.Event()

    async def block_in_sleep(seconds: float) -> None:
        assert seconds == 2.0
        sleeping.set()
        await asyncio.Event().wait()

    worker = asyncio.create_task(
        _run_request_start_worker(
            delivery,
            batch_size=5,
            minimum_cycle_seconds=2.0,
            clock=lambda: 0.0,
            sleep=block_in_sleep,
        )
    )
    await asyncio.wait_for(sleeping.wait(), timeout=1.0)

    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    assert delivery.limits == [5]
