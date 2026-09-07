"""Current-observation catalog browse repository and API coverage."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from events_concierge.adapters.postgres.catalog import PostgresCatalogRepository
from events_concierge.adapters.postgres.catalog_observations import (
    PostgresCatalogObservationRepository,
)
from events_concierge.adapters.postgres.catalog_refresh_commit import (
    PostgresCatalogRefreshCommitter,
)
from events_concierge.adapters.postgres.catalog_sources import PostgresCatalogSourceRepository
from events_concierge.adapters.ranking.embedding import DeterministicEmbedding
from events_concierge.api.app import create_app
from events_concierge.domain.catalog_browse import CatalogBrowseCursor
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import (
    CatalogSourceMode,
    PriceStatus,
    RegistrationStatus,
    Source,
)
from events_concierge.domain.events import CandidateEvent, EventEntityProfile, GeoPoint
from events_concierge.domain.request import RequestConstraints

pytestmark = pytest.mark.integration


def _candidate(
    source_event_id: str,
    title: str,
    start_at: datetime,
    *,
    source: Source = Source.PUBLIC_JSONLD,
    lat: float = 37.7749,
    lon: float = -122.4194,
) -> CandidateEvent:
    return CandidateEvent(
        source=source,
        source_event_id=source_event_id,
        title=title,
        start_at=start_at,
        registration_url=f"https://luma.com/{source_event_id}",
        end_at=start_at + timedelta(hours=2),
        venue_name="Luma browse venue",
        geo=GeoPoint(lat, lon),
        city="San Francisco",
        description=f"Current Luma catalog event {title}",
        organizer_name="Luma Browse",
        entity_profiles=(
            EventEntityProfile(
                name="Luma Browse",
                role="organizer",
                kind="organization",
                profile_url="https://www.linkedin.com/company/luma-browse",
            ),
        ),
    )


async def _seed_source(source: CatalogSource) -> None:
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.catalog_sources
                        (source_key, display_name, publisher, seed_url, approved_origins, region,
                         mode, handoff_only, enabled, reviewed_at, review_expires_at,
                         refresh_interval_minutes, min_interval_ms, page_limit)
                    VALUES
                        (:source_key, :display_name, :publisher, :seed_url, :approved_origins, :region,
                         :mode, true, true, :reviewed_at, NULL, 360, 1500, 1)
                    """
                ),
                {
                    "source_key": source.source_key,
                    "display_name": source.display_name,
                    "publisher": source.publisher,
                    "seed_url": source.seed_url,
                    "approved_origins": list(source.approved_origins),
                    "region": source.region,
                    "mode": source.mode.value,
                    "reviewed_at": source.reviewed_at,
                },
            )
    finally:
        await owner.dispose()


async def _source_with_current_events(
    *,
    fixture: bool = False,
    event_count: int = 3,
) -> tuple[
    str,
    list[CandidateEvent],
    CandidateEvent,
    PostgresCatalogRepository,
]:
    tag = uuid4().hex
    source_key = f"{'test-' if fixture else ''}browse-luma-{tag}"
    now = datetime.now(UTC).replace(microsecond=0)
    source = CatalogSource(
        source_key=source_key,
        display_name=f"Luma calendar {tag[:8]}",
        publisher="Catalog Browse Integration",
        seed_url=f"https://luma.com/calendar-{tag}",
        approved_origins=("https://luma.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=now,
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
    )
    await _seed_source(source)

    catalog = PostgresCatalogRepository(DeterministicEmbedding())
    observations = PostgresCatalogObservationRepository()
    committer = PostgresCatalogRefreshCommitter(catalog, observations)
    runs = PostgresCatalogSourceRepository()
    stale = _candidate(f"stale-{tag}", "Stale prior-run Luma event", now + timedelta(hours=1))
    current = [
        _candidate(
            f"current-{index}-{tag}",
            f"Current Luma event {index} {tag}",
            now + timedelta(hours=(index * 4) + 2),
            lat=37.77 + (index / 100),
            lon=-122.42 + (index / 100),
        )
        for index in range(event_count)
    ]

    first_run = f"manual:browse-first-{tag}"
    first_claim = await runs.claim_refresh(source_key, first_run, lease_seconds=300)
    assert first_claim.lease_token is not None
    assert (
        await committer.commit_refresh(
            source_key,
            first_run,
            lease_token=first_claim.lease_token,
            candidates=[stale, current[0]],
        )
        is not None
    )

    latest_run = f"manual:browse-latest-{tag}"
    latest_claim = await runs.claim_refresh(source_key, latest_run, lease_seconds=300)
    assert latest_claim.lease_token is not None
    assert (
        await committer.commit_refresh(
            source_key,
            latest_run,
            lease_token=latest_claim.lease_token,
            candidates=current,
        )
        is not None
    )
    return source_key, current, stale, catalog


async def test_repository_browses_latest_nonfixture_source_observations_by_keyset(
    db: None,
) -> None:
    source_key, current, stale, catalog = await _source_with_current_events()
    synthetic = _candidate(
        f"synthetic-{uuid4().hex}",
        "Synthetic Luma row without reviewed provenance",
        current[0].start_at - timedelta(minutes=30),
        source=Source.LUMA,
    )
    synthetic_event = (await catalog.upsert_candidates([synthetic]))[0]

    first_page, providers = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=2,
    )
    assert [item.canonical_event.title for item in first_page] == [
        current[0].title,
        current[1].title,
    ]
    selected_provider = next(
        provider for provider in providers if provider.source_key == source_key
    )
    assert selected_provider.display_name.startswith("Luma calendar ")
    assert selected_provider.publisher == "Catalog Browse Integration"
    assert selected_provider.provider == "luma"
    assert selected_provider.seed_url.startswith("https://luma.com/calendar-")
    assert selected_provider.event_count == 3
    assert any(provider.event_count == 0 for provider in providers)
    assert first_page[0].canonical_event.geo == current[0].geo
    assert first_page[0].canonical_event.entity_profiles == current[0].entity_profiles
    assert first_page[0].sources[0].source_key == source_key
    assert first_page[0].sources[0].provider == "luma"
    assert first_page[0].sources[0].source is Source.PUBLIC_JSONLD

    after_event = first_page[-1].canonical_event
    second_page, repeated_providers = await catalog.browse_current(
        source_keys=(source_key,),
        after=CatalogBrowseCursor(
            after_event.start_at,
            after_event.canonical_event_id,
        ),
        limit=2,
    )
    assert [item.canonical_event.title for item in second_page] == [current[2].title]
    assert repeated_providers == providers

    latest_page, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=2,
        sort="latest",
    )
    assert [item.canonical_event.title for item in latest_page] == [
        current[2].title,
        current[1].title,
    ]
    latest_tail = latest_page[-1].canonical_event
    earlier_page, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=CatalogBrowseCursor(
            latest_tail.start_at,
            latest_tail.canonical_event_id,
        ),
        limit=2,
        sort="latest",
    )
    assert [item.canonical_event.title for item in earlier_page] == [current[0].title]
    visible_ids = {item.canonical_event.canonical_event_id for item in [*first_page, *second_page]}
    stale_event = await catalog.retrieve(
        constraints=RequestConstraints(),
        intent_embedding=None,
        limit=10_000,
    )
    stale_id = next(item.canonical_event_id for item in stale_event if item.title == stale.title)
    assert stale_id not in visible_ids
    assert synthetic_event.canonical_event_id not in visible_ids

    fixture_source_key, _, _, _ = await _source_with_current_events(
        fixture=True,
        event_count=1,
    )
    fixture_items, fixture_providers = await catalog.browse_current(
        source_keys=(fixture_source_key,),
        after=None,
        limit=10,
    )
    assert fixture_items == []
    assert fixture_source_key not in {provider.source_key for provider in fixture_providers}


