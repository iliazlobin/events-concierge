"""Postgres catalog pricing integration coverage (FR-3.7/FR-4.6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from events_concierge.composition import build_container
from events_concierge.config import get_settings
from events_concierge.domain.enums import PriceStatus, Source
from events_concierge.domain.events import CandidateEvent, GeoPoint
from events_concierge.domain.request import RequestConstraints, TimeWindow

pytestmark = pytest.mark.integration


async def test_catalog_persists_all_price_states_and_retrieves_verified_free_only(db: None) -> None:
    """Catalog storage never turns an unknown Luma/public price into free (FR-3.7/FR-4.6)."""
    tag = uuid4().hex
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=30)
    candidates = [
        CandidateEvent(
            source=Source.PUBLIC_JSONLD,
            source_event_id=f"price-free-{tag}",
            title=f"price-test-free-{tag}",
            start_at=start,
            registration_url="https://example.test/price/free",
            is_free=True,
        ),
        CandidateEvent(
            source=Source.PUBLIC_JSONLD,
            source_event_id=f"price-paid-{tag}",
            title=f"price-test-paid-{tag}",
            start_at=start + timedelta(hours=4),
            registration_url="https://example.test/price/paid",
            is_free=False,
        ),
        CandidateEvent(
            source=Source.PUBLIC_JSONLD,
            source_event_id=f"price-unknown-{tag}",
            title=f"price-test-unknown-{tag}",
            start_at=start + timedelta(hours=8),
            registration_url="https://example.test/price/unknown",
        ),
    ]
    container = build_container(get_settings())

    persisted = await container.catalog.upsert_candidates(candidates)
    price_by_id = {event.canonical_event_id: event.price_status for event in persisted}
    window = TimeWindow(start=start - timedelta(minutes=1), end=start + timedelta(hours=9))
    all_prices = await container.catalog.retrieve(
        RequestConstraints(time_window=window), intent_embedding=None, limit=1_000
    )
    free_only = await container.catalog.retrieve(
        RequestConstraints(time_window=window, budget_free=True), intent_embedding=None, limit=10
    )

    assert set(price_by_id.values()) == {PriceStatus.FREE, PriceStatus.PAID, PriceStatus.UNKNOWN}
    assert {
        event.canonical_event_id: event.price_status
        for event in all_prices
        if event.canonical_event_id in price_by_id
    } == price_by_id
    assert {
        event.canonical_event_id for event in free_only if event.canonical_event_id in price_by_id
    } == {
        canonical_event_id
        for canonical_event_id, price_status in price_by_id.items()
        if price_status is PriceStatus.FREE
    }


async def test_catalog_keeps_conflicting_source_prices_unknown_until_all_links_are_free(
    db: None,
) -> None:
    """A free refresh cannot erase a retained paid source observation (FR-3.8/FR-5.10)."""
    tag = uuid4().hex
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=60)
    common = {
        "source": Source.PUBLIC_JSONLD,
        "title": f"price-conflict-{tag}",
        "start_at": start,
        "city": f"price-test-{tag}",
    }
    free = CandidateEvent(
        source_event_id=f"free-{tag}",
        registration_url=f"https://example.test/{tag}/free",
        is_free=True,
        **common,
    )
    paid = CandidateEvent(
        source_event_id=f"paid-{tag}",
        registration_url=f"https://example.test/{tag}/paid",
        is_free=False,
        **common,
    )
    container = build_container(get_settings())

    canonical = (await container.catalog.upsert_candidates([free]))[0]
    assert canonical.price_status is PriceStatus.FREE

    conflicting = (await container.catalog.upsert_candidates([paid]))[0]
    assert conflicting.canonical_event_id == canonical.canonical_event_id
    assert conflicting.price_status is PriceStatus.UNKNOWN
    assert {link.source_event_id: link.price_status for link in conflicting.source_links} == {
        free.source_event_id: PriceStatus.FREE,
        paid.source_event_id: PriceStatus.PAID,
    }

    still_conflicted = (await container.catalog.upsert_candidates([free]))[0]
    assert still_conflicted.price_status is PriceStatus.UNKNOWN

    corrected_paid = CandidateEvent(
        source_event_id=paid.source_event_id,
        registration_url=paid.registration_url,
        is_free=True,
        **common,
    )
    all_free = (await container.catalog.upsert_candidates([corrected_paid]))[0]
    assert all_free.price_status is PriceStatus.FREE
    assert {link.price_status for link in all_free.source_links} == {PriceStatus.FREE}


async def test_catalog_enriches_absent_metadata_on_a_repeat_source_observation(db: None) -> None:
    """A richer retry fills blanks without reminting or rewriting the canonical identity (FR-3.8)."""
    tag = uuid4().hex
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=90)
    common = {
        "source": Source.PUBLIC_JSONLD,
        "source_event_id": f"enrich-{tag}",
        "title": f"metadata-enrichment-{tag}",
        "start_at": start,
        "registration_url": f"https://example.test/{tag}",
        "city": f"enrich-city-{tag}",
    }
    sparse = CandidateEvent(**common)
    richer = CandidateEvent(
        end_at=start + timedelta(hours=2),
        venue_name="Enriched venue",
        geo=GeoPoint(lat=37.8715, lon=-122.2730),
        description="A substantially richer publisher description for catalog retrieval.",
        **common,
    )
    container = build_container(get_settings())

    initial = (await container.catalog.upsert_candidates([sparse]))[0]
    enriched = (await container.catalog.upsert_candidates([richer]))[0]

    assert enriched.canonical_event_id == initial.canonical_event_id
    assert enriched.end_at == richer.end_at
    assert enriched.venue_name == "Enriched venue"
    assert enriched.geo == richer.geo
    assert enriched.description == richer.description
