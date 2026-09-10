"""Price-state catalog and API projection tests (FR-3.7/FR-4.6)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from events_concierge.adapters.postgres.catalog import PostgresCatalogRepository
from events_concierge.api.app import _feed_out
from events_concierge.domain.enums import ConflictVerdict, PriceStatus
from events_concierge.domain.events import CanonicalEvent
from events_concierge.domain.request import EventRequest, Feed, RankedCandidate, RequestConstraints


def test_catalog_free_only_predicate_requires_explicit_free_status() -> None:
    """Unknown catalog rows cannot pass a free-only retrieval constraint (FR-4.6)."""
    where, params = PostgresCatalogRepository._constraint_sql(RequestConstraints(budget_free=True))

    assert "coalesce(end_at, start_at) > CURRENT_TIMESTAMP" in where
    assert "price_status = :price_status" in where
    assert params == {"price_status": "free"}


def test_catalog_retrieval_always_excludes_elapsed_events() -> None:
    """A missing request window cannot allow durable stale rows back into the feed."""
    where, params = PostgresCatalogRepository._constraint_sql(RequestConstraints())

    assert "coalesce(end_at, start_at) > CURRENT_TIMESTAMP" in where
    assert "event_status <> 'cancelled'" in where
    assert params == {}


def test_feed_api_projects_the_explicit_price_status() -> None:
    """Consumers can distinguish a paid recommendation from an unknown price (FR-3.7)."""
    event = CanonicalEvent(
        canonical_event_id=uuid4(),
        title="Paid Bay Area conference",
        start_at=datetime(2026, 8, 1, 18, 0, tzinfo=UTC),
        price_status=PriceStatus.PAID,
        price_min_cents=2_500,
        price_max_cents=5_000,
        price_currency="USD",
    )
    request = EventRequest(
        request_id=uuid4(),
        tenant_id=uuid4(),
        raw_text="all events",
        constraints=RequestConstraints(),
    )
    feed = Feed(
        request_id=request.request_id,
        items=(
            RankedCandidate(
                canonical_event=event,
                score=0.9,
                rationale="match score 0.90",
                conflict_verdict=ConflictVerdict.OK,
                lane_plan=(),
            ),
        ),
    )

    output = _feed_out(feed)

    assert output.items[0].price_status == "paid"
    assert output.items[0].price_min_cents == 2_500
    assert output.items[0].price_max_cents == 5_000
    assert output.items[0].price_currency == "USD"
