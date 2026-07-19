"""Outer wiring for P15a/P15b catalog-refresh Temporal start boundaries (ADR-003/005)."""

from __future__ import annotations

from temporalio.client import Client

from ..application.catalog_refresh_router import CatalogRefreshRouter
from ..composition import Container
from ..config import Settings
from ..infra.logging import get_logger
from ..workflows.claim_check import build_claim_check_data_converter
from ..workflows.start import TemporalCatalogPagedRefreshStarter, TemporalCatalogRefreshStarter

_log = get_logger(__name__)


async def build_catalog_refresh_router(
    settings: Settings, container: Container
) -> CatalogRefreshRouter:
    """Return a router that queues P15a/P15b only when shared execution dependencies exist.

    Connection failure deliberately leaves the starters absent. The router then skips P15a/P15b
    sources rather than falling back to the legacy direct service; other modes retain their bounded
    path (FR-10.3/10.4, NFR-8, ADR-003/005).
    """
    starter: TemporalCatalogRefreshStarter | None = None
    paged_starter: TemporalCatalogPagedRefreshStarter | None = None
    if settings.uses_shared_pacer_redis:
        try:
            client = await Client.connect(
                settings.temporal_target,
                namespace=settings.temporal_namespace,
                data_converter=build_claim_check_data_converter(
                    container.object_store, settings.claim_check_threshold_bytes
                ),
            )
            starter = TemporalCatalogRefreshStarter(client, settings)
            paged_starter = TemporalCatalogPagedRefreshStarter(client, settings)
        except Exception as exc:
            _log.warning(
                "temporal unavailable for single-GET catalog refresh; source will remain unqueued",
                error=str(exc),
            )
    else:
        _log.warning(
            "single-GET catalog refresh requires shared Redis Pacer; source will remain unqueued"
        )
    return CatalogRefreshRouter(
        container.catalog_source_repo,
        container.catalog_refresh,
        starter,
        paged_starter,
    )
