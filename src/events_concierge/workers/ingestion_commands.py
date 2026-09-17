"""Durable command worker for the ingestion administration surface.

The browser/API edge only appends commands. This process claims one command at a time and delegates
one eligible source through the guarded catalog-refresh router before yielding its continuation.
It runs with a separate executor database role and is inert unless the local admin or explicit
production executor setting is enabled.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Protocol

from ..adapters.postgres.command_investigation import CommandInvestigationStore
from ..application.ingestion_command_execution import ResumableIngestionCommandProcessor
from ..catalog_runtime import build_catalog_container, verify_catalog_executor_database
from ..config import get_settings
from ..deployment.startup import preflight_catalog_runtime
from ..domain.ingestion_admin import IngestionProcessReport
from ..infra.db import dispose_engine
from ..infra.logging import configure_logging, get_logger
from .catalog_refresh_routing import build_catalog_refresh_router

_log = get_logger(__name__)


class _IngestionCommandProcessor(Protocol):
    """Minimal service seam needed by the paced worker loop and its unit tests."""

    async def process_once(self, limit: int = 10) -> IngestionProcessReport: ...


async def run_ingestion_commands() -> None:
    """Drain durable ingestion commands under the isolated executor role when enabled."""
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    if not settings.admin_ingestion_enabled and not settings.ingestion_executor_enabled:
        _log.info("ingestion command worker disabled")
        return
    if not settings.mock_cloud and not settings.ingestion_executor_enabled:
        raise ValueError("non-mock command execution requires explicit executor enablement")
    preflight_catalog_runtime(settings)
    container = build_catalog_container(settings)
    try:
        await verify_catalog_executor_database()
        router = await build_catalog_refresh_router(settings, container)
        service = ResumableIngestionCommandProcessor(
            container.ingestion_admin_repo,
            router,
            CommandInvestigationStore(),
            lease_seconds=settings.catalog_ingestion_command_lease_seconds,
            cadence_batch_size=settings.catalog_refresh_dispatch_batch_size,
            release_revision=settings.release_revision,
            image_digest=settings.image_digest,
        )
        _log.info(
            "ingestion command worker started",
            batch_size=settings.catalog_ingestion_command_batch_size,
            cadence_batch_size=settings.catalog_refresh_dispatch_batch_size,
            lease_seconds=settings.catalog_ingestion_command_lease_seconds,
            poll_seconds=settings.catalog_ingestion_command_poll_seconds,
            execution_model="one_source_per_claim",
        )
        await _run_ingestion_command_loop(
            service,
            batch_size=settings.catalog_ingestion_command_batch_size,
            minimum_cycle_seconds=settings.catalog_ingestion_command_poll_seconds,
        )
    finally:
        await dispose_engine()


async def _run_ingestion_command_loop(
    processor: _IngestionCommandProcessor,
    *,
    batch_size: int,
    minimum_cycle_seconds: float,
    clock: Callable[[], float] = monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Run bounded passes at a stable start-to-start cadence and always yield to cancellation."""
    if batch_size < 1:
        raise ValueError("ingestion command batch_size must be positive")
    if minimum_cycle_seconds <= 0:
        raise ValueError("ingestion command minimum_cycle_seconds must be positive")

    while True:
        cycle_started = clock()
        try:
            report = await processor.process_once(limit=batch_size)
        except Exception as error:
            # Exception strings can contain database/provider details. The durable command service
            # records only closed error codes, so this process log retains the exception class only.
            _log.warning(
                "ingestion command worker cycle failed",
                error_type=type(error).__name__,
            )
        else:
            if report.claimed:
                _log.info("ingestion command worker cycle", **_report_fields(report))
        elapsed = max(clock() - cycle_started, 0.0)
        await sleep(max(minimum_cycle_seconds - elapsed, 0.0))


def _report_fields(report: IngestionProcessReport) -> dict[str, int]:
    """Keep cycle summaries aggregate; the processor emits correlated execution events."""
    return {
        "claimed": report.claimed,
        "completed": report.completed,
        "deferred": report.deferred,
        "failed": report.failed,
        "lost_leases": report.lost_leases,
    }


def main() -> None:
    asyncio.run(run_ingestion_commands())


if __name__ == "__main__":
    main()
