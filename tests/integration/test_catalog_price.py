"""Postgres catalog pricing integration coverage (FR-3.7/FR-4.6)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from events_concierge.composition import build_container
from events_concierge.config import get_settings
from events_concierge.domain import dedup
from events_concierge.domain.enums import PriceStatus, Source
from events_concierge.domain.events import CandidateEvent, GeoPoint
from events_concierge.domain.request import RequestConstraints, TimeWindow
from events_concierge.infra.db import system_session_scope

pytestmark = pytest.mark.integration


async def _catalog_identity_counts(*, candidate: CandidateEvent) -> tuple[int, int, int, UUID]:
    async with system_session_scope() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        (
                            SELECT count(*) FROM canonical_events
                            WHERE title = :title
                        ) AS canonical_count,
                        (
                            SELECT count(*) FROM event_source_links
                            WHERE source = :source AND source_event_id = :source_event_id
                        ) AS link_count,
                        (
                            SELECT count(*)
                            FROM canonical_events AS canonical
                            LEFT JOIN event_source_links AS link
                              ON link.canonical_event_id = canonical.canonical_event_id
                            WHERE canonical.title = :title
                              AND link.canonical_event_id IS NULL
                        ) AS orphan_count,
                        (
                            SELECT canonical_event_id FROM event_source_links
                            WHERE source = :source AND source_event_id = :source_event_id
                        ) AS linked_canonical_id
                    """
                ),
                {
                    "title": candidate.title,
                    "source": candidate.source.value,
                    "source_event_id": candidate.source_event_id,
                },
            )
        ).one()
    return (
        row.canonical_count,
        row.link_count,
        row.orphan_count,
        row.linked_canonical_id,
    )


