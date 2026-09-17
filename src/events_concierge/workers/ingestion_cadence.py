"""Durable ingestion cadence: local recurring loop or deployment-owned one-shot schedule.

This process never calls an event provider. It periodically checks whether any reviewed,
non-fixture source is due and, if so, appends one deterministic ``refresh_due`` command for the
separate ingestion-command worker to execute.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Protocol

from ..adapters.postgres.ingestion_admin import PostgresIngestionAdminRepository
from ..application.catalog_execution_descriptors import CatalogExecutionDescriptorRegistry
from ..application.ingestion_cadence_scheduler import (
    IngestionCadenceScheduleOutcome,
    IngestionCadenceScheduler,
    IngestionCadenceScheduleReport,
)
from ..config import get_settings
from ..deployment.startup import preflight_operator_runtime
from ..infra.logging import configure_logging, get_logger
from ..infra.operator_database import OperatorDatabase

_log = get_logger(__name__)


class _IngestionCadenceSchedulerPort(Protocol):
    """Minimal scheduler seam used by the paced worker loop and its unit tests."""

    async def schedule_once(self) -> IngestionCadenceScheduleReport: ...


async def run_ingestion_cadence(*, once: bool = False) -> None:
    """Enqueue one scheduled pass, or run the explicit local recurring loop."""
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    if not settings.catalog_ingestion_scheduler_enabled:
        _log.info("ingestion cadence scheduler disabled")
        return
    if not settings.mock_cloud and not once:
        raise ValueError("production cadence is deployment-owned; use --once")
    if settings.mock_cloud and not settings.admin_ingestion_enabled:
        raise ValueError("local cadence requires explicit local ingestion administration")
    preflight_operator_runtime(settings)
    database = OperatorDatabase(settings)
    repository = PostgresIngestionAdminRepository(
        CatalogExecutionDescriptorRegistry(settings.temporal_catalog_queue),
        session_scope=database.session_scope,
    )
    scheduler = IngestionCadenceScheduler(
        repository,
        interval_seconds=settings.catalog_ingestion_scheduler_interval_seconds,
        release_revision=settings.release_revision,
        image_digest=settings.image_digest,
    )
    _log.info(
        "ingestion cadence scheduler started",
        interval_seconds=settings.catalog_ingestion_scheduler_interval_seconds,
    )
    try:
        if once:
            report = await scheduler.schedule_once()
            _log.info(
                "ingestion cadence pass",
                outcome=report.outcome.value,
                due_sources=report.due_sources,
            )
        else:
            await _run_ingestion_cadence_loop(
                scheduler,
                minimum_cycle_seconds=settings.catalog_ingestion_scheduler_interval_seconds,
            )
    finally:
        await database.aclose()


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
    parser = argparse.ArgumentParser(description="Enqueue one durable due-source cadence command")
    parser.add_argument("--once", action="store_true", help="one deployment-owned cadence pass")
    args = parser.parse_args()
    asyncio.run(run_ingestion_cadence(once=args.once))


if __name__ == "__main__":
    main()
