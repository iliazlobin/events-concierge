"""Paced, local-only orchestration contracts for the ingestion command worker."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from events_concierge.config import Settings
from events_concierge.domain.ingestion_admin import IngestionProcessReport
from events_concierge.workers import ingestion_commands as worker_module


class _StopLoopError(Exception):
    """Terminate the worker after its first observable sleep."""


class _Processor:
    def __init__(self, outcomes: list[IngestionProcessReport | Exception]) -> None:
        self._outcomes = outcomes
        self.limits: list[int] = []

    async def process_once(self, limit: int = 10) -> IngestionProcessReport:
        self.limits.append(limit)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _RecordingLog:
    def __init__(self) -> None:
        self.infos: list[tuple[str, dict[str, object]]] = []
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.infos.append((event, fields))

    def warning(self, event: str, **fields: object) -> None:
        self.warnings.append((event, fields))


def _scripted_clock(*values: float) -> Callable[[], float]:
    readings = iter(values)
    return lambda: next(readings)


async def _run_one_cycle(
    processor: _Processor,
    *,
    clock: Callable[[], float],
    minimum_cycle_seconds: float = 2.0,
) -> list[float]:
    sleeps: list[float] = []

    async def stop_after_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise _StopLoopError

    with pytest.raises(_StopLoopError):
        await worker_module._run_ingestion_command_loop(
            processor,
            batch_size=2,
            minimum_cycle_seconds=minimum_cycle_seconds,
            clock=clock,
            sleep=stop_after_sleep,
        )
    return sleeps


async def test_disabled_worker_returns_before_building_any_runtime_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(admin_ingestion_enabled=False)
    built: list[object] = []
    log = _RecordingLog()
    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module, "_log", log)
    monkeypatch.setattr(worker_module, "build_container", built.append)

    await worker_module.run_ingestion_commands()

    assert built == []
    assert log.infos == [("ingestion command worker disabled", {})]


async def test_worker_runtime_guard_refuses_an_injected_non_mock_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = SimpleNamespace(
        log_level="info",
        env="production",
        admin_ingestion_enabled=True,
        mock_cloud=False,
    )
    built: list[object] = []
    log = _RecordingLog()
    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module, "_log", log)
    monkeypatch.setattr(worker_module, "build_container", built.append)

    await worker_module.run_ingestion_commands()

    assert built == []
    assert log.warnings == [
        ("ingestion command worker refused outside local mock mode", {})
    ]


async def test_enabled_worker_wires_repository_router_and_all_bounded_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        admin_ingestion_enabled=True,
        release_revision="abc123",
        image_digest="sha256:" + "a" * 64,
        catalog_ingestion_command_batch_size=3,
        catalog_ingestion_command_poll_seconds=0.75,
        catalog_ingestion_command_lease_seconds=1_200,
        catalog_refresh_dispatch_batch_size=17,
    )
    repository = object()
    container = SimpleNamespace(ingestion_admin_repo=repository)
    router = object()
    constructor_calls: list[dict[str, object]] = []
    loop_calls: list[dict[str, object]] = []

    class _Service:
        def __init__(
            self,
            received_repository: object,
            received_router: object,
            *,
            lease_seconds: int,
            cadence_batch_size: int,
            release_revision: str,
            image_digest: str | None,
        ) -> None:
            constructor_calls.append(
                {
                    "repository": received_repository,
                    "router": received_router,
                    "lease_seconds": lease_seconds,
                    "cadence_batch_size": cadence_batch_size,
                    "release_revision": release_revision,
                    "image_digest": image_digest,
                }
            )

    async def build_router(received_settings: object, received_container: object) -> object:
        assert received_settings is settings
        assert received_container is container
        return router

    async def stop_loop(
        processor: object,
        *,
        batch_size: int,
        minimum_cycle_seconds: float,
    ) -> None:
        loop_calls.append(
            {
                "processor": processor,
                "batch_size": batch_size,
                "minimum_cycle_seconds": minimum_cycle_seconds,
            }
        )

    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module, "build_container", lambda value: container)
    monkeypatch.setattr(worker_module, "build_catalog_refresh_router", build_router)
    monkeypatch.setattr(worker_module, "IngestionAdminService", _Service)
    monkeypatch.setattr(worker_module, "_run_ingestion_command_loop", stop_loop)

    await worker_module.run_ingestion_commands()

    assert constructor_calls == [
        {
            "repository": repository,
            "router": router,
            "lease_seconds": 1_200,
            "cadence_batch_size": 17,
            "release_revision": "abc123",
            "image_digest": "sha256:" + "a" * 64,
        }
    ]
    assert len(loop_calls) == 1
    assert loop_calls[0]["processor"].__class__ is _Service
    assert loop_calls[0]["batch_size"] == 3
    assert loop_calls[0]["minimum_cycle_seconds"] == 0.75


async def test_busy_cycle_waits_only_for_the_remaining_stable_interval() -> None:
    processor = _Processor(
        [IngestionProcessReport(claimed=2, completed=2, failed=0, lost_leases=0)]
    )

    sleeps = await _run_one_cycle(
        processor,
        clock=_scripted_clock(100.0, 100.4),
    )

    assert processor.limits == [2]
    assert sleeps == [pytest.approx(1.6)]


async def test_slow_cycle_still_yields_at_a_cancellation_point() -> None:
    processor = _Processor(
        [IngestionProcessReport(claimed=2, completed=1, failed=1, lost_leases=0)]
    )

    sleeps = await _run_one_cycle(
        processor,
        clock=_scripted_clock(100.0, 103.0),
    )

    assert processor.limits == [2]
    assert sleeps == [0.0]


async def test_failed_cycle_logs_only_exception_type_and_uses_the_same_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _RecordingLog()
    secret = "postgresql://operator:password@database/private"
    processor = _Processor([RuntimeError(secret)])
    monkeypatch.setattr(worker_module, "_log", log)

    sleeps = await _run_one_cycle(
        processor,
        clock=_scripted_clock(20.0, 20.25),
    )

    assert sleeps == [pytest.approx(1.75)]
    assert log.warnings == [
        (
            "ingestion command worker cycle failed",
            {"error_type": "RuntimeError"},
        )
    ]
    assert secret not in repr(log.warnings)


async def test_successful_cycle_logs_aggregate_counts_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _RecordingLog()
    report = IngestionProcessReport(claimed=2, completed=1, failed=1, lost_leases=0)
    processor = _Processor([report])
    monkeypatch.setattr(worker_module, "_log", log)

    await _run_one_cycle(processor, clock=_scripted_clock(5.0, 5.1))

    assert log.infos == [
        (
            "ingestion command worker cycle",
            {
                "claimed": 2,
                "completed": 1,
                "deferred": 0,
                "failed": 1,
                "lost_leases": 0,
            },
        )
    ]
    assert all(
        isinstance(value, int)
        for event, fields in log.infos
        for value in fields.values()
        if event == "ingestion command worker cycle"
    )


@pytest.mark.parametrize(
    ("batch_size", "minimum_cycle_seconds"),
    ((0, 1.0), (-1, 1.0), (1, 0.0), (1, -0.1)),
)
async def test_invalid_loop_bounds_fail_before_claiming(
    batch_size: int,
    minimum_cycle_seconds: float,
) -> None:
    processor = _Processor([IngestionProcessReport()])

    with pytest.raises(ValueError):
        await worker_module._run_ingestion_command_loop(
            processor,
            batch_size=batch_size,
            minimum_cycle_seconds=minimum_cycle_seconds,
        )

    assert processor.limits == []


async def test_worker_sleep_remains_interruptible_for_fast_shutdown() -> None:
    processor = _Processor([IngestionProcessReport()])
    sleeping = asyncio.Event()

    async def block_in_sleep(seconds: float) -> None:
        assert seconds == 2.0
        sleeping.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        worker_module._run_ingestion_command_loop(
            processor,
            batch_size=2,
            minimum_cycle_seconds=2.0,
            clock=lambda: 0.0,
            sleep=block_in_sleep,
        )
    )
    await asyncio.wait_for(sleeping.wait(), timeout=1.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert processor.limits == [2]
