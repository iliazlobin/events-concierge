"""Enqueue-only local ingestion cadence scheduler contracts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from events_concierge.application.ingestion_cadence_scheduler import (
    IngestionCadenceScheduleOutcome,
    IngestionCadenceScheduler,
    IngestionCadenceScheduleReport,
    cadence_command_id,
)
from events_concierge.config import Settings
from events_concierge.domain.catalog_sources import CatalogRefreshDue, CatalogSource
from events_concierge.domain.enums import CatalogSourceMode
from events_concierge.domain.ingestion_admin import (
    IngestionCommand,
    IngestionCommandAction,
    IngestionCommandStatus,
)
from events_concierge.ports.ingestion_admin import (
    IngestionCommandConflictError,
    IngestionCommandUnavailableError,
)
from events_concierge.workers import ingestion_cadence as worker_module

_NOW = datetime(2026, 7, 29, 12, 2, 3, tzinfo=UTC)


class _Repository:
    """Record due probes and durable enqueue attempts."""

    def __init__(
        self,
        due: list[CatalogRefreshDue],
        *,
        enqueue_error: Exception | None = None,
    ) -> None:
        self._due = due
        self._enqueue_error = enqueue_error
        self.due_calls: list[tuple[datetime, int]] = []
        self.enqueue_calls: list[dict[str, object]] = []

    async def list_due_refreshes(
        self,
        now: datetime,
        *,
        limit: int,
    ) -> list[CatalogRefreshDue]:
        self.due_calls.append((now, limit))
        return self._due[:limit]

    async def enqueue(
        self,
        command_id: UUID,
        action: IngestionCommandAction,
        source_key: str | None,
        requested_by: str,
        release_revision: str,
        image_digest: str | None,
    ) -> IngestionCommand:
        self.enqueue_calls.append(
            {
                "command_id": command_id,
                "action": action,
                "source_key": source_key,
                "requested_by": requested_by,
                "release_revision": release_revision,
                "image_digest": image_digest,
            }
        )
        if self._enqueue_error is not None:
            raise self._enqueue_error
        return IngestionCommand(
            command_id=command_id,
            action=action,
            source_key=source_key,
            status=IngestionCommandStatus.QUEUED,
            requested_at=_NOW,
            started_at=None,
            completed_at=None,
            result=None,
            error_code=None,
        )


class _RecordingLog:
    def __init__(self) -> None:
        self.infos: list[tuple[str, dict[str, object]]] = []
        self.warnings: list[tuple[str, dict[str, object]]] = []

    def info(self, event: str, **fields: object) -> None:
        self.infos.append((event, fields))

    def warning(self, event: str, **fields: object) -> None:
        self.warnings.append((event, fields))


class _StopLoopError(Exception):
    """Terminate the perpetual worker after its first observable sleep."""


class _Scheduler:
    def __init__(self, outcome: IngestionCadenceScheduleReport | Exception) -> None:
        self._outcome = outcome
        self.calls = 0

    async def schedule_once(self) -> IngestionCadenceScheduleReport:
        self.calls += 1
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _due(source_key: str = "reviewed-source") -> CatalogRefreshDue:
    source = CatalogSource(
        source_key=source_key,
        display_name="Reviewed source",
        publisher="Tests",
        seed_url="https://events.example.test/calendar",
        approved_origins=("https://events.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=_NOW - timedelta(days=1),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1_500,
    )
    return CatalogRefreshDue(
        source=source,
        due_at=_NOW - timedelta(minutes=2),
        last_succeeded_at=_NOW - timedelta(minutes=62),
    )


def _scripted_clock(*values: float) -> Callable[[], float]:
    readings = iter(values)
    return lambda: next(readings)


async def _run_one_cycle(
    scheduler: _Scheduler,
    *,
    clock: Callable[[], float],
    minimum_cycle_seconds: float = 300.0,
) -> list[float]:
    sleeps: list[float] = []

    async def stop_after_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        raise _StopLoopError

    with pytest.raises(_StopLoopError):
        await worker_module._run_ingestion_cadence_loop(
            scheduler,
            minimum_cycle_seconds=minimum_cycle_seconds,
            clock=clock,
            sleep=stop_after_sleep,
        )
    return sleeps


def test_scheduler_settings_are_explicit_and_bounded() -> None:
    settings = Settings()

    assert settings.catalog_ingestion_scheduler_enabled is False
    assert settings.catalog_ingestion_scheduler_interval_seconds == 300

    for interval in (59, 3_601):
        with pytest.raises(ValidationError):
            Settings(catalog_ingestion_scheduler_interval_seconds=interval)
    with pytest.raises(ValidationError):
        Settings(catalog_ingestion_scheduler_enabled=True)


def test_command_identity_is_stable_within_a_utc_slot_and_changes_after_it() -> None:
    first = cadence_command_id(_NOW, interval_seconds=300)
    same_slot = cadence_command_id(_NOW + timedelta(seconds=100), interval_seconds=300)
    next_slot = cadence_command_id(_NOW + timedelta(seconds=300), interval_seconds=300)

    assert first == same_slot
    assert first != next_slot


async def test_idle_probe_never_appends_a_command() -> None:
    repository = _Repository([])
    scheduler = IngestionCadenceScheduler(repository, now=lambda: _NOW)

    report = await scheduler.schedule_once()

    assert report == IngestionCadenceScheduleReport(
        outcome=IngestionCadenceScheduleOutcome.IDLE,
        due_sources=0,
    )
    assert repository.due_calls == [(_NOW, 1)]
    assert repository.enqueue_calls == []


async def test_due_probe_appends_one_deterministic_fleet_command() -> None:
    digest = "sha256:" + "a" * 64
    repository = _Repository([_due(), _due("second-reviewed-source")])
    scheduler = IngestionCadenceScheduler(
        repository,
        interval_seconds=300,
        release_revision="revision-1",
        image_digest=digest,
        now=lambda: _NOW,
    )

    report = await scheduler.schedule_once()

    expected_id = cadence_command_id(_NOW, interval_seconds=300)
    assert report == IngestionCadenceScheduleReport(
        outcome=IngestionCadenceScheduleOutcome.SCHEDULED,
        due_sources=1,
        command_id=expected_id,
        command_status=IngestionCommandStatus.QUEUED,
    )
    assert repository.due_calls == [(_NOW, 1)]
    assert repository.enqueue_calls == [
        {
            "command_id": expected_id,
            "action": IngestionCommandAction.REFRESH_DUE,
            "source_key": None,
            "requested_by": "local-cadence-scheduler",
            "release_revision": "revision-1",
            "image_digest": digest,
        }
    ]


async def test_repeated_probe_in_the_same_slot_reuses_the_command_identity() -> None:
    repository = _Repository([_due()])
    scheduler = IngestionCadenceScheduler(repository, now=lambda: _NOW)

    first = await scheduler.schedule_once()
    second = await scheduler.schedule_once()

    assert first.command_id == second.command_id
    assert [call["command_id"] for call in repository.enqueue_calls] == [
        first.command_id,
        first.command_id,
    ]


@pytest.mark.parametrize(
    ("error", "outcome"),
    [
        (
            IngestionCommandConflictError("command_conflict"),
            IngestionCadenceScheduleOutcome.ACTIVE_COMMAND,
        ),
        (
            IngestionCommandUnavailableError("policy_blocked"),
            IngestionCadenceScheduleOutcome.UNAVAILABLE,
        ),
    ],
)
async def test_expected_queue_refusals_remain_nonexceptional_scheduler_posture(
    error: Exception,
    outcome: IngestionCadenceScheduleOutcome,
) -> None:
    repository = _Repository([_due()], enqueue_error=error)
    scheduler = IngestionCadenceScheduler(repository, now=lambda: _NOW)

    report = await scheduler.schedule_once()

    assert report.outcome is outcome
    assert report.due_sources == 1
    assert report.command_id == cadence_command_id(_NOW, interval_seconds=300)


async def test_worker_guards_return_before_building_runtime_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _RecordingLog()
    built: list[object] = []
    monkeypatch.setattr(worker_module, "_log", log)
    monkeypatch.setattr(worker_module, "build_container", built.append)

    monkeypatch.setattr(worker_module, "get_settings", Settings)
    await worker_module.run_ingestion_cadence()

    assert built == []
    assert log.infos == [("ingestion cadence scheduler disabled", {})]

    log.infos.clear()
    monkeypatch.setattr(
        worker_module,
        "get_settings",
        lambda: SimpleNamespace(
            log_level="info",
            env="local",
            catalog_ingestion_scheduler_enabled=True,
            admin_ingestion_enabled=True,
            mock_cloud=False,
        ),
    )
    await worker_module.run_ingestion_cadence()

    assert built == []
    assert log.warnings == [("ingestion cadence scheduler refused outside local mock mode", {})]


async def test_worker_wires_scheduler_and_five_minute_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        admin_ingestion_enabled=True,
        catalog_ingestion_scheduler_enabled=True,
        catalog_ingestion_scheduler_interval_seconds=420,
        release_revision="revision-2",
        image_digest="sha256:" + "b" * 64,
    )
    repository = object()
    container = SimpleNamespace(ingestion_admin_repo=repository)
    constructor_calls: list[dict[str, object]] = []
    loop_calls: list[dict[str, object]] = []

    class _ConstructedScheduler:
        def __init__(
            self,
            received_repository: object,
            *,
            interval_seconds: int,
            release_revision: str,
            image_digest: str | None,
        ) -> None:
            constructor_calls.append(
                {
                    "repository": received_repository,
                    "interval_seconds": interval_seconds,
                    "release_revision": release_revision,
                    "image_digest": image_digest,
                }
            )

    async def stop_loop(
        scheduler: object,
        *,
        minimum_cycle_seconds: float,
    ) -> None:
        loop_calls.append(
            {
                "scheduler": scheduler,
                "minimum_cycle_seconds": minimum_cycle_seconds,
            }
        )

    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module, "build_container", lambda value: container)
    monkeypatch.setattr(worker_module, "IngestionCadenceScheduler", _ConstructedScheduler)
    monkeypatch.setattr(worker_module, "_run_ingestion_cadence_loop", stop_loop)

    await worker_module.run_ingestion_cadence()

    assert constructor_calls == [
        {
            "repository": repository,
            "interval_seconds": 420,
            "release_revision": "revision-2",
            "image_digest": "sha256:" + "b" * 64,
        }
    ]
    assert len(loop_calls) == 1
    assert loop_calls[0]["scheduler"].__class__ is _ConstructedScheduler
    assert loop_calls[0]["minimum_cycle_seconds"] == 420


async def test_worker_loop_uses_stable_cadence_and_logs_no_exception_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _RecordingLog()
    secret = "postgresql://operator:password@database/private"
    scheduler = _Scheduler(RuntimeError(secret))
    monkeypatch.setattr(worker_module, "_log", log)

    sleeps = await _run_one_cycle(
        scheduler,
        clock=_scripted_clock(10.0, 10.25),
    )

    assert sleeps == [pytest.approx(299.75)]
    assert log.warnings == [
        (
            "ingestion cadence scheduler cycle failed",
            {"error_type": "RuntimeError"},
        )
    ]
    assert secret not in repr(log.warnings)


async def test_active_command_cycle_logs_only_bounded_posture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _RecordingLog()
    scheduler = _Scheduler(
        IngestionCadenceScheduleReport(
            outcome=IngestionCadenceScheduleOutcome.ACTIVE_COMMAND,
            due_sources=1,
        )
    )
    monkeypatch.setattr(worker_module, "_log", log)

    sleeps = await _run_one_cycle(
        scheduler,
        clock=_scripted_clock(20.0, 20.5),
    )

    assert sleeps == [pytest.approx(299.5)]
    assert log.infos == [
        (
            "ingestion cadence scheduler cycle",
            {
                "outcome": "active_command",
                "due_sources": 1,
                "command_status": None,
            },
        )
    ]