async def test_city_facets_cover_the_catalog_beyond_the_first_result_page(
    db: None,
) -> None:
    _, current, _, catalog = await _source_with_current_events(event_count=4)

    facets = await catalog.list_city_facets()
    counts = {facet.city: facet.event_count for facet in facets}

    assert counts["sanfrancisco"] >= len(current)


async def test_explicit_history_window_is_authoritative_through_repository_and_api(
    db: None,
) -> None:
    tag = uuid4().hex
    source_key = f"browse-history-{tag}"
    now = datetime.now(UTC).replace(microsecond=0)
    await _seed_source(
        CatalogSource(
            source_key=source_key,
            display_name=f"Historical Luma calendar {tag[:8]}",
            publisher="Catalog Browse History Integration",
            seed_url=f"https://luma.com/history-{tag}",
            approved_origins=("https://luma.com",),
            region="bay_area_9_county",
            mode=CatalogSourceMode.PUBLIC_JSONLD,
            enabled=True,
            reviewed_at=now,
            review_expires_at=None,
            refresh_interval_minutes=360,
            min_interval_ms=1_500,
        )
    )
    historical = [
        _candidate(
            f"history-{index}-{tag}",
            f"Historical Luma event {index} {tag}",
            now - timedelta(days=4 - index),
        )
        for index in range(3)
    ]
    future = _candidate(
        f"history-future-{tag}",
        f"Future Luma event {tag}",
        now + timedelta(days=1),
    )
    catalog = PostgresCatalogRepository(DeterministicEmbedding())
    runs = PostgresCatalogSourceRepository()
    run_key = f"manual:browse-history-{tag}"
    claim = await runs.claim_refresh(source_key, run_key, lease_seconds=300)
    assert claim.lease_token is not None
    committer = PostgresCatalogRefreshCommitter(
        catalog,
        PostgresCatalogObservationRepository(),
    )
    assert (
        await committer.commit_refresh(
            source_key,
            run_key,
            lease_token=claim.lease_token,
            candidates=[*historical, future],
        )
        is not None
    )

    live_items, live_providers = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
    )
    assert [item.canonical_event.title for item in live_items] == [future.title]
    assert (
        next(
            provider.event_count for provider in live_providers if provider.source_key == source_key
        )
        == 1
    )

    window_start = now - timedelta(days=5)
    window_end = now - timedelta(hours=1)
    first_page, history_providers = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=2,
        starts_after=window_start,
        starts_before=window_end,
    )
    assert [item.canonical_event.title for item in first_page] == [
        historical[0].title,
        historical[1].title,
    ]
    assert (
        next(
            provider.event_count
            for provider in history_providers
            if provider.source_key == source_key
        )
        == 3
    )

    page_tail = first_page[-1].canonical_event
    second_page, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=CatalogBrowseCursor(
            start_at=page_tail.start_at,
            canonical_event_id=page_tail.canonical_event_id,
        ),
        limit=2,
        starts_after=window_start,
        starts_before=window_end,
    )
    assert [item.canonical_event.title for item in second_page] == [historical[2].title]
    first_ids = {item.canonical_event.canonical_event_id for item in first_page}
    second_ids = {item.canonical_event.canonical_event_id for item in second_page}
    assert first_ids.isdisjoint(second_ids)

    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        onboard = await client.post(
            "/v1/onboard",
            json={"notify_email": f"browse-history-{tag}@example.com"},
        )
        headers = {"X-EC-Tenant-ID": onboard.json()["tenant_id"]}
        history_response = await client.get(
            "/v1/catalog/events",
            params={
                "source_key": source_key,
                "starts_after": window_start.isoformat(),
                "starts_before": window_end.isoformat(),
            },
            headers=headers,
        )
        live_response = await client.get(
            "/v1/catalog/events",
            params={"source_key": source_key},
            headers=headers,
        )

    assert history_response.status_code == 200
    assert [item["title"] for item in history_response.json()["items"]] == [
        event.title for event in historical
    ]
    assert (
        next(
            provider["event_count"]
            for provider in history_response.json()["providers"]
            if provider["source_key"] == source_key
        )
        == 3
    )
    assert live_response.status_code == 200
    assert [item["title"] for item in live_response.json()["items"]] == [future.title]


async def test_browse_window_includes_ongoing_excludes_ended_and_preserves_cursor(
    db: None,
) -> None:
    """Window eligibility uses interval overlap; pages remain ordered by event start."""
    tag = uuid4().hex
    source_key = f"browse-overlap-{tag}"
    now = datetime.now(UTC).replace(microsecond=0)
    await _seed_source(
        CatalogSource(
            source_key=source_key,
            display_name=f"Overlap Luma calendar {tag[:8]}",
            publisher="Catalog Browse Overlap Integration",
            seed_url=f"https://luma.com/overlap-{tag}",
            approved_origins=("https://luma.com",),
            region="bay_area_9_county",
            mode=CatalogSourceMode.PUBLIC_JSONLD,
            enabled=True,
            reviewed_at=now,
            review_expires_at=None,
            refresh_interval_minutes=360,
            min_interval_ms=1_500,
        )
    )
    ended = replace(
        _candidate(
            f"ended-{tag}",
            f"Ended before window {tag}",
            now - timedelta(hours=3),
        ),
        end_at=now - timedelta(hours=1),
    )
    ongoing = replace(
        _candidate(
            f"ongoing-{tag}",
            f"Ongoing across window start {tag}",
            now - timedelta(minutes=30),
        ),
        end_at=now + timedelta(minutes=30),
    )
    future = _candidate(
        f"future-after-overlap-{tag}",
        f"Future after ongoing event {tag}",
        now + timedelta(hours=1),
    )
    catalog = PostgresCatalogRepository(DeterministicEmbedding())
    runs = PostgresCatalogSourceRepository()
    claim = await runs.claim_refresh(
        source_key,
        f"manual:browse-overlap-{tag}",
        lease_seconds=300,
    )
    assert claim.lease_token is not None
    committer = PostgresCatalogRefreshCommitter(
        catalog,
        PostgresCatalogObservationRepository(),
    )
    assert (
        await committer.commit_refresh(
            source_key,
            f"manual:browse-overlap-{tag}",
            lease_token=claim.lease_token,
            candidates=[ended, ongoing, future],
        )
        is not None
    )

    window_end = now + timedelta(hours=3)
    first_page, providers = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=1,
        starts_after=now,
        starts_before=window_end,
    )
    assert [item.canonical_event.title for item in first_page] == [ongoing.title]
    assert ended.title not in {item.canonical_event.title for item in first_page}
    assert (
        next(provider.event_count for provider in providers if provider.source_key == source_key)
        == 2
    )

    page_tail = first_page[0].canonical_event
    second_page, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=CatalogBrowseCursor(
            start_at=page_tail.start_at,
            canonical_event_id=page_tail.canonical_event_id,
        ),
        limit=1,
        starts_after=now,
        starts_before=window_end,
    )
    assert [item.canonical_event.title for item in second_page] == [future.title]
    assert first_page[0].canonical_event.canonical_event_id != (
        second_page[0].canonical_event.canonical_event_id
    )


