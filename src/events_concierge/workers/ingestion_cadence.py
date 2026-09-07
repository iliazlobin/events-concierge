"""Local/mock-only recurring scheduler for durable ingestion cadence commands.

This process never calls an event provider. It periodically checks whether any reviewed,
non-fixture source is due and, if so, appends one deterministic ``refresh_due`` command for the
separate ingestion-command worker to execute.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Protocol

from ..application.ingestion_cadence_scheduler import (
    IngestionCadenceScheduleOutcome,
    IngestionCadenceScheduler,
    IngestionCadenceScheduleReport,
)
from ..composition import build_container
from ..config import get_settings
from ..infra.logging import configure_logging, get_logger

_log = get_logger(__name__)


class _IngestionCadenceSchedulerPort(Protocol):
    """Minimal scheduler seam used by the paced worker loop and its unit tests."""

    async def schedule_once(self) -> IngestionCadenceScheduleReport: ...


async def run_ingestion_cadence() -> None:
    """Continuously enqueue local cadence work when the explicit scheduler is enabled."""
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    if not settings.catalog_ingestion_scheduler_enabled:
        _log.info("ingestion cadence scheduler disabled")
        return
    if not settings.admin_ingestion_enabled:
        _log.warning("ingestion cadence scheduler refused without local ingestion admin")
        return
    if not settings.mock_cloud:
        # Settings already rejects this combination. Retain a runtime guard for injected settings
        # so this convenience scheduler cannot silently become a production schedule.
        _log.warning("ingestion cadence scheduler refused outside local mock mode")
        return

    container = build_container(settings)
    scheduler = IngestionCadenceScheduler(
        container.ingestion_admin_repo,
        interval_seconds=settings.catalog_ingestion_scheduler_interval_seconds,
        release_revision=settings.release_revision,
        image_digest=settings.image_digest,
    )
    _log.info(
        "ingestion cadence scheduler started",
        interval_seconds=settings.catalog_ingestion_scheduler_interval_seconds,
    )
    await _run_ingestion_cadence_loop(
        scheduler,
        minimum_cycle_seconds=settings.catalog_ingestion_scheduler_interval_seconds,
    )


async def _run_ingestion_cadence_loop(
    scheduler: _IngestionCadenceSchedulerPort,
    *,
    minimum_cycle_seconds: float,
    clock: Callable[[], float] = monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Probe at a stable start-to-start cadence and remain promptly cancellable."""
    if minimum_cycle_seconds <= 0:
        raise ValueError("ingestion cadence minimum_cycle_seconds must be positive")

    while True:
        cycle_started = clock()
        try:
            report = await scheduler.schedule_once()
        except Exception as error:
            # Database exception messages can contain operational details. Keep only the type.
            _log.warning(
                "ingestion cadence scheduler cycle failed",
                error_type=type(error).__name__,
            )
        else:
            if report.outcome is not IngestionCadenceScheduleOutcome.IDLE:
                _log.info(
                    "ingestion cadence scheduler cycle",
                    outcome=report.outcome.value,
                    due_sources=report.due_sources,
                    command_status=(
                        report.command_status.value if report.command_status is not None else None
                    ),
                )
        elapsed = max(clock() - cycle_started, 0.0)
        await sleep(max(minimum_cycle_seconds - elapsed, 0.0))


def main() -> None:
    asyncio.run(run_ingestion_cadence())


if __name__ == "__main__":
    main()