async def _catalog_title_counts(title: str) -> tuple[int, int, int]:
    async with system_session_scope() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT
                        count(DISTINCT canonical.canonical_event_id) AS canonical_count,
                        count(link.source_event_id) AS link_count,
                        count(*) FILTER (
                            WHERE link.canonical_event_id IS NULL
                        ) AS orphan_count
                    FROM canonical_events AS canonical
                    LEFT JOIN event_source_links AS link
                      ON link.canonical_event_id = canonical.canonical_event_id
                    WHERE canonical.title = :title
                    """
                ),
                {"title": title},
            )
        ).one()
    return int(row.canonical_count), int(row.link_count), int(row.orphan_count)


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


async def test_catalog_retrieval_separates_live_discovery_from_explicit_history(db: None) -> None:
    """Default discovery is current; an explicit window can inspect retained history."""
    tag = uuid4().hex
    now = datetime.now(UTC).replace(microsecond=0)
    past = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"elapsed-{tag}",
        title=f"elapsed-catalog-row-{tag}",
        start_at=now - timedelta(days=1),
        registration_url=f"https://example.test/{tag}/elapsed",
        is_free=True,
    )
    future = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"future-{tag}",
        title=f"future-catalog-row-{tag}",
        start_at=now + timedelta(days=1),
        registration_url=f"https://example.test/{tag}/future",
        is_free=True,
    )
    container = build_container(get_settings())
    persisted = await container.catalog.upsert_candidates([past, future])
    past_id, future_id = (event.canonical_event_id for event in persisted)

    current = await container.catalog.retrieve(
        RequestConstraints(), intent_embedding=None, limit=10_000
    )
    current_ids = {event.canonical_event_id for event in current}
    assert past_id not in current_ids
    assert future_id in current_ids

    history = await container.catalog.retrieve(
        RequestConstraints(
            time_window=TimeWindow(start=now - timedelta(days=2), end=now + timedelta(days=2))
        ),
        intent_embedding=None,
        limit=10_000,
    )
    history_ids = {event.canonical_event_id for event in history}
    assert {past_id, future_id} <= history_ids


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


async def test_free_text_fallback_never_asserts_a_second_deduplicated_source_is_free(
    db: None,
) -> None:
    """Unambiguous text may fill one link, but shared provenance requires structured pricing."""
    tag = uuid4().hex
    common = {
        "source": Source.PUBLIC_JSONLD,
        "title": f"single-source-free-text-{tag}",
        "description": "COST: FREE! Public program.",
        "start_at": datetime.now(UTC).replace(microsecond=0) + timedelta(days=65),
        "city": f"single-source-price-city-{tag}",
    }
    first = CandidateEvent(
        source_event_id=f"first-{tag}",
        registration_url=f"https://example.test/{tag}/first",
        **common,
    )
    second = CandidateEvent(
        source_event_id=f"second-{tag}",
        registration_url=f"https://example.test/{tag}/second",
        **common,
    )
    catalog = build_container(get_settings()).catalog

    single_source = (await catalog.upsert_candidates([first]))[0]
    assert single_source.price_status is PriceStatus.FREE
    assert single_source.source_links[0].price_status is PriceStatus.FREE

    shared = (await catalog.upsert_candidates([second]))[0]
    assert shared.canonical_event_id == single_source.canonical_event_id
    assert shared.price_status is PriceStatus.UNKNOWN
    assert {link.source_event_id: link.price_status for link in shared.source_links} == {
        first.source_event_id: PriceStatus.FREE,
        second.source_event_id: PriceStatus.UNKNOWN,
    }


async def test_catalog_publishes_an_exact_range_only_while_all_paid_sources_agree(
    db: None,
) -> None:
    tag = uuid4().hex
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=75)
    common = {
        "source": Source.PUBLIC_JSONLD,
        "title": f"exact-price-range-{tag}",
        "start_at": start,
        "city": f"price-range-{tag}",
        "price_status": PriceStatus.PAID,
    }

    def candidate(source_event_id: str, minimum: int | None) -> CandidateEvent:
        return CandidateEvent(
            source_event_id=f"{source_event_id}-{tag}",
            registration_url=f"https://example.test/{tag}/{source_event_id}",
            price_min_cents=minimum,
            price_max_cents=5_000 if minimum is not None else None,
            price_currency="USD" if minimum is not None else None,
            **common,
        )

    container = build_container(get_settings())
    first = (await container.catalog.upsert_candidates([candidate("one", 2_500)]))[0]
    assert (
        first.price_status,
        first.price_min_cents,
        first.price_max_cents,
        first.price_currency,
    ) == (PriceStatus.PAID, 2_500, 5_000, "USD")

    agreed = (await container.catalog.upsert_candidates([candidate("two", 2_500)]))[0]
    assert agreed.canonical_event_id == first.canonical_event_id
    assert (
        agreed.price_min_cents,
        agreed.price_max_cents,
        agreed.price_currency,
    ) == (2_500, 5_000, "USD")

    conflicted = (await container.catalog.upsert_candidates([candidate("three", 3_000)]))[0]
    assert conflicted.canonical_event_id == first.canonical_event_id
    assert conflicted.price_status is PriceStatus.PAID
    assert (
        conflicted.price_min_cents,
        conflicted.price_max_cents,
        conflicted.price_currency,
    ) == (None, None, None)

    restored = (await container.catalog.upsert_candidates([candidate("three", 2_500)]))[0]
    assert (
        restored.price_min_cents,
        restored.price_max_cents,
        restored.price_currency,
    ) == (2_500, 5_000, "USD")


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


async def test_catalog_replay_outside_fuzzy_window_reuses_exact_source_identity(db: None) -> None:
    """One publisher's moved occurrence authoritatively refreshes its exclusive canonical."""
    tag = uuid4().hex
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=120)
    common = {
        "source": Source.PUBLIC_JSONLD,
        "source_event_id": f"rescheduled-{tag}",
    }
    initial = CandidateEvent(
        title=f"Original catalog source identity {tag}",
        start_at=start,
        end_at=start + timedelta(hours=1),
        registration_url=f"https://example.test/{tag}/initial",
        venue_name="Original venue",
        geo=GeoPoint(lat=37.7749, lon=-122.4194),
        city="San Francisco",
        description="Initial listing.",
        is_free=True,
        **common,
    )
    moved_start = start + timedelta(days=7)
    moved = CandidateEvent(
        title=f"Renamed and rescheduled source identity {tag}",
        start_at=moved_start,
        end_at=moved_start + timedelta(hours=3),
        registration_url=f"https://example.test/{tag}/moved",
        venue_name="Replacement venue",
        geo=GeoPoint(lat=37.3382, lon=-121.8863),
        city="San Jose",
        description="Updated.",
        is_free=False,
        **common,
    )
    container = build_container(get_settings())

    first = (await container.catalog.upsert_candidates([initial]))[0]
    replay = (await container.catalog.upsert_candidates([moved]))[0]
    canonical_count, link_count, orphan_count, linked_canonical_id = await _catalog_identity_counts(
        candidate=moved
    )

    assert replay.canonical_event_id == first.canonical_event_id == linked_canonical_id
    assert replay.title == moved.title
    assert replay.start_at == moved.start_at
    assert replay.end_at == moved.end_at
    assert replay.venue_name == moved.venue_name
    assert replay.geo == moved.geo
    assert replay.city_norm == "sanjose"
    assert replay.description == moved.description
    assert replay.price_status is PriceStatus.PAID
    assert len(replay.source_links) == 1
    assert replay.source_links[0].registration_url == moved.registration_url
    assert replay.source_links[0].price_status is PriceStatus.PAID
    assert (canonical_count, link_count, orphan_count) == (1, 1, 0)