@dataclass(frozen=True, slots=True)
class _RetainedHistoryScenario:
    tag: str
    source_key: str
    now: datetime
    rolled_past: CandidateEvent
    rolled_future: CandidateEvent
    current_future: CandidateEvent
    ancient: CandidateEvent
    catalog: PostgresCatalogRepository
    first_run: str
    latest_run: str


async def _retained_history_scenario() -> _RetainedHistoryScenario:
    tag = uuid4().hex
    source_key = f"browse-retained-{tag}"
    now = datetime.now(UTC).replace(microsecond=0)
    await _seed_source(
        CatalogSource(
            source_key=source_key,
            display_name=f"Retained Luma calendar {tag[:8]}",
            publisher="Catalog Retained History Integration",
            seed_url=f"https://luma.com/retained-{tag}",
            approved_origins=("https://luma.com",),
            region="bay_area_9_county",
            mode=CatalogSourceMode.PUBLIC_JSONLD,
            enabled=True,
            reviewed_at=now,
            review_expires_at=None,
            refresh_interval_minutes=360,
            min_interval_ms=1_500,
        )
    )
    rolled_past = _candidate(
        f"retained-past-{tag}",
        f"Retained past event {tag}",
        now - timedelta(days=2),
    )
    rolled_future = _candidate(
        f"rolled-future-{tag}",
        f"Rolled-off future event {tag}",
        now + timedelta(days=1),
    )
    current_future = _candidate(
        f"current-future-{tag}",
        f"Latest future event {tag}",
        now + timedelta(days=2),
    )
    ancient = _candidate(
        f"retained-ancient-{tag}",
        f"Retained archive event {tag}",
        now - timedelta(days=8 * 365),
    )
    catalog = PostgresCatalogRepository(DeterministicEmbedding())
    observations = PostgresCatalogObservationRepository()
    committer = PostgresCatalogRefreshCommitter(catalog, observations)
    runs = PostgresCatalogSourceRepository()

    first_run = f"manual:browse-retained-first-{tag}"
    first_claim = await runs.claim_refresh(source_key, first_run, lease_seconds=300)
    assert first_claim.lease_token is not None
    assert (
        await committer.commit_refresh(
            source_key,
            first_run,
            lease_token=first_claim.lease_token,
            candidates=[rolled_past, rolled_future, current_future, ancient],
        )
        is not None
    )

    latest_run = f"manual:browse-retained-latest-{tag}"
    latest_claim = await runs.claim_refresh(source_key, latest_run, lease_seconds=300)
    assert latest_claim.lease_token is not None
    assert (
        await committer.commit_refresh(
            source_key,
            latest_run,
            lease_token=latest_claim.lease_token,
            candidates=[current_future],
        )
        is not None
    )

    return _RetainedHistoryScenario(
        tag=tag,
        source_key=source_key,
        now=now,
        rolled_past=rolled_past,
        rolled_future=rolled_future,
        current_future=current_future,
        ancient=ancient,
        catalog=catalog,
        first_run=first_run,
        latest_run=latest_run,
    )


async def test_retained_past_rolloff_is_visible_without_weakening_future_or_facets(
    db: None,
) -> None:
    """Past uses retained last-known identities; future still requires the latest crawl."""
    scenario = await _retained_history_scenario()

    mixed_start = scenario.now - timedelta(days=3)
    mixed_end = scenario.now + timedelta(days=3)
    mixed, mixed_providers = await scenario.catalog.browse_current(
        source_keys=(scenario.source_key,),
        after=None,
        limit=10,
        starts_after=mixed_start,
        starts_before=mixed_end,
    )
    assert [item.canonical_event.title for item in mixed] == [
        scenario.rolled_past.title,
        scenario.current_future.title,
    ]
    assert scenario.rolled_future.title not in {item.canonical_event.title for item in mixed}
    assert mixed[0].sources[0].refresh_run_key == scenario.first_run
    assert mixed[1].sources[0].refresh_run_key == scenario.latest_run
    assert (
        next(
            provider.event_count
            for provider in mixed_providers
            if provider.source_key == scenario.source_key
        )
        == 2
    )

    live, live_providers = await scenario.catalog.browse_current(
        source_keys=(scenario.source_key,),
        after=None,
        limit=10,
    )
    assert [item.canonical_event.title for item in live] == [scenario.current_future.title]
    assert (
        next(
            provider.event_count
            for provider in live_providers
            if provider.source_key == scenario.source_key
        )
        == 1
    )

    empty, empty_providers = await scenario.catalog.browse_current(
        source_keys=(scenario.source_key,),
        after=None,
        limit=10,
        starts_after=scenario.now - timedelta(days=300),
        starts_before=scenario.now - timedelta(days=290),
    )
    assert empty == []
    assert (
        next(
            provider.event_count
            for provider in empty_providers
            if provider.source_key == scenario.source_key
        )
        == 0
    )

    archive_start = scenario.now - timedelta(days=9 * 365)
    archive_end = scenario.now - timedelta(days=7 * 365)
    archive, archive_providers = await scenario.catalog.browse_current(
        source_keys=(scenario.source_key,),
        after=None,
        limit=10,
        starts_after=archive_start,
        starts_before=archive_end,
    )
    assert [item.canonical_event.title for item in archive] == [scenario.ancient.title]
    assert [(provider.source_key, provider.event_count) for provider in archive_providers] == [
        (scenario.source_key, 1)
    ]
    with pytest.raises(ValueError, match="catalog browse window is invalid"):
        await scenario.catalog.browse_current(
            source_keys=(),
            after=None,
            limit=10,
            starts_after=archive_start,
            starts_before=archive_end,
        )


