"""Provider visibility survives source publication and uses the same public admission gate."""

from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import text
from tests.integration.test_catalog_browse import _source_with_current_events
from tests.integration.test_catalog_name_suggestions import _publish_names

from events_concierge.api.app import _catalog_browse_item_out
from events_concierge.infra.db import system_session_scope

pytestmark = pytest.mark.integration


async def test_vendor_source_counts_respect_place_and_registration_filters(db) -> None:
    source, _, _, catalog = await _source_with_current_events(event_count=2)
    _, providers = await catalog.browse_current(
        source_keys=(source,), cities=("Oakland",), after=None, limit=10
    )
    assert (
        next(provider for provider in providers if provider.source_key == source).event_count == 0
    )
    _, providers = await catalog.browse_current(
        source_keys=(source,), cities=("San Francisco",), after=None, limit=10
    )
    assert (
        next(provider for provider in providers if provider.source_key == source).event_count == 2
    )
    _, providers = await catalog.browse_current(
        source_keys=(source,), availability="sold_out", after=None, limit=10
    )
    assert (
        next(provider for provider in providers if provider.source_key == source).event_count == 0
    )


async def test_private_card_visibility_is_admitted_source_metadata_not_a_global_guess(db) -> None:
    source, events, _, catalog = await _source_with_current_events(event_count=1)
    [candidate] = events
    candidate = replace(
        candidate,
        visibility="private",
        location_withheld=True,
        geo=None,
        venue_name=None,
        organizer_name="SemiAnalysis",
        entity_profiles=(),
        title="Silicon After Dark " + uuid4().hex,
    )
    await _publish_names(source, [candidate], catalog)
    [item], _ = await catalog.browse_current(source_keys=(source,), after=None, limit=10)
    assert item.sources[0].visibility == "private"
    output = _catalog_browse_item_out(item)
    assert output.visibility == "private" and output.sources[0].visibility == "private"
    assert output.latitude is None and output.longitude is None and output.venue_name is None
    detail = await catalog.get_browse_event(item.canonical_event.canonical_event_id)
    assert detail is not None and detail.sources[0].visibility == "private"
    async with system_session_scope() as session:
        denied = await session.execute(
            text("""
            SELECT public.fn_catalog_observation_card_v1(
                :source, 'public_jsonld', 'undisclosed-id', :event_id)
        """),
            {"source": source, "event_id": item.canonical_event.canonical_event_id},
        )
        assert denied.scalar_one() is None


async def test_unadmitted_fixture_visibility_cannot_be_queried_by_id(db) -> None:
    source, events, _, catalog = await _source_with_current_events(fixture=True, event_count=1)
    await _publish_names(source, [replace(events[0], visibility="private")], catalog)
    async with system_session_scope() as session:
        row = (
            await session.execute(
                text("""
            SELECT canonical_event_id, source, source_event_id
            FROM catalog_event_observations WHERE source_key=:source AND source_event_id=:identity
        """),
                {"source": source, "identity": events[0].source_event_id},
            )
        ).one()
        label = (
            await session.execute(
                text("""
            SELECT public.fn_catalog_observation_card_v1(:source,:kind,:identity,:cid)
        """),
                {
                    "source": source,
                    "kind": row.source,
                    "identity": row.source_event_id,
                    "cid": row.canonical_event_id,
                },
            )
        ).scalar_one()
        assert label is None
