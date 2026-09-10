"""Outer wiring for P15a/P15b catalog-refresh Temporal start boundaries (ADR-003/005)."""

from __future__ import annotations

from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as DatabaseTimeoutError

from ..application.catalog_refresh import CatalogRefreshResult
from ..application.catalog_refresh_dispatcher import CatalogRefreshRunner
from ..application.catalog_refresh_router import CatalogRefreshRouter
from ..catalog_runtime import CatalogContainer
from ..composition import Container
from ..config import Settings
from ..infra.logging import get_logger
from ..ports.ingestion_command_execution import CommandExecutionUncertainError
from ..workflows.start import TemporalCatalogPagedRefreshStarter, TemporalCatalogRefreshStarter
from ..workflows.temporal_client import connect_temporal

_log = get_logger(__name__)


async def build_catalog_refresh_router(
    settings: Settings, container: Container | CatalogContainer
) -> CatalogRefreshRunner:
    """Return a router that queues P15a/P15b only when shared execution dependencies exist.

    Connection failure deliberately leaves the starters absent. The router then skips P15a/P15b
    sources rather than falling back to the legacy direct service; other modes retain their bounded
    path (FR-10.3/10.4, NFR-8, ADR-003/005).
    """
    starter: TemporalCatalogRefreshStarter | None = None
    paged_starter: TemporalCatalogPagedRefreshStarter | None = None
    if settings.uses_shared_pacer_redis:
        try:
            client = await connect_temporal(
                settings,
                container.object_store,
                catalog_only=isinstance(container, CatalogContainer),
            )
            starter = TemporalCatalogRefreshStarter(client, settings)
            paged_starter = TemporalCatalogPagedRefreshStarter(client, settings)
        except Exception as exc:
            _log.warning(
                "temporal unavailable for single-GET catalog refresh; source will remain unqueued",
                error_type=type(exc).__name__,
            )
    else:
        _log.warning(
            "single-GET catalog refresh requires shared Redis Pacer; source will remain unqueued"
        )
    router = CatalogRefreshRouter(
        container.catalog_source_repo,
        container.catalog_refresh,
        starter,
        paged_starter,
    )
    return _PersistenceAwareRouter(router)


class _PersistenceAwareRouter:
    """Translate database-specific uncertainty at the composition boundary."""

    def __init__(self, router: CatalogRefreshRunner) -> None:
        self._router = router

    async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
        try:
            return await self._router.refresh(source_key, run_key)
        except (
            DisconnectionError,
            InterfaceError,
            OperationalError,
            DatabaseTimeoutError,
        ) as error:
            raise CommandExecutionUncertainError(
                "catalog persistence acknowledgement uncertain"
            ) from error