async def test_source_archive_range_is_available_through_api_but_unscoped_range_is_bounded(
    db: None,
) -> None:
    scenario = await _retained_history_scenario()
    archive_start = scenario.now - timedelta(days=9 * 365)
    archive_end = scenario.now - timedelta(days=7 * 365)
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        onboard = await client.post(
            "/v1/onboard",
            json={"notify_email": f"browse-retained-api-{scenario.tag}@example.com"},
        )
        headers = {"X-EC-Tenant-ID": onboard.json()["tenant_id"]}
        selected_archive = await client.get(
            "/v1/catalog/events",
            params={
                "source_key": scenario.source_key,
                "starts_after": archive_start.isoformat(),
                "starts_before": archive_end.isoformat(),
            },
            headers=headers,
        )
        unscoped_archive = await client.get(
            "/v1/catalog/events",
            params={
                "starts_after": archive_start.isoformat(),
                "starts_before": archive_end.isoformat(),
            },
            headers=headers,
        )

    assert selected_archive.status_code == 200
    assert [item["title"] for item in selected_archive.json()["items"]] == [scenario.ancient.title]
    assert selected_archive.json()["providers"] == [
        {
            "source_key": scenario.source_key,
            "label": f"Retained Luma calendar {scenario.tag[:8]}",
            "display_name": f"Retained Luma calendar {scenario.tag[:8]}",
            "publisher": "Catalog Retained History Integration",
            "provider": "luma",
            "seed_url": f"https://luma.com/retained-{scenario.tag}",
            "event_count": 1,
        }
    ]
    assert unscoped_archive.status_code == 422


async def test_current_browse_uses_refreshed_fields_when_stable_source_identity_is_rescheduled(
    db: None,
) -> None:
    """A latest successful refresh can move an elapsed source identity back into the live map."""
    tag = uuid4().hex
    source_key = f"browse-reschedule-{tag}"
    now = datetime.now(UTC).replace(microsecond=0)
    await _seed_source(
        CatalogSource(
            source_key=source_key,
            display_name=f"Rescheduled Luma calendar {tag[:8]}",
            publisher="Catalog Browse Reschedule Integration",
            seed_url=f"https://luma.com/reschedule-{tag}",
            approved_origins=("https://luma.com",),
            region="bay_area_9_county",
            mode=CatalogSourceMode.PUBLIC_JSONLD,
            enabled=True,
            reviewed_at=now,
            review_expires_at=None,
            refresh_interval_minutes=360,
            min_interval_ms=1_500,
        )
    )
    catalog = PostgresCatalogRepository(DeterministicEmbedding())
    observations = PostgresCatalogObservationRepository()
    committer = PostgresCatalogRefreshCommitter(catalog, observations)
    runs = PostgresCatalogSourceRepository()
    source_event_id = f"rescheduled-current-{tag}"
    initial = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=source_event_id,
        title=f"Original Luma occurrence {tag}",
        start_at=now - timedelta(days=1),
        end_at=now - timedelta(days=1) + timedelta(hours=1),
        registration_url=f"https://luma.com/{source_event_id}",
        venue_name="Original Luma venue",
        geo=GeoPoint(37.7749, -122.4194),
        city="San Francisco",
        description="Original occurrence that is now elapsed.",
    )
    first_run = f"manual:browse-reschedule-first-{tag}"
    first_claim = await runs.claim_refresh(source_key, first_run, lease_seconds=300)
    assert first_claim.lease_token is not None
    assert (
        await committer.commit_refresh(
            source_key,
            first_run,
            lease_token=first_claim.lease_token,
            candidates=[initial],
        )
        is not None
    )
    first_observation = (await observations.list_for_source(source_key))[0]

    moved_start = now + timedelta(days=6)
    moved = CandidateEvent(
        source=initial.source,
        source_event_id=initial.source_event_id,
        title=f"Renamed future Luma occurrence {tag}",
        start_at=moved_start,
        end_at=moved_start + timedelta(hours=2),
        registration_url=initial.registration_url,
        venue_name="Future Luma venue",
        geo=GeoPoint(37.3382, -121.8863),
        city="San Jose",
        description="The organizer moved this occurrence into the future.",
    )
    latest_run = f"manual:browse-reschedule-latest-{tag}"
    latest_claim = await runs.claim_refresh(source_key, latest_run, lease_seconds=300)
    assert latest_claim.lease_token is not None
    assert (
        await committer.commit_refresh(
            source_key,
            latest_run,
            lease_token=latest_claim.lease_token,
            candidates=[moved],
        )
        is not None
    )

    latest_observation = (await observations.list_for_source(source_key))[0]
    items, providers = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
    )

    assert latest_observation.canonical_event_id == first_observation.canonical_event_id
    assert latest_observation.first_seen_at == first_observation.first_seen_at
    assert latest_observation.last_run_key == latest_run
    assert len(items) == 1
    event = items[0].canonical_event
    assert event.canonical_event_id == first_observation.canonical_event_id
    assert event.title == moved.title
    assert event.start_at == moved.start_at
    assert event.end_at == moved.end_at
    assert event.venue_name == moved.venue_name
    assert event.geo == moved.geo
    assert event.city_norm == "sanjose"
    assert items[0].sources[0].refresh_run_key == latest_run
    selected_provider = next(
        provider for provider in providers if provider.source_key == source_key
    )
    assert selected_provider.provider == "luma"
    assert selected_provider.event_count == 1


