"""One-shot, bounded catalog-cadence dispatcher (FR-3.3/FR-3.9, NFR-1/NFR-8).

This command only evaluates durable due slots and delegates each one through the mode-aware refresh
router. It is not a recurring Temporal Schedule and does not enable source policy or make an
implicit network call: the refresh boundary remains policy-gated and fail-closed by default.
"""

from __future__ import annotations

import asyncio

from ..application.catalog_refresh import CatalogRefreshOutcome
from ..application.catalog_refresh_dispatcher import (
    CatalogCadenceDispatcher,
    CatalogCadenceDispatchReport,
)
from ..catalog_runtime import build_catalog_container, verify_catalog_executor_database
from ..config import get_settings
from ..deployment.startup import preflight_catalog_runtime
from ..infra.logging import configure_logging, get_logger
from .catalog_refresh_routing import build_catalog_refresh_router

_log = get_logger(__name__)


async def dispatch_once() -> CatalogCadenceDispatchReport:
    """Run one bounded source-cadence pass and log only safe operational identifiers."""
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    preflight_catalog_runtime(settings)
    container = build_catalog_container(settings)
    await verify_catalog_executor_database()
    router = await build_catalog_refresh_router(settings, container)
    report = await CatalogCadenceDispatcher(
        container.catalog_source_repo,
        router,
        batch_size=settings.catalog_refresh_dispatch_batch_size,
    ).dispatch_once()
    _log.info(
        "catalog_cadence_dispatch_finished",
        due_sources=report.due_sources,
        attempted=report.attempted,
        succeeded=report.outcome_count(CatalogRefreshOutcome.SUCCEEDED),
        queued=report.outcome_count(CatalogRefreshOutcome.QUEUED),
        skipped=report.outcome_count(CatalogRefreshOutcome.SKIPPED),
        deferred=report.outcome_count(CatalogRefreshOutcome.DEFERRED),
        already_succeeded=report.outcome_count(CatalogRefreshOutcome.ALREADY_SUCCEEDED),
        busy=report.outcome_count(CatalogRefreshOutcome.BUSY),
        failed=len(report.failures),
        source_keys=[result.source_key for result in report.results],
        failed_source_keys=[failure.source_key for failure in report.failures],
        failure_types=[failure.error_type for failure in report.failures],
    )
    return report


def main() -> None:
    """Expose the dispatch seam for an explicit local/ops invocation."""
    asyncio.run(dispatch_once())


if __name__ == "__main__":
    main()