async def test_catalog_moved_source_splits_from_shared_canonical_without_rewriting_other_link(
    db: None,
) -> None:
    """A moved provider link is re-deduped while another provider keeps the old occurrence."""
    tag = uuid4().hex
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=135)
    original_title = f"Shared occurrence {tag}"
    public = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"public-shared-{tag}",
        title=original_title,
        start_at=start,
        end_at=start + timedelta(hours=1),
        registration_url=f"https://example.test/{tag}/public",
        venue_name="Shared venue",
        geo=GeoPoint(lat=37.7749, lon=-122.4194),
        city="San Francisco",
        description="Original shared occurrence.",
        is_free=True,
    )
    meetup = CandidateEvent(
        source=Source.MEETUP,
        source_event_id=f"meetup-shared-{tag}",
        title=original_title,
        start_at=start,
        end_at=start + timedelta(hours=1),
        registration_url=f"https://meetup.test/{tag}",
        venue_name="Shared venue",
        geo=public.geo,
        city="San Francisco",
        description=public.description,
        is_free=True,
    )
    moved_start = start + timedelta(days=8)
    moved_public = CandidateEvent(
        source=public.source,
        source_event_id=public.source_event_id,
        title=f"Moved occurrence {tag}",
        start_at=moved_start,
        end_at=moved_start + timedelta(hours=2),
        registration_url=f"https://example.test/{tag}/public-moved",
        venue_name="Moved venue",
        geo=GeoPoint(lat=37.3382, lon=-121.8863),
        city="San Jose",
        description="The public listing moved; Meetup still asserts the original occurrence.",
        is_free=False,
    )
    container = build_container(get_settings())

    public_event, meetup_event = await container.catalog.upsert_candidates([public, meetup])
    moved_event = (await container.catalog.upsert_candidates([moved_public]))[0]
    original_event = await container.catalog.get(public_event.canonical_event_id)

    assert meetup_event.canonical_event_id == public_event.canonical_event_id
    assert moved_event.canonical_event_id != public_event.canonical_event_id
    assert original_event is not None
    assert original_event.title == original_title
    assert original_event.start_at == start
    assert original_event.venue_name == "Shared venue"
    assert original_event.geo == public.geo
    assert original_event.price_status is PriceStatus.FREE
    assert [(link.source, link.source_event_id) for link in original_event.source_links] == [
        (Source.MEETUP, meetup.source_event_id)
    ]
    assert moved_event.title == moved_public.title
    assert moved_event.start_at == moved_public.start_at
    assert moved_event.end_at == moved_public.end_at
    assert moved_event.venue_name == moved_public.venue_name
    assert moved_event.geo == moved_public.geo
    assert moved_event.city_norm == "sanjose"
    assert moved_event.price_status is PriceStatus.PAID
    assert [(link.source, link.source_event_id) for link in moved_event.source_links] == [
        (Source.PUBLIC_JSONLD, public.source_event_id)
    ]


