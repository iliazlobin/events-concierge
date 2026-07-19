"""P15a catalog execution routing coverage (FR-10.3/10.4, NFR-8, ADR-003/005)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast

from events_concierge.application.catalog_refresh import (
    CatalogRefreshOutcome,
    CatalogRefreshResult,
)
from events_concierge.application.catalog_refresh_dispatcher import CatalogCadenceDispatcher
from events_concierge.application.catalog_refresh_router import CatalogRefreshRouter
from events_concierge.domain.catalog_sources import CatalogRefreshDue, CatalogSource
from events_concierge.domain.enums import CatalogSourceMode
from events_concierge.ports.catalog_sources import CatalogRefreshDueReader, CatalogSourceRepository


class _Sources:
    """Fixture source registry shared by direct router and cadence-reader seams."""

    def __init__(self, sources: dict[str, CatalogSource], due: list[CatalogRefreshDue]) -> None:
        self._sources = sources
        self._due = due

    async def get(self, source_key: str) -> CatalogSource | None:
        return self._sources.get(source_key)

    async def list_due_refreshes(self, now: datetime, *, limit: int) -> list[CatalogRefreshDue]:
        del now
        return list(self._due[:limit])


class _DirectRunner:
    """Record direct-path effects without exposing an HTTP adapter."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
        self.calls.append((source_key, run_key))
        return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.SUCCEEDED)


class _Starter:
    """Record only the opaque source/run Temporal start intent."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def start(self, source_key: str, run_key: str) -> None:
        self.calls.append((source_key, run_key))


def _source(
    source_key: str,
    now: datetime,
    *,
    mode: CatalogSourceMode = CatalogSourceMode.PUBLIC_JSONLD,
    page_limit: int = 1,
) -> CatalogSource:
    """Build one reviewed source with a router-relevant request shape."""
    return CatalogSource(
        source_key=source_key,
        display_name=f"{source_key} events",
        publisher="Tests",
        seed_url=f"https://{source_key}.example.test/events",
        approved_origins=(f"https://{source_key}.example.test",),
        region="bay_area_9_county",
        mode=mode,
        enabled=True,
        reviewed_at=now - timedelta(days=1),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1_500,
        page_limit=page_limit,
    )


def _due(source: CatalogSource, due_at: datetime) -> CatalogRefreshDue:
    """Build one stable cadence slot for a reviewed source."""
    return CatalogRefreshDue(
        source=source,
        due_at=due_at,
        last_succeeded_at=due_at - timedelta(minutes=source.refresh_interval_minutes),
    )


async def test_cadence_router_queues_single_get_and_directly_runs_only_legacy_source() -> None:
    """A mixed due batch cannot send P15a's LibCal GET through the direct runner (ADR-003/005)."""
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    single_get = _source(
        "libcal-source",
        now,
        mode=CatalogSourceMode.LIBCAL_ICS,
    )
    legacy = _source("legacy-source", now)
    sources = _Sources(
        {single_get.source_key: single_get, legacy.source_key: legacy},
        [_due(single_get, now - timedelta(minutes=2)), _due(legacy, now - timedelta(minutes=1))],
    )
    direct = _DirectRunner()
    starter = _Starter()
    router = CatalogRefreshRouter(
        cast(CatalogSourceRepository, sources),
        direct,
        starter,
    )

    report = await CatalogCadenceDispatcher(
        cast(CatalogRefreshDueReader, sources),
        router,
        now=lambda: now,
    ).dispatch_once()

    assert starter.calls == [
        (single_get.source_key, _due(single_get, now - timedelta(minutes=2)).run_key())
    ]
    assert direct.calls == [(legacy.source_key, _due(legacy, now - timedelta(minutes=1)).run_key())]
    assert [result.outcome for result in report.results] == [
        CatalogRefreshOutcome.QUEUED,
        CatalogRefreshOutcome.SUCCEEDED,
    ]
    assert report.failures == ()


