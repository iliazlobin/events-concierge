"""Reviewed, repository-relative code ownership for catalog refresh execution paths.

This registry is composition metadata, not dynamic tracing.  It intentionally contains no host
paths, command lines, environment values, source payloads, or secrets.  The admin adapter uses it
to point an operator at the source adapter, orchestration boundary, and worker entrypoint that own
a run's reviewed mode.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.enums import CatalogSourceMode
from ..domain.ingestion_admin import IngestionRunExecutionDescriptor

_PAGED_LEGISTAR_PROFILES = frozenset(
    {
        ("san-jose-legistar-meetings", CatalogSourceMode.SAN_JOSE_LEGISTAR),
        ("sunnyvale-legistar-meetings", CatalogSourceMode.SUNNYVALE_LEGISTAR),
        ("alameda-legistar-meetings", CatalogSourceMode.ALAMEDA_LEGISTAR),
        ("oakland-legistar-meetings", CatalogSourceMode.OAKLAND_LEGISTAR),
    }
)


@dataclass(frozen=True, slots=True)
class CatalogAdapterDescriptor:
    """One reviewed source mode's adapter ownership."""

    module: str
    symbol: str


_ADAPTERS: dict[CatalogSourceMode, CatalogAdapterDescriptor] = {
    CatalogSourceMode.PUBLIC_JSONLD: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/crawl/source.py", "PublicJsonLdSource.fetch"
    ),
    CatalogSourceMode.LIVEWHALE_JSON: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/livewhale/source.py", "LiveWhaleCatalogFetcher.fetch"
    ),
    CatalogSourceMode.SF_GOV_JSON: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/sf_gov/source.py", "SfGovCatalogFetcher.fetch"
    ),
    CatalogSourceMode.DATASF_OUR415: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/datasf/source.py", "DataSfOur415CatalogFetcher.fetch"
    ),
    CatalogSourceMode.BIBLIOCOMMONS_RSS: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/bibliocommons/source.py",
        "BiblioCommonsCatalogFetcher.fetch",
    ),
    CatalogSourceMode.SAN_JOSE_LEGISTAR: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/legistar/source.py", "LegistarCatalogFetcher.fetch_page"
    ),
    CatalogSourceMode.SUNNYVALE_LEGISTAR: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/legistar/source.py", "LegistarCatalogFetcher.fetch_page"
    ),
    CatalogSourceMode.ALAMEDA_LEGISTAR: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/legistar/source.py", "LegistarCatalogFetcher.fetch_page"
    ),
    CatalogSourceMode.OAKLAND_LEGISTAR: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/legistar/source.py", "LegistarCatalogFetcher.fetch_page"
    ),
    CatalogSourceMode.COMMUNICO_JSON: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/communico/source.py", "CommunicoCatalogFetcher.fetch"
    ),
    CatalogSourceMode.TRIBE_EVENTS_JSON: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/tribe/source.py", "TribeEventsCatalogFetcher.fetch"
    ),
    CatalogSourceMode.LOCALIST_JSON: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/localist/source.py", "LocalistCatalogFetcher.fetch"
    ),
    CatalogSourceMode.LIBCAL_ICS: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/libcal/source.py", "LibCalIcsCatalogFetcher.fetch"
    ),
    CatalogSourceMode.CIVIC_ENGAGE_RSS: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/civic_engage/source.py",
        "CivicEngageRssCatalogFetcher.fetch",
    ),
    CatalogSourceMode.MIDPEN_HTML: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/midpen/source.py", "MidpenCatalogFetcher.fetch"
    ),
    CatalogSourceMode.USFCA_HTML: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/usfca/source.py", "UsfcaCatalogFetcher.fetch"
    ),
    CatalogSourceMode.CAL_PERFORMANCES_JSON: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/calperformances/source.py",
        "CalPerformancesCatalogFetcher.fetch",
    ),
    CatalogSourceMode.BERKELEY_REP_HTML: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/berkeley_rep/source.py",
        "BerkeleyRepCatalogFetcher.fetch",
    ),
    CatalogSourceMode.YBCA_HTML: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/ybca/source.py", "YbcaCatalogFetcher.fetch"
    ),
    CatalogSourceMode.OAKLAND_HTML: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/oakland/source.py", "OaklandCatalogFetcher.fetch"
    ),
    CatalogSourceMode.LUMA_CALENDAR_JSON: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/luma_calendar/source.py",
        "LumaCalendarCatalogFetcher.fetch",
    ),
    CatalogSourceMode.LUMA_DISCOVER_JSON: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/luma_discover/source.py",
        "LumaDiscoverCatalogFetcher.fetch",
    ),
    CatalogSourceMode.MEETUP_CITY_JSONLD: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/meetup_city/source.py", "MeetupCityCatalogFetcher.fetch"
    ),
    CatalogSourceMode.MEETUP_GROUP_ICS: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/meetup_group/source.py", "MeetupGroupCalendarCatalogFetcher.fetch"
    ),
    CatalogSourceMode.TECH_WEEK_MCP: CatalogAdapterDescriptor(
        "src/events_concierge/adapters/tech_week/source.py", "TechWeekCatalogFetcher.fetch"
    ),
}


