"""Autocomplete searches all admitted names, not the first chronological event page."""

from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from tests.integration.test_catalog_browse import _source_with_current_events

from events_concierge.adapters.postgres.catalog_observations import (
    PostgresCatalogObservationRepository,
)
from events_concierge.adapters.postgres.catalog_refresh_commit import (
    PostgresCatalogRefreshCommitter,
)
from events_concierge.adapters.postgres.catalog_sources import PostgresCatalogSourceRepository
from events_concierge.domain.enums import RegistrationStatus
from events_concierge.domain.events import EventEntityProfile
from events_concierge.infra.db import system_session_scope

pytestmark = pytest.mark.integration


async def test_names_beyond_first_page_with_role_counts_and_literal_punctuation(db: None) -> None:
    source, current, _, catalog = await _source_with_current_events(event_count=76)
    # Earlier events mention Nebius only in prose. They must neither use up the name limit
    # nor crowd out a structured company name on the last chronological event.
    candidates = [replace(event, description="Nebius appears only in the description.")
                  for event in current]
    candidates[-1] = replace(
        candidates[-1], title="Nebius Builders Night", venue_name="Nebius Studio",
        organizer_name="A%_B Collective", host_names=("Nebius",),
        speaker_names=("Mira Developer",), partner_names=("Cloud Partner",),
        registration_status=RegistrationStatus.OPEN,
        entity_profiles=(EventEntityProfile(
            name="Nebius", role="host", kind="organization",
            profile_url="https://www.linkedin.com/company/nebius",
        ),),
    )
    candidates[-2] = replace(candidates[-2], host_names=("NEBIUS",), entity_profiles=())
    runs = PostgresCatalogSourceRepository()
    committer = PostgresCatalogRefreshCommitter(catalog, PostgresCatalogObservationRepository())
    run_key = f"manual:autocomplete-{uuid4().hex}"
    claim = await runs.claim_refresh(source, run_key, lease_seconds=300)
    assert await committer.commit_refresh(source, run_key, lease_token=claim.lease_token,
                                          candidates=candidates) is not None
    first, _ = await catalog.browse_current(source_keys=(source,), after=None, limit=72)
    assert not any(name.lower() == "nebius"
                   for item in first for name in item.canonical_event.host_names)
    matches = await catalog.suggest_names(query="nebius", source_keys=(source,), limit=1)
    assert len(matches) == 1
    assert matches[0].name.lower() == "nebius"
    assert matches[0].kinds == ("host", "organization")
    assert matches[0].event_count == 2
    all_matches = await catalog.suggest_names(query="nebius", source_keys=(source,))
    assert {item.name.lower() for item in all_matches} == {
        "nebius", "nebius builders night", "nebius studio",
    }
    open_matches = await catalog.suggest_names(
        query="nebius", source_keys=(source,), availability="available",
    )
    assert next(item for item in open_matches if item.name.lower() == "nebius").event_count == 1
    for query, name, kind in [
        ("%_", "A%_B Collective", "organizer"),
        ("mira", "Mira Developer", "speaker"),
        ("cloud", "Cloud Partner", "partner"),
    ]:
        result = await catalog.suggest_names(query=query, source_keys=(source,))
        assert [(item.name, item.kinds) for item in result] == [(name, (kind,))]
        selected, _ = await catalog.browse_current(
            source_keys=(source,), after=None, limit=5, query=name,
        )
        assert [item.canonical_event.title for item in selected] == [candidates[-1].title]

    # An event crossing two disjoint windows and multiple mentions still counts only once.
    start = candidates[-1].start_at
    windows = ((start, start + timedelta(minutes=20)),
               (start + timedelta(minutes=40), start + timedelta(hours=1)))
    ranged = await catalog.suggest_names(query="nebius", source_keys=(source,), date_ranges=windows)
    assert all(item.event_count == 1 for item in ranged)
    assert await catalog.suggest_names(query="nebius", source_keys=(source,), cities=("Oakland",)) == []


async def test_fixture_names_and_noncurrent_observations_are_not_public(db: None) -> None:
    fixture_source, _, _, catalog = await _source_with_current_events(fixture=True)
    assert await catalog.suggest_names(query="Luma", source_keys=(fixture_source,)) == []
    source, _, _, catalog = await _source_with_current_events()
    assert await catalog.suggest_names(query="Stale", source_keys=(source,)) == []
    # The app role can execute only the bounded public function, never its unbounded primitive.
    async with system_session_scope() as session:
        grants = (await session.execute(text("""
            SELECT has_function_privilege(current_user,
                'public.fn_browse_filtered_current_catalog_events_unbounded_v1(text[],timestamptz,timestamptz,text,text[],text[],text,integer,integer,text[],text,timestamptz,uuid,integer)',
                'EXECUTE')
        """))).scalar_one()
        assert grants is False