async def test_router_skips_single_get_without_a_temporal_starter_and_never_falls_back() -> None:
    """Missing shared-Temporal wiring makes zero direct source calls (FR-10.3, NFR-8)."""
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    single_get = _source(
        "libcal-source",
        now,
        mode=CatalogSourceMode.LIBCAL_ICS,
    )
    sources = _Sources({single_get.source_key: single_get}, [])
    direct = _DirectRunner()
    router = CatalogRefreshRouter(cast(CatalogSourceRepository, sources), direct, None)

    result = await router.refresh(single_get.source_key, "manual:no-temporal")

    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert result.detail == "single-GET catalog workflow starter is unavailable"
    assert direct.calls == []


async def test_router_queues_exact_paged_legistar_profiles_without_a_direct_fallback() -> None:
    """P15b/P15c/P15d/P15e profiles never take the legacy one-shot fetch path (ADR-003/005)."""
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    san_jose = _source(
        "san-jose-legistar-meetings",
        now,
        mode=CatalogSourceMode.SAN_JOSE_LEGISTAR,
        page_limit=5,
    )
    sunnyvale = _source(
        "sunnyvale-legistar-meetings",
        now,
        mode=CatalogSourceMode.SUNNYVALE_LEGISTAR,
        page_limit=5,
    )
    alameda = _source(
        "alameda-legistar-meetings",
        now,
        mode=CatalogSourceMode.ALAMEDA_LEGISTAR,
        page_limit=5,
    )
    oakland = _source(
        "oakland-legistar-meetings",
        now,
        mode=CatalogSourceMode.OAKLAND_LEGISTAR,
        page_limit=5,
    )
    legacy = _source("legacy-source", now)
    sources = _Sources(
        {
            san_jose.source_key: san_jose,
            sunnyvale.source_key: sunnyvale,
            alameda.source_key: alameda,
            oakland.source_key: oakland,
            legacy.source_key: legacy,
        },
        [],
    )
    direct = _DirectRunner()
    paged_starter = _Starter()
    router = CatalogRefreshRouter(
        cast(CatalogSourceRepository, sources),
        direct,
        None,
        paged_starter,
    )

    san_jose_queued = await router.refresh(san_jose.source_key, "manual:san-jose-paged")
    sunnyvale_queued = await router.refresh(sunnyvale.source_key, "manual:sunnyvale-paged")
    alameda_queued = await router.refresh(alameda.source_key, "manual:alameda-paged")
    oakland_queued = await router.refresh(oakland.source_key, "manual:oakland-paged")
    legacy_result = await router.refresh(legacy.source_key, "manual:legacy")

    assert san_jose_queued.outcome is CatalogRefreshOutcome.QUEUED
    assert sunnyvale_queued.outcome is CatalogRefreshOutcome.QUEUED
    assert alameda_queued.outcome is CatalogRefreshOutcome.QUEUED
    assert oakland_queued.outcome is CatalogRefreshOutcome.QUEUED
    assert paged_starter.calls == [
        (san_jose.source_key, "manual:san-jose-paged"),
        (sunnyvale.source_key, "manual:sunnyvale-paged"),
        (alameda.source_key, "manual:alameda-paged"),
        (oakland.source_key, "manual:oakland-paged"),
    ]
    assert legacy_result.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert direct.calls == [(legacy.source_key, "manual:legacy")]


async def test_router_skips_paged_profile_when_temporal_start_is_unavailable() -> None:
    """Missing P15b wiring means zero San Jose source calls, never a direct legacy fallback."""
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    paged = _source(
        "san-jose-legistar-meetings",
        now,
        mode=CatalogSourceMode.SAN_JOSE_LEGISTAR,
    )
    direct = _DirectRunner()
    router = CatalogRefreshRouter(
        cast(CatalogSourceRepository, _Sources({paged.source_key: paged}, [])),
        direct,
        None,
        None,
    )

    result = await router.refresh(paged.source_key, "manual:no-paged-temporal")

    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert result.detail == "paged catalog workflow starter is unavailable"
    assert direct.calls == []