async def test_catalog_browse_filters_before_selecting_the_keyset_page(db: None) -> None:
    source_key, current, _, catalog = await _source_with_current_events(event_count=4)

    windowed, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=1,
        starts_after=current[1].start_at - timedelta(seconds=1),
        starts_before=current[2].start_at + timedelta(seconds=1),
    )
    assert [item.canonical_event.title for item in windowed] == [current[1].title]

    searched, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=1,
        starts_after=current[0].start_at - timedelta(seconds=1),
        starts_before=current[2].start_at + timedelta(seconds=1),
        query="event 2",
        city="San Francisco",
        price="unknown",
    )
    assert [item.canonical_event.title for item in searched] == [current[2].title]

    no_city_match, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        city="Oakland",
    )
    assert no_city_match == []

    free = replace(
        current[0],
        city="Oakland",
        geo=GeoPoint(37.8044, -122.2712),
        price_status=PriceStatus.FREE,
        description="Outdoor beach volleyball clinic.",
    )
    manhattan_paid = replace(
        current[1],
        city="New York",
        geo=GeoPoint(40.7505, -73.9934),
        price_status=PriceStatus.PAID,
        price_min_cents=1_000,
        price_max_cents=2_000,
        price_currency="usd",
        description="Indie game showcase, fighting game tournament, and Smash Melee.",
    )
    enriched = replace(
        current[2],
        price_status=PriceStatus.PAID,
        price_min_cents=2_500,
        price_max_cents=5_000,
        price_currency="usd",
        organizer_name="Needle Organizer",
        host_names=("Needle Host",),
        speaker_names=("Needle Speaker",),
        partner_names=("Needle Partner",),
        entity_profiles=(),
        description="Hands-on generative AI workshop for software teams.",
    )
    committer = PostgresCatalogRefreshCommitter(
        catalog,
        PostgresCatalogObservationRepository(),
    )
    runs = PostgresCatalogSourceRepository()
    role_run = f"manual:browse-roles-{uuid4().hex}"
    role_claim = await runs.claim_refresh(source_key, role_run, lease_seconds=300)
    assert role_claim.lease_token is not None
    assert (
        await committer.commit_refresh(
            source_key,
            role_run,
            lease_token=role_claim.lease_token,
            candidates=[free, manhattan_paid, enriched, current[3]],
        )
        is not None
    )
    for query in (
        "Needle Organizer",
        "Needle Host",
        "Needle Speaker",
        "Needle Partner",
    ):
        people_match, _ = await catalog.browse_current(
            source_keys=(source_key,),
            after=None,
            limit=1,
            query=query,
        )
        assert [item.canonical_event.title for item in people_match] == [enriched.title]
        event = people_match[0].canonical_event
        assert (
            event.price_min_cents,
            event.price_max_cents,
            event.price_currency,
        ) == (2_500, 5_000, "USD")

    city_matches, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        cities=("Oakland", "San Francisco"),
    )
    assert [item.canonical_event.title for item in city_matches] == [
        free.title,
        enriched.title,
        current[3].title,
    ]

    manhattan_matches, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        location_scopes=("manhattan",),
    )
    assert [item.canonical_event.title for item in manhattan_matches] == [manhattan_paid.title]

    bay_area_matches, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        location_scopes=("bay_area",),
    )
    assert [item.canonical_event.title for item in bay_area_matches] == [
        free.title,
        enriched.title,
        current[3].title,
    ]

    ai_workshops, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        topics=("ai", "workshop"),
    )
    assert [item.canonical_event.title for item in ai_workshops] == [enriched.title]
    assert ai_workshops[0].canonical_event.topics == ("ai", "technology", "workshop")

    gaming, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        topics=("gaming",),
    )
    assert [item.canonical_event.title for item in gaming] == [manhattan_paid.title]

    topic_facets = await catalog.list_topic_facets(
        source_keys=(source_key,),
        starts_after=None,
        starts_before=None,
        query=None,
        cities=(),
        location_scopes=(),
        price=None,
        price_max_cents=None,
    )
    counts = {facet.topic: facet.event_count for facet in topic_facets}
    assert counts["volleyball"] == 1
    assert counts["sports"] == 1
    assert counts["gaming"] == 1
    assert counts["ai"] == 1

    under_ceiling, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        price_max_cents=2_500,
    )
    assert [item.canonical_event.title for item in under_ceiling] == [
        free.title,
        manhattan_paid.title,
    ]

    paid_under_ceiling, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        price="paid",
        price_max_cents=2_500,
    )
    assert [item.canonical_event.title for item in paid_under_ceiling] == [manhattan_paid.title]

    free_with_ignored_ceiling, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        price="free",
        price_max_cents=2_500,
    )
    assert [item.canonical_event.title for item in free_with_ignored_ceiling] == [free.title]

    with pytest.raises(ValueError, match="maximum price"):
        await catalog.browse_current(
            source_keys=(source_key,),
            after=None,
            limit=10,
            price="unknown",
            price_max_cents=2_500,
        )


async def test_registration_availability_filters_before_paging_and_facets(db: None) -> None:
    source_key, current, _, catalog = await _source_with_current_events(event_count=4)
    sold_out = replace(
        current[0],
        registration_status=RegistrationStatus.SOLD_OUT,
        description="Indie game showcase and fighting game tournament.",
    )
    available = replace(
        current[2],
        registration_status=RegistrationStatus.OPEN,
        description="Hands-on generative AI workshop.",
    )
    waitlist = replace(
        current[3],
        registration_status=RegistrationStatus.WAITLIST,
        description="Outdoor beach volleyball clinic.",
    )
    committer = PostgresCatalogRefreshCommitter(
        catalog,
        PostgresCatalogObservationRepository(),
    )
    runs = PostgresCatalogSourceRepository()
    run_key = f"manual:browse-availability-{uuid4().hex}"
    claim = await runs.claim_refresh(source_key, run_key, lease_seconds=300)
    assert claim.lease_token is not None
    assert await committer.commit_refresh(
        source_key,
        run_key,
        lease_token=claim.lease_token,
        candidates=[sold_out, current[1], available, waitlist],
    ) is not None

    open_page, _ = await catalog.browse_current(
        source_keys=(source_key,), after=None, limit=1, availability="available"
    )
    sold_out_page, _ = await catalog.browse_current(
        source_keys=(source_key,), after=None, limit=1, availability="sold_out"
    )
    assert [item.canonical_event.title for item in open_page] == [available.title]
    assert [item.canonical_event.title for item in sold_out_page] == [sold_out.title]

    sold_out_facets = await catalog.list_topic_facets(
        source_keys=(source_key,),
        starts_after=None,
        starts_before=None,
        query=None,
        cities=(),
        location_scopes=(),
        price=None,
        price_max_cents=None,
        availability="sold_out",
    )
    sold_out_counts = {facet.topic: facet.event_count for facet in sold_out_facets}
    assert sold_out_counts["gaming"] == 1
    assert sold_out_counts["ai"] == 0

    sold_out_days = await catalog.list_day_facets(
        source_keys=(source_key,),
        starts_after=None,
        starts_before=None,
        query=None,
        cities=(),
        location_scopes=(),
        price=None,
        price_max_cents=None,
        availability="sold_out",
        time_zone="America/Los_Angeles",
    )
    assert sum(day.event_count for day in sold_out_days) == 1


