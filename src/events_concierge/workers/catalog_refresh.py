"""One-shot reviewed-source catalog refresh worker.

This is intentionally a manual operational entrypoint for the registry foundation. Its mode-aware
router queues P15a's one-GET source into Temporal and delegates only legacy modes to the existing
refresh service; a future Temporal Schedule can use the same source/time-bucket run keys. Neither
this worker nor the request API signs in, RSVPs, buys tickets, or writes calendars.
"""

from __future__ import annotations

import argparse
import asyncio
from uuid import uuid4

from ..application.catalog_refresh import CatalogRefreshOutcome, CatalogRefreshResult
from ..catalog_runtime import build_catalog_container, verify_catalog_executor_database
from ..config import get_settings
from ..infra.logging import configure_logging, get_logger
from .catalog_refresh_routing import build_catalog_refresh_router

_log = get_logger(__name__)


async def refresh_once(source_key: str, run_key: str) -> CatalogRefreshResult:
    """Run one source/key through its only permitted catalog execution path (NFR-8)."""
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    container = build_catalog_container(settings)
    await verify_catalog_executor_database()
    router = await build_catalog_refresh_router(settings, container)
    result = await router.refresh(source_key, run_key)
    _log.info(
        "catalog_refresh_finished",
        source_key=source_key,
        run_key=run_key,
        outcome=result.outcome.value,
        candidate_count=result.candidate_count,
        canonical_count=result.canonical_count,
        detail=result.detail,
    )
    return result


def main() -> None:
    """Accept an explicit reviewed source key; never infer a platform-wide crawl target."""
    parser = argparse.ArgumentParser(
        description="Refresh one approved Events Concierge catalog source"
    )
    parser.add_argument("source_key", help="catalog_sources.source_key, for example luma-sf")
    parser.add_argument("--run-key", help="stable retry key; defaults to a fresh manual key")
    args = parser.parse_args()
    run_key = args.run_key or f"manual:{uuid4().hex}"
    result = asyncio.run(refresh_once(args.source_key, run_key))
    if result.outcome is CatalogRefreshOutcome.SKIPPED:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