async def test_catalog_exact_source_identity_wins_before_fuzzy_dedup(db: None) -> None:
    """A moved replay cannot be redirected to a different nearby fuzzy canonical."""
    tag = uuid4().hex
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=150)
    common = {
        "source": Source.PUBLIC_JSONLD,
        "title": f"catalog-identity-priority-{tag}",
        "city": f"priority-city-{tag}",
    }
    original = CandidateEvent(
        source_event_id=f"original-{tag}",
        start_at=start,
        registration_url=f"https://example.test/{tag}/original",
        **common,
    )
    nearby_other = CandidateEvent(
        source_event_id=f"other-{tag}",
        start_at=start + timedelta(days=1),
        registration_url=f"https://example.test/{tag}/other",
        **common,
    )
    moved_original = CandidateEvent(
        source_event_id=original.source_event_id,
        start_at=nearby_other.start_at,
        registration_url=f"https://example.test/{tag}/original-moved",
        **common,
    )
    container = build_container(get_settings())

    original_event, other_event = await container.catalog.upsert_candidates(
        [original, nearby_other]
    )
    replayed_original, replayed_other = await container.catalog.upsert_candidates(
        [moved_original, nearby_other]
    )
    canonical_count, link_count, orphan_count, linked_canonical_id = await _catalog_identity_counts(
        candidate=original
    )

    assert original_event.canonical_event_id != other_event.canonical_event_id
    assert replayed_original.canonical_event_id == original_event.canonical_event_id
    assert replayed_other.canonical_event_id == other_event.canonical_event_id
    assert linked_canonical_id == original_event.canonical_event_id
    assert (canonical_count, link_count, orphan_count) == (2, 1, 0)


async def test_catalog_concurrent_first_ingest_replay_has_one_canonical_and_link(
    db: None,
) -> None:
    """Opposite-order concurrent retries serialize without orphans, deadlocks, or reordering."""
    tag = uuid4().hex
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=180)
    first_candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"concurrent-first-{tag}",
        title=f"catalog-concurrent-first-{tag}",
        start_at=start,
        registration_url=f"https://example.test/{tag}/first",
        city=f"concurrent-first-city-{tag}",
    )
    second_candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"concurrent-second-{tag}",
        title=f"catalog-concurrent-second-{tag}",
        start_at=start + timedelta(hours=12),
        registration_url=f"https://example.test/{tag}/second",
        city=f"concurrent-second-city-{tag}",
    )
    # All eight contenders must reach PostgreSQL; a five-slot application pool
    # tests queue deadlines instead of the database identity/locking contract.
    container = build_container(get_settings().model_copy(update={"database_pool_size": 8}))
    input_batches = [
        [first_candidate, second_candidate]
        if index % 2 == 0
        else [second_candidate, first_candidate]
        for index in range(8)
    ]

    batches = await asyncio.gather(
        *(container.catalog.upsert_candidates(candidates) for candidates in input_batches)
    )
    first_counts = await _catalog_identity_counts(candidate=first_candidate)
    second_counts = await _catalog_identity_counts(candidate=second_candidate)

    for candidates, events in zip(input_batches, batches, strict=True):
        assert [event.source_links[0].source_event_id for event in events] == [
            candidate.source_event_id for candidate in candidates
        ]
    assert first_counts[:3] == (1, 1, 0)
    assert second_counts[:3] == (1, 1, 0)
    assert {event.canonical_event_id for batch in batches for event in batch} == {
        first_counts[3],
        second_counts[3],
    }