async def test_source_selection_is_a_union_and_price_bounds_describe_a_band(db: None) -> None:
    """Several sources widen the page; a price floor narrows it from below.

    Source selection and price selection compose in opposite directions, so they are asserted
    together: adding a source may only add events, and adding a floor may only remove them.
    """
    left_key, left_events, _, catalog = await _source_with_current_events(event_count=1)
    right_key, right_events, _, _ = await _source_with_current_events(event_count=1)

    only_left, _ = await catalog.browse_current(
        source_keys=(left_key,), after=None, limit=10
    )
    only_right, _ = await catalog.browse_current(
        source_keys=(right_key,), after=None, limit=10
    )
    both, _ = await catalog.browse_current(
        source_keys=(left_key, right_key), after=None, limit=10
    )
    left_titles = {item.canonical_event.title for item in only_left}
    right_titles = {item.canonical_event.title for item in only_right}
    both_titles = {item.canonical_event.title for item in both}
    assert left_events[0].title in left_titles
    assert right_events[0].title in right_titles
    assert left_titles.isdisjoint(right_titles)
    assert both_titles == left_titles | right_titles

    # Repeating a selection is the same request, not a doubled one.
    repeated, _ = await catalog.browse_current(
        source_keys=(left_key, left_key), after=None, limit=10
    )
    assert {item.canonical_event.title for item in repeated} == left_titles

    priced_key, priced, _, priced_catalog = await _source_with_current_events(event_count=3)
    cheap = replace(
        priced[0],
        price_status=PriceStatus.PAID,
        price_min_cents=1_000,
        price_max_cents=1_000,
        price_currency="USD",
    )
    middle = replace(
        priced[1],
        price_status=PriceStatus.PAID,
        price_min_cents=2_500,
        price_max_cents=4_000,
        price_currency="USD",
    )
    free = replace(priced[2], price_status=PriceStatus.FREE)
    runs = PostgresCatalogSourceRepository()
    committer = PostgresCatalogRefreshCommitter(
        priced_catalog, PostgresCatalogObservationRepository()
    )
    run_key = f"manual:browse-price-band-{uuid4().hex}"
    claim = await runs.claim_refresh(priced_key, run_key, lease_seconds=300)
    assert claim.lease_token is not None
    assert (
        await committer.commit_refresh(
            priced_key,
            run_key,
            lease_token=claim.lease_token,
            candidates=[cheap, middle, free],
        )
        is not None
    )

    at_least, _ = await priced_catalog.browse_current(
        source_keys=(priced_key,), after=None, limit=10, price_min_cents=2_500
    )
    assert [item.canonical_event.title for item in at_least] == [middle.title]

    band, _ = await priced_catalog.browse_current(
        source_keys=(priced_key,),
        after=None,
        limit=10,
        price_min_cents=500,
        price_max_cents=1_500,
    )
    assert [item.canonical_event.title for item in band] == [cheap.title]

    exact, _ = await priced_catalog.browse_current(
        source_keys=(priced_key,),
        after=None,
        limit=10,
        price_min_cents=1_000,
        price_max_cents=1_000,
    )
    assert [item.canonical_event.title for item in exact] == [cheap.title]

    # A ceiling admits free events because nothing is below it; a floor cannot.
    under_ceiling, _ = await priced_catalog.browse_current(
        source_keys=(priced_key,), after=None, limit=10, price_max_cents=1_500
    )
    assert free.title in {item.canonical_event.title for item in under_ceiling}
    assert free.title not in {item.canonical_event.title for item in at_least}

    with pytest.raises(ValueError, match="minimum price"):
        await priced_catalog.browse_current(
            source_keys=(priced_key,), after=None, limit=10, price="free", price_min_cents=1_000
        )
    with pytest.raises(ValueError, match="minimum price"):
        await priced_catalog.browse_current(
            source_keys=(priced_key,),
            after=None,
            limit=10,
            price_min_cents=4_000,
            price_max_cents=1_000,
        )


async def test_chess_is_indexed_as_a_specific_and_board_game_parent_topic(db: None) -> None:
    source_key, current, _, catalog = await _source_with_current_events(event_count=1)
    chess = replace(
        current[0],
        title="Chess at Alamo Square Park",
        description="Friendly regular chess and exciting Bughouse games. Bring a board.",
    )
    runs = PostgresCatalogSourceRepository()
    committer = PostgresCatalogRefreshCommitter(
        catalog,
        PostgresCatalogObservationRepository(),
    )
    run_key = f"manual:browse-chess-topics-{uuid4().hex}"
    claim = await runs.claim_refresh(source_key, run_key, lease_seconds=300)
    assert claim.lease_token is not None
    assert (
        await committer.commit_refresh(
            source_key,
            run_key,
            lease_token=claim.lease_token,
            candidates=[chess],
        )
        is not None
    )

    chess_matches, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        topics=("chess",),
    )
    board_games, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        topics=("board-games",),
    )
    facets = await catalog.list_topic_facets(
        source_keys=(source_key,),
        starts_after=None,
        starts_before=None,
        query=None,
        cities=(),
        location_scopes=(),
        price=None,
        price_max_cents=None,
    )

    assert [item.canonical_event.title for item in chess_matches] == [chess.title]
    assert [item.canonical_event.title for item in board_games] == [chess.title]
    assert chess_matches[0].canonical_event.topics == ("board-games", "chess")
    counts = {facet.topic: facet.event_count for facet in facets}
    assert counts["board-games"] == 1
    assert counts["chess"] == 1
    assert counts["music"] == 0


async def test_topic_filter_scans_past_first_101_current_events_without_temp_oid_failure(
    db: None,
) -> None:
    """Broad single and AND-topic filters can advance beyond the first bounded base scan."""
    source_key, current, _, catalog = await _source_with_current_events(event_count=103)
    late_volleyball = replace(
        current[101],
        description="Outdoor beach volleyball clinic and skills workshop.",
    )
    late_gaming = replace(
        current[102],
        description="Indie game showcase, fighting game tournament, and Super Smash Bros.",
    )
    revised = [*current[:101], late_volleyball, late_gaming]
    runs = PostgresCatalogSourceRepository()
    committer = PostgresCatalogRefreshCommitter(
        catalog,
        PostgresCatalogObservationRepository(),
    )
    run_key = f"manual:browse-topic-second-scan-{uuid4().hex}"
    claim = await runs.claim_refresh(source_key, run_key, lease_seconds=300)
    assert claim.lease_token is not None
    assert (
        await committer.commit_refresh(
            source_key,
            run_key,
            lease_token=claim.lease_token,
            candidates=revised,
        )
        is not None
    )

    gaming, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        topics=("gaming",),
    )
    sports, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        topics=("sports",),
    )
    volleyball, _ = await catalog.browse_current(
        source_keys=(source_key,),
        after=None,
        limit=10,
        topics=("sports", "volleyball"),
    )

    assert [item.canonical_event.title for item in gaming] == [late_gaming.title]
    assert [item.canonical_event.title for item in sports] == [late_volleyball.title]
    assert [item.canonical_event.title for item in volleyball] == [late_volleyball.title]
    assert set(volleyball[0].canonical_event.topics) >= {"sports", "volleyball"}