class CatalogExecutionDescriptorRegistry:
    """Resolve a reviewed source profile to its bounded operational ownership metadata."""

    def __init__(self, temporal_task_queue: str) -> None:
        if not temporal_task_queue.strip():
            raise ValueError("catalog execution task queue must not be empty")
        self._temporal_task_queue = temporal_task_queue

    def describe(
        self,
        *,
        source_key: str,
        mode: str | None,
        page_limit: int | None,
        trigger: str,
    ) -> IngestionRunExecutionDescriptor | None:
        """Return fixed code ownership, or ``None`` for fixture/unknown legacy modes."""
        if mode is None:
            return None
        try:
            source_mode = CatalogSourceMode(mode)
        except ValueError:
            return None
        adapter = _ADAPTERS.get(source_mode)
        if adapter is None:
            return None

        if source_mode is CatalogSourceMode.LIBCAL_ICS and page_limit == 1:
            return IngestionRunExecutionDescriptor(
                execution_path="temporal_single_get",
                worker_service="temporal-activity-worker",
                task_queue=self._temporal_task_queue,
                adapter_id=source_mode.value,
                adapter_module=adapter.module,
                adapter_symbol=adapter.symbol,
                orchestration_module="src/events_concierge/application/catalog_refresh.py",
                orchestration_symbol="CatalogRefreshService.refresh",
                worker_module="src/events_concierge/workflows/activities.py",
                worker_symbol="refresh_catalog_single_get",
            )
        if (source_key, source_mode) in _PAGED_LEGISTAR_PROFILES:
            return IngestionRunExecutionDescriptor(
                execution_path="temporal_paged",
                worker_service="temporal-activity-worker",
                task_queue=self._temporal_task_queue,
                adapter_id=source_mode.value,
                adapter_module=adapter.module,
                adapter_symbol=adapter.symbol,
                orchestration_module="src/events_concierge/application/catalog_paged_refresh.py",
                orchestration_symbol="PagedCatalogRefreshService.refresh_page",
                worker_module="src/events_concierge/workflows/activities.py",
                worker_symbol="refresh_catalog_paged_legistar",
            )

        admin_trigger = trigger == "admin_source"
        return IngestionRunExecutionDescriptor(
            execution_path="guarded_direct",
            worker_service=(
                "ingestion-command-worker" if admin_trigger else "catalog-refresh-worker"
            ),
            task_queue=None,
            adapter_id=source_mode.value,
            adapter_module=adapter.module,
            adapter_symbol=adapter.symbol,
            orchestration_module="src/events_concierge/application/catalog_refresh.py",
            orchestration_symbol="CatalogRefreshService.refresh",
            worker_module=(
                "src/events_concierge/workers/ingestion_commands.py"
                if admin_trigger
                else "src/events_concierge/workers/catalog_refresh.py"
            ),
            worker_symbol=("run_ingestion_commands" if admin_trigger else "refresh_once"),
        )