async def test_catalog_concurrent_distinct_sources_fuzzy_merge_first_ingest(
    db: None,
) -> None:
    """Two source identities for one new event cannot concurrently mint split canonicals."""
    tag = uuid4().hex
    title = f"catalog-concurrent-fuzzy-first-{tag}"
    common = {
        "title": title,
        "start_at": datetime.now(UTC).replace(microsecond=0) + timedelta(days=210),
        "city": f"concurrent-fuzzy-city-{tag}",
        "is_free": True,
    }
    public_candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"public-{tag}",
        registration_url=f"https://example.test/{tag}/public",
        **common,
    )
    meetup_candidate = CandidateEvent(
        source=Source.MEETUP,
        source_event_id=f"meetup-{tag}",
        registration_url=f"https://meetup.test/{tag}",
        **common,
    )
    container = build_container(get_settings())

    public_result, meetup_result = await asyncio.wait_for(
        asyncio.gather(
            container.catalog.upsert_candidates([public_candidate]),
            container.catalog.upsert_candidates([meetup_candidate]),
        ),
        timeout=10,
    )

    assert public_result[0].canonical_event_id == meetup_result[0].canonical_event_id
    assert await _catalog_title_counts(title) == (1, 2, 0)


async def test_catalog_crossed_fuzzy_batches_lock_canonicals_in_one_order(
    db: None,
) -> None:
    """Disjoint source IDs in opposite input order converge without a canonical-row deadlock."""
    tag = uuid4().hex
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=240)
    city = f"crossed-fuzzy-city-{tag}"
    first_title = f"crossed-fuzzy-first-{tag}"
    second_title = f"crossed-fuzzy-second-{tag}"
    # All eight contenders must reach PostgreSQL; a five-slot application pool
    # tests queue deadlines instead of the database identity/locking contract.
    container = build_container(get_settings().model_copy(update={"database_pool_size": 8}))
    seeds = [
        CandidateEvent(
            source=Source.PUBLIC_JSONLD,
            source_event_id=f"seed-first-{tag}",
            title=first_title,
            start_at=start,
            registration_url=f"https://example.test/{tag}/seed-first",
            city=city,
        ),
        CandidateEvent(
            source=Source.PUBLIC_JSONLD,
            source_event_id=f"seed-second-{tag}",
            title=second_title,
            start_at=start + timedelta(hours=12),
            registration_url=f"https://example.test/{tag}/seed-second",
            city=city,
        ),
    ]
    seeded = await container.catalog.upsert_candidates(seeds)
    batches: list[list[CandidateEvent]] = []
    for index in range(8):
        first_refresh = CandidateEvent(
            source=Source.MEETUP,
            source_event_id=f"refresh-first-{index}-{tag}",
            title=first_title,
            start_at=start,
            registration_url=f"https://meetup.test/{tag}/first/{index}",
            city=city,
        )
        second_refresh = CandidateEvent(
            source=Source.MEETUP,
            source_event_id=f"refresh-second-{index}-{tag}",
            title=second_title,
            start_at=start + timedelta(hours=12),
            registration_url=f"https://meetup.test/{tag}/second/{index}",
            city=city,
        )
        batches.append(
            [first_refresh, second_refresh] if index % 2 == 0 else [second_refresh, first_refresh]
        )

    refreshed = await asyncio.wait_for(
        asyncio.gather(*(container.catalog.upsert_candidates(batch) for batch in batches)),
        timeout=30,
    )

    expected_by_title = {
        first_title: seeded[0].canonical_event_id,
        second_title: seeded[1].canonical_event_id,
    }
    for candidates, events in zip(batches, refreshed, strict=True):
        assert [event.title for event in events] == [candidate.title for candidate in candidates]
        assert [event.canonical_event_id for event in events] == [
            expected_by_title[candidate.title] for candidate in candidates
        ]
    assert await _catalog_title_counts(first_title) == (1, 9, 0)
    assert await _catalog_title_counts(second_title) == (1, 9, 0)