async def test_catalog_browse_api_pages_and_binds_cursor_to_source_filter(db: None) -> None:
    source_key, current, _, _ = await _source_with_current_events(event_count=2)
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        onboard = await client.post(
            "/v1/onboard",
            json={"notify_email": f"browse-{uuid4().hex}@example.com"},
        )
        headers = {"X-EC-Tenant-ID": onboard.json()["tenant_id"]}

        first = await client.get(
            "/v1/catalog/events",
            params={"source_key": source_key, "limit": 1},
            headers=headers,
        )
        assert first.status_code == 200
        first_body = first.json()
        assert first_body["next_cursor"] is not None
        selected_facets = [
            provider for provider in first_body["providers"] if provider["source_key"] == source_key
        ]
        assert selected_facets == [
            {
                "source_key": source_key,
                "label": first_body["items"][0]["calendar_labels"][0],
                "display_name": first_body["items"][0]["calendar_labels"][0],
                "publisher": "Catalog Browse Integration",
                "provider": "luma",
                "seed_url": f"https://luma.com/calendar-{source_key.rsplit('-', 1)[-1]}",
                "event_count": 2,
            }
        ]
        assert any(provider["event_count"] == 0 for provider in first_body["providers"])
        assert any(
            facet["city"] == "sanfrancisco" and facet["event_count"] >= 2
            for facet in first_body["city_facets"]
        )
        item = first_body["items"][0]
        assert item["title"] == current[0].title
        assert item["score"] is None
        assert item["conflict"] == "not_evaluated"
        assert item["registerable"] is None
        assert item["providers"] == ["luma"]
        assert item["source_keys"] == [source_key]
        assert current[0].geo is not None
        assert item["latitude"] == current[0].geo.lat
        assert item["longitude"] == current[0].geo.lon
        assert item["entity_profiles"] == [
            {
                "name": "Luma Browse",
                "role": "organizer",
                "kind": "organization",
                "profile_url": "https://www.linkedin.com/company/luma-browse",
            }
        ]
        assert {
            "source": "public_jsonld",
            "source_key": source_key,
            "provider": "luma",
            "registration_url": current[0].registration_url,
        }.items() <= item["sources"][0].items()

        second = await client.get(
            "/v1/catalog/events",
            params={
                "source_key": source_key,
                "limit": 1,
                "cursor": first_body["next_cursor"],
            },
            headers=headers,
        )
        assert second.status_code == 200
        assert [item["title"] for item in second.json()["items"]] == [current[1].title]
        assert second.json()["next_cursor"] is None

        wrong_scope = await client.get(
            "/v1/catalog/events",
            params={"cursor": first_body["next_cursor"]},
            headers=headers,
        )
        malformed = await client.get(
            "/v1/catalog/events",
            params={"source_key": source_key, "cursor": "not-a-valid-cursor"},
            headers=headers,
        )
        changed_filter = await client.get(
            "/v1/catalog/events",
            params={
                "source_key": source_key,
                "cursor": first_body["next_cursor"],
                "q": "different filter",
            },
            headers=headers,
        )
        changed_availability = await client.get(
            "/v1/catalog/events",
            params={
                "source_key": source_key,
                "cursor": first_body["next_cursor"],
                "availability": "sold_out",
            },
            headers=headers,
        )
        invalid_availability = await client.get(
            "/v1/catalog/events",
            params={"source_key": source_key, "availability": "maybe"},
            headers=headers,
        )
        incomplete_window = await client.get(
            "/v1/catalog/events",
            params={"source_key": source_key, "starts_after": current[0].start_at.isoformat()},
            headers=headers,
        )
        additive_cities = await client.get(
            "/v1/catalog/events",
            params=[
                ("source_key", source_key),
                ("city", "Oakland"),
                ("city", "San Francisco"),
            ],
            headers=headers,
        )
        bay_area_scope = await client.get(
            "/v1/catalog/events",
            params={"source_key": source_key, "location_scope": "bay_area"},
            headers=headers,
        )
        invalid_unknown_ceiling = await client.get(
            "/v1/catalog/events",
            params={
                "source_key": source_key,
                "price": "unknown",
                "price_max_cents": 2_500,
            },
            headers=headers,
        )
        unauthenticated = await client.get(
            "/v1/catalog/events",
            params={"source_key": source_key},
        )
        assert wrong_scope.status_code == 422
        assert malformed.status_code == 422
        assert changed_filter.status_code == 422
        assert changed_availability.status_code == 422
        assert invalid_availability.status_code == 422
        assert incomplete_window.status_code == 422
        assert [item["title"] for item in additive_cities.json()["items"]] == [
            current[0].title,
            current[1].title,
        ]
        assert [item["title"] for item in bay_area_scope.json()["items"]] == [
            current[0].title,
            current[1].title,
        ]
        assert invalid_unknown_ceiling.status_code == 422
        assert unauthenticated.status_code == 401


async def test_catalog_browse_api_pages_latest_first_and_binds_cursor_to_sort(db: None) -> None:
    source_key, current, _, _ = await _source_with_current_events(event_count=2)
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        onboard = await client.post(
            "/v1/onboard",
            json={"notify_email": f"browse-sort-{uuid4().hex}@example.com"},
        )
        headers = {"X-EC-Tenant-ID": onboard.json()["tenant_id"]}
        first = await client.get(
            "/v1/catalog/events",
            params={"source_key": source_key, "limit": 1, "sort": "latest"},
            headers=headers,
        )
        assert first.status_code == 200
        first_body = first.json()
        assert [item["title"] for item in first_body["items"]] == [current[1].title]
        assert first_body["next_cursor"] is not None

        second = await client.get(
            "/v1/catalog/events",
            params={
                "source_key": source_key,
                "limit": 1,
                "sort": "latest",
                "cursor": first_body["next_cursor"],
            },
            headers=headers,
        )
        wrong_sort = await client.get(
            "/v1/catalog/events",
            params={
                "source_key": source_key,
                "limit": 1,
                "cursor": first_body["next_cursor"],
            },
            headers=headers,
        )

    assert second.status_code == 200
    assert [item["title"] for item in second.json()["items"]] == [current[0].title]
    assert second.json()["next_cursor"] is None
    assert wrong_sort.status_code == 422


async def test_catalog_browse_api_unions_additive_date_ranges_and_binds_cursor(db: None) -> None:
    source_key, current, _, _ = await _source_with_current_events(event_count=3)
    first_window = (
        current[0].start_at - timedelta(seconds=1),
        current[0].start_at + timedelta(seconds=1),
    )
    last_window = (
        current[2].start_at - timedelta(seconds=1),
        current[2].start_at + timedelta(seconds=1),
    )
    ranges = [
        f"{first_window[0].isoformat()}..{first_window[1].isoformat()}",
        f"{last_window[0].isoformat()}..{last_window[1].isoformat()}",
    ]
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        onboard = await client.post(
            "/v1/onboard",
            json={"notify_email": f"browse-ranges-{uuid4().hex}@example.com"},
        )
        headers = {"X-EC-Tenant-ID": onboard.json()["tenant_id"]}
        first = await client.get(
            "/v1/catalog/events",
            params=[
                ("source_key", source_key),
                ("limit", "1"),
                *(("date_range", value) for value in reversed(ranges)),
            ],
            headers=headers,
        )
        first_body = first.json()
        second = await client.get(
            "/v1/catalog/events",
            params=[
                ("source_key", source_key),
                ("limit", "1"),
                ("cursor", first_body["next_cursor"]),
                *(("date_range", value) for value in ranges),
            ],
            headers=headers,
        )
        changed_scope = await client.get(
            "/v1/catalog/events",
            params=[
                ("source_key", source_key),
                ("limit", "1"),
                ("cursor", first_body["next_cursor"]),
                ("date_range", ranges[0]),
            ],
            headers=headers,
        )

    assert first.status_code == 200
    assert [item["title"] for item in first_body["items"]] == [current[0].title]
    assert first_body["next_cursor"] is not None
    selected_provider = next(
        provider for provider in first_body["providers"] if provider["source_key"] == source_key
    )
    assert selected_provider["event_count"] == 2
    assert second.status_code == 200
    assert [item["title"] for item in second.json()["items"]] == [current[2].title]
    assert second.json()["next_cursor"] is None
    assert changed_scope.status_code == 422


