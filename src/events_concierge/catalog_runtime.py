"""Catalog-only composition for durable ingestion executors and catalog Temporal workers.

The catalog graph needs PostgreSQL, Redis pacing, reviewed public fetchers, and Temporal's opaque
payload storage. It has no tenant identity, Calendar, credentials, notification, or RSVP adapters.
The full consumer/transactional preflight remains a separate unchanged launch requirement.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .adapters.mock.object_store import MockFilesystemObjectStore
from .adapters.postgres.catalog import PostgresCatalogRepository
from .adapters.postgres.catalog_entity_social_links import PostgresCatalogEntitySocialLinks
from .adapters.postgres.catalog_observations import PostgresCatalogObservationRepository
from .adapters.postgres.catalog_paged_promotion import PostgresCatalogPagedRefreshPromoter
from .adapters.postgres.catalog_refresh_commit import PostgresCatalogRefreshCommitter
from .adapters.postgres.catalog_sources import PostgresCatalogSourceRepository
from .adapters.postgres.ingestion_admin import PostgresIngestionAdminRepository
from .adapters.ranking.embedding import DeterministicEmbedding
from .application.catalog_execution_descriptors import CatalogExecutionDescriptorRegistry
from .application.catalog_paged_refresh import PagedCatalogRefreshService
from .application.catalog_refresh import CatalogRefreshService
from .composition import (
    _build_pacer,
    _build_policy_graph,
    build_catalog_fetchers,
)
from .config import Settings
from .deployment.gcp_runtime import build_gcs_object_store
from .infra.db import init_engine, system_session_scope
from .infra.operator_database import validate_operator_database_url, verify_database_role
from .operations.network_safety import is_non_remote_host
from .ports.object_store import ObjectStorePort

_DEVELOPMENT_RUNTIME_FACTORY = "events_concierge.deployment.development_runtime:build_runtime_ports"


@dataclass(slots=True)
class CatalogContainer:
    settings: Settings
    catalog_source_repo: PostgresCatalogSourceRepository
    ingestion_admin_repo: PostgresIngestionAdminRepository
    catalog_refresh: CatalogRefreshService
    catalog_paged_refresh: PagedCatalogRefreshService
    object_store: ObjectStorePort


def build_catalog_container(settings: Settings) -> CatalogContainer:
    """Build only reviewed catalog capabilities with a dedicated executor login."""
    url = validate_operator_database_url(settings, settings.ingestion_executor_database_url)
    development = settings.env == "development" and settings.mock_cloud
    if development:
        _validate_development_catalog(settings)
    shared_storage = not settings.mock_cloud or development
    if shared_storage and not settings.ingestion_executor_enabled:
        raise ValueError("deployed catalog execution requires explicit executor enablement")
    if shared_storage and not settings.uses_shared_pacer_redis:
        raise ValueError("deployed catalog execution requires shared Redis pacing")
    # The target catalog profile needs GCS only, not the full product provider factory.
    # Private development mocks external product integrations, but independently deployed
    # catalog/command workers still need one shared payload store.
    if not shared_storage:
        object_store: ObjectStorePort = MockFilesystemObjectStore(
            Path(settings.claim_check_local_root) / "catalog",
        )
    else:
        if not settings.gcs_claim_check_prefix.startswith("events-concierge/catalog/"):
            raise ValueError(
                "catalog execution requires a separate events-concierge/catalog/ storage prefix"
            )
        object_store = build_gcs_object_store(settings)
    init_engine(
        url,
        pool_size=settings.database_pool_size,
        max_overflow=0,
        pool_timeout_seconds=settings.database_pool_timeout_seconds,
        pool_recycle_seconds=settings.database_pool_recycle_seconds,
        work_mem=settings.database_work_mem,
    )
    catalog = PostgresCatalogRepository(DeterministicEmbedding())
    observations = PostgresCatalogObservationRepository()
    sources = PostgresCatalogSourceRepository()
    committer = PostgresCatalogRefreshCommitter(
        catalog, observations, PostgresCatalogEntitySocialLinks()
    )
    promoter = PostgresCatalogPagedRefreshPromoter(catalog, observations)
    _, _, gate, quarantine = _build_policy_graph(settings, None, None, None)
    pacer = _build_pacer(settings)
    _, fetchers, paged_fetchers = build_catalog_fetchers(settings)
    return CatalogContainer(
        settings=settings,
        catalog_source_repo=sources,
        ingestion_admin_repo=PostgresIngestionAdminRepository(
            CatalogExecutionDescriptorRegistry(settings.temporal_catalog_queue),
        ),
        catalog_refresh=CatalogRefreshService(
            sources,
            committer,
            fetchers,
            pacer,
            gate,
            quarantine,
            sources,
            collection_windows=sources,
            lease_seconds=settings.catalog_refresh_lease_seconds,
        ),
        catalog_paged_refresh=PagedCatalogRefreshService(
            sources,
            sources,
            promoter,
            paged_fetchers,
            pacer,
            gate,
            quarantine,
            sources,
            collection_windows=sources,
            lease_seconds=settings.catalog_refresh_lease_seconds,
        ),
        object_store=object_store,
    )


def _validate_development_catalog(settings: Settings) -> None:
    if settings.runtime_provider_factory != _DEVELOPMENT_RUNTIME_FACTORY:
        raise ValueError(
            "development catalog execution requires the explicit development runtime factory"
        )
    # Catch local defaults before creating any database pool or GCS client. Redis connections
    # remain lazy, and the existing pacer fails throttle-first if the service becomes unavailable.
    redis = urlsplit(settings.redis_url)
    if redis.scheme not in {"redis", "rediss"} or is_non_remote_host(redis.hostname):
        raise ValueError("development catalog execution requires a shared Redis service URL")


async def verify_catalog_executor_database() -> None:
    """Do not claim work until the connected principal proves its isolated executor role."""
    async with system_session_scope() as session:
        await verify_database_role(session, "ec_ingestion_executor")