async def test_catalog_crossed_exact_refreshes_lock_drifted_canonicals_in_one_order(
    db: None,
) -> None:
    """Crossed provider splits outside old fuzzy domains cannot invert canonical row locks."""
    tag = uuid4().hex
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=270)
    first_title = f"crossed-drift-first-{tag}"
    second_title = f"crossed-drift-second-{tag}"
    first_public = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"drift-first-public-{tag}",
        title=first_title,
        start_at=start,
        registration_url=f"https://example.test/{tag}/first/public",
        city=f"drift-seed-first-{tag}",
    )
    first_meetup = CandidateEvent(
        source=Source.MEETUP,
        source_event_id=f"drift-first-meetup-{tag}",
        title=first_title,
        start_at=start,
        registration_url=f"https://meetup.test/{tag}/first",
        city=f"drift-seed-first-{tag}",
    )
    second_public = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"drift-second-public-{tag}",
        title=second_title,
        start_at=start + timedelta(days=1),
        registration_url=f"https://example.test/{tag}/second/public",
        city=f"drift-seed-second-{tag}",
    )
    second_meetup = CandidateEvent(
        source=Source.MEETUP,
        source_event_id=f"drift-second-meetup-{tag}",
        title=second_title,
        start_at=start + timedelta(days=1),
        registration_url=f"https://meetup.test/{tag}/second",
        city=f"drift-seed-second-{tag}",
    )
    container = build_container(get_settings())
    seeded = await container.catalog.upsert_candidates(
        [first_public, first_meetup, second_public, second_meetup]
    )
    first_id = seeded[0].canonical_event_id
    second_id = seeded[2].canonical_event_id
    assert seeded[1].canonical_event_id == first_id
    assert seeded[3].canonical_event_id == second_id

    def drift(candidate: CandidateEvent, *, city: str, days: int) -> CandidateEvent:
        return CandidateEvent(
            source=candidate.source,
            source_event_id=candidate.source_event_id,
            title=candidate.title,
            start_at=start + timedelta(days=days),
            registration_url=f"{candidate.registration_url}?drift={days}",
            city=city,
        )

    first_then_second = [
        drift(first_public, city=f"drift-a-{tag}", days=30),
        drift(second_public, city=f"drift-b-{tag}", days=40),
    ]
    second_then_first = [
        drift(second_meetup, city=f"drift-c-{tag}", days=50),
        drift(first_meetup, city=f"drift-d-{tag}", days=60),
    ]

    crossed = await asyncio.wait_for(
        asyncio.gather(
            container.catalog.upsert_candidates(first_then_second),
            container.catalog.upsert_candidates(second_then_first),
        ),
        timeout=10,
    )

    input_batches = [first_then_second, second_then_first]
    for candidates, events in zip(input_batches, crossed, strict=True):
        for candidate, event in zip(candidates, events, strict=True):
            assert event.title == candidate.title
            assert event.start_at == candidate.start_at
            assert event.city_norm == dedup.normalize_city(candidate.city)
            assert [(link.source, link.source_event_id) for link in event.source_links] == [
                (candidate.source, candidate.source_event_id)
            ]
    result_ids = {event.canonical_event_id for batch in crossed for event in batch}
    assert len(result_ids) == 4
    assert {first_id, second_id} <= result_ids
    assert await _catalog_title_counts(first_title) == (2, 2, 0)
    assert await _catalog_title_counts(second_title) == (2, 2, 0)