async def _paged_catalog_events(
    client: AsyncClient,
    headers: dict[str, str],
    params: list[tuple[str, str]],
) -> list[dict[str, object]]:
    """Read a whole range the way the calendar used to: one keyset page at a time."""
    items: list[dict[str, object]] = []
    cursor: str | None = None
    for _ in range(50):
        page = await client.get(
            "/v1/catalog/events",
            params=[*params, *([("cursor", cursor)] if cursor else [])],
            headers=headers,
        )
        assert page.status_code == 200, page.text
        body = page.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            return items
    raise AssertionError("the paged browse did not terminate")


async def test_day_summary_agrees_with_the_paged_page_it_replaces(db: None) -> None:
    """The grid is served by an aggregate, so it must count exactly what paging counts."""
    source_key, _, _, _ = await _source_with_current_events(event_count=5)
    zone = "America/Los_Angeles"
    window_start = datetime.now(UTC) - timedelta(days=1)
    window_end = window_start + timedelta(days=30)
    date_range = f"{window_start.isoformat()}..{window_end.isoformat()}"
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        onboard = await client.post(
            "/v1/onboard",
            json={"notify_email": f"summary-{uuid4().hex}@example.com"},
        )
        headers = {"X-EC-Tenant-ID": onboard.json()["tenant_id"]}
        shared = [("source_key", source_key), ("date_range", date_range)]

        paged = await _paged_catalog_events(client, headers, [*shared, ("limit", "1")])
        summary = await client.get(
            "/v1/catalog/events/summary",
            params=[*shared, ("time_zone", zone)],
            headers=headers,
        )

    assert summary.status_code == 200, summary.text
    body = summary.json()
    assert body["time_zone"] == zone

    expected_days: dict[str, set[str]] = {}
    expected_topics: dict[str, dict[str, set[str]]] = {}
    for item in paged:
        start_at = datetime.fromisoformat(str(item["start_at"]))
        day = start_at.astimezone(ZoneInfo(zone)).date().isoformat()
        canonical_id = str(item["canonical_event_id"])
        expected_days.setdefault(day, set()).add(canonical_id)
        topics = [str(topic) for topic in (item.get("topics") or [])] or ["other"]
        for topic in topics:
            expected_topics.setdefault(day, {}).setdefault(topic, set()).add(canonical_id)

    assert expected_days, "the fixture must produce at least one day to compare"
    assert {day["start_day"] for day in body["days"]} == set(expected_days)
    assert body["total_event_count"] == sum(len(ids) for ids in expected_days.values())
    for day in body["days"]:
        start_day = str(day["start_day"])
        assert day["event_count"] == len(expected_days[start_day])
        assert {
            topic["topic"]: topic["event_count"] for topic in day["topics"]
        } == {
            topic: len(ids) for topic, ids in expected_topics[start_day].items()
        }
        # A day's total counts distinct events, so a multi-topic event is counted
        # once there and once per topic beside it.
        assert all(topic["event_count"] <= day["event_count"] for topic in day["topics"])


async def test_day_summary_buckets_by_the_requested_zone_and_rejects_an_invalid_one(
    db: None,
) -> None:
    source_key, _, _, _ = await _source_with_current_events(event_count=3)
    window_start = datetime.now(UTC) - timedelta(days=1)
    date_range = f"{window_start.isoformat()}..{(window_start + timedelta(days=30)).isoformat()}"
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        onboard = await client.post(
            "/v1/onboard",
            json={"notify_email": f"zone-{uuid4().hex}@example.com"},
        )
        headers = {"X-EC-Tenant-ID": onboard.json()["tenant_id"]}
        shared = [("source_key", source_key), ("date_range", date_range)]

        pacific = await client.get(
            "/v1/catalog/events/summary",
            params=[*shared, ("time_zone", "America/Los_Angeles")],
            headers=headers,
        )
        kiritimati = await client.get(
            "/v1/catalog/events/summary",
            params=[*shared, ("time_zone", "Pacific/Kiritimati")],
            headers=headers,
        )
        invalid = await client.get(
            "/v1/catalog/events/summary",
            params=[*shared, ("time_zone", "Not/AZone")],
            headers=headers,
        )
        missing = await client.get(
            "/v1/catalog/events/summary",
            params=shared,
            headers=headers,
        )

    assert pacific.status_code == 200
    assert kiritimati.status_code == 200
    # Both zones see the same events; only the day each one lands on differs, so
    # the totals must agree while at least one boundary moves.
    assert pacific.json()["total_event_count"] == kiritimati.json()["total_event_count"]
    assert (
        min(day["start_day"] for day in kiritimati.json()["days"])
        >= min(day["start_day"] for day in pacific.json()["days"])
    )
    assert invalid.status_code == 422
    assert missing.status_code == 422


async def test_day_summary_applies_the_topic_selection(db: None) -> None:
    """Unlike the topic facet, the grid must narrow with the agenda it describes."""
    source_key, _, _, _ = await _source_with_current_events(event_count=4)
    window_start = datetime.now(UTC) - timedelta(days=1)
    date_range = f"{window_start.isoformat()}..{(window_start + timedelta(days=30)).isoformat()}"
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        onboard = await client.post(
            "/v1/onboard",
            json={"notify_email": f"topic-{uuid4().hex}@example.com"},
        )
        headers = {"X-EC-Tenant-ID": onboard.json()["tenant_id"]}
        shared = [
            ("source_key", source_key),
            ("date_range", date_range),
            ("time_zone", "America/Los_Angeles"),
        ]

        unfiltered = await client.get(
            "/v1/catalog/events/summary", params=shared, headers=headers
        )
        filtered = await client.get(
            "/v1/catalog/events/summary",
            params=[*shared, ("topic", "chess")],
            headers=headers,
        )
        rejected = await client.get(
            "/v1/catalog/events/summary",
            params=[*shared, ("topic", "not-a-topic")],
            headers=headers,
        )

    assert unfiltered.status_code == 200
    assert filtered.status_code == 200
    assert filtered.json()["total_event_count"] <= unfiltered.json()["total_event_count"]
    assert rejected.status_code == 422
