"""Verified direct public entity-profile domain boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest

from events_concierge.adapters.postgres.catalog import _conservative_shared_entities
from events_concierge.api.app import _catalog_browse_item_out
from events_concierge.domain.catalog_browse import CatalogBrowseEvent, CatalogBrowseSource
from events_concierge.domain.enums import Source
from events_concierge.domain.events import (
    CandidateEvent,
    CanonicalEvent,
    EventEntityKind,
    EventEntityProfile,
)


def _candidate(
    *profiles: EventEntityProfile,
    host_names: tuple[str, ...] = ("Ada Lovelace",),
) -> CandidateEvent:
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id="profile-event",
        title="Verified profiles",
        start_at=datetime(2026, 8, 1, tzinfo=UTC),
        registration_url="https://example.test/events/profile",
        organizer_name="Deep Tech Connections",
        host_names=host_names,
        speaker_names=("Grace Hopper",),
        partner_names=("Corgi",),
        entity_profiles=profiles,
    )


def test_direct_linkedin_and_organization_website_profiles_are_canonicalized() -> None:
    event = _candidate(
        EventEntityProfile(
            name="Ada Lovelace",
            role="host",
            kind="person",
            profile_url="https://www.linkedin.com/in/ada-lovelace?trk=public#about",
        ),
        EventEntityProfile(
            name="Deep Tech Connections",
            role="organizer",
            kind="organization",
            profile_url="https://linkedin.com/company/deep-tech-connections/?trk=public",
        ),
        EventEntityProfile(
            name="Corgi",
            role="partner",
            kind="organization",
            profile_url="https://corgi.example.test/about#team",
        ),
    )

    assert [profile.profile_url for profile in event.entity_profiles] == [
        "https://www.linkedin.com/in/ada-lovelace",
        "https://linkedin.com/company/deep-tech-connections/",
        "https://corgi.example.test/about",
    ]


@pytest.mark.parametrize(
    ("kind", "url"),
    [
        (
            "person",
            "https://www.linkedin.com/search/results/people/?keywords=Ada%20Lovelace",
        ),
        (
            "organization",
            "https://www.linkedin.com/search/results/companies/?keywords=Corgi",
        ),
        ("person", "https://ada.example.test"),
        ("organization", "javascript:alert(1)"),
        ("organization", "https://user:password@example.test/profile"),
    ],
)
def test_search_guesses_and_malformed_or_wrong_kind_urls_are_rejected(
    kind: str,
    url: str,
) -> None:
    with pytest.raises(ValueError):
        EventEntityProfile(
            name="Ada Lovelace",
            role="host",
            kind=cast(EventEntityKind, kind),
            profile_url=url,
        )


def test_profiles_must_match_a_displayed_role_and_role_identity_is_unique() -> None:
    detached = EventEntityProfile(
        name="Private Attendee",
        role="host",
        kind="person",
        profile_url="https://www.linkedin.com/in/private-attendee",
    )
    with pytest.raises(ValueError, match="displayed role"):
        _candidate(detached)

    direct = EventEntityProfile(
        name="Ada Lovelace",
        role="host",
        kind="person",
        profile_url="https://www.linkedin.com/in/ada-lovelace",
    )
    duplicate = EventEntityProfile(
        name="ada lovelace",
        role="host",
        kind="person",
        profile_url="https://www.linkedin.com/in/ada-lovelace-2",
    )
    with pytest.raises(ValueError, match="duplicate role"):
        _candidate(direct, duplicate)


def test_profile_count_is_bounded() -> None:
    host_names = tuple(f"Host {index}" for index in range(22))
    speaker_names = tuple(f"Speaker {index}" for index in range(21))
    partner_names = tuple(f"Partner {index}" for index in range(21))
    profiles = [
        EventEntityProfile(
            name="Organizer",
            role="organizer",
            kind="organization",
            profile_url="https://organizer.example.test",
        )
    ]
    profiles.extend(
        EventEntityProfile(
            name=name,
            role="host",
            kind="person",
            profile_url=f"https://www.linkedin.com/in/host-{index}",
        )
        for index, name in enumerate(host_names)
    )
    profiles.extend(
        EventEntityProfile(
            name=name,
            role="speaker",
            kind="person",
            profile_url=f"https://www.linkedin.com/in/speaker-{index}",
        )
        for index, name in enumerate(speaker_names)
    )
    profiles.extend(
        EventEntityProfile(
            name=name,
            role="partner",
            kind="organization",
            profile_url=f"https://partner-{index}.example.test",
        )
        for index, name in enumerate(partner_names)
    )

    with pytest.raises(ValueError, match="profile limit"):
        CandidateEvent(
            source=Source.PUBLIC_JSONLD,
            source_event_id="too-many-profiles",
            title="Too many profiles",
            start_at=datetime(2026, 8, 1, tzinfo=UTC),
            registration_url="https://example.test/events/profile-limit",
            organizer_name="Organizer",
            host_names=host_names,
            speaker_names=speaker_names,
            partner_names=partner_names,
            entity_profiles=tuple(profiles),
        )


def test_catalog_api_output_emits_the_typed_profile_shape() -> None:
    profile = EventEntityProfile(
        name="Ada Lovelace",
        role="speaker",
        kind="person",
        profile_url="https://www.linkedin.com/in/ada-lovelace",
    )
    now = datetime(2026, 8, 1, tzinfo=UTC)
    canonical = CanonicalEvent(
        canonical_event_id=uuid4(),
        title="API profile",
        start_at=now,
        speaker_names=("Ada Lovelace",),
        entity_profiles=(profile,),
    )
    source = CatalogBrowseSource(
        source_key="luma-sf",
        label="Luma Bay Area",
        publisher="Luma Discover",
        provider="luma",
        seed_url="https://api.luma.com/discover/get-paginated-events",
        source=Source.PUBLIC_JSONLD,
        source_event_id="api-profile",
        registration_url="https://luma.com/api-profile",
        last_seen_at=now,
        refresh_run_key="profile-run",
    )

    output = _catalog_browse_item_out(
        CatalogBrowseEvent(canonical_event=canonical, sources=(source,))
    )

    assert [item.model_dump() for item in output.entity_profiles] == [
        {
            "name": "Ada Lovelace",
            "role": "speaker",
            "kind": "person",
            "profile_url": "https://www.linkedin.com/in/ada-lovelace",
        }
    ]


def test_shared_canonical_merge_fills_empty_profiles_without_overwriting_conflicts() -> None:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    direct_ada = EventEntityProfile(
        name="Ada Lovelace",
        role="host",
        kind="person",
        profile_url="https://www.linkedin.com/in/ada-lovelace",
    )
    empty = CanonicalEvent(
        canonical_event_id=uuid4(),
        title="Shared event",
        start_at=now,
        host_names=("Ada Lovelace",),
    )
    candidate = _candidate(direct_ada)

    *_, filled = _conservative_shared_entities(empty, candidate)

    assert filled == (direct_ada,)

    existing = CanonicalEvent(
        canonical_event_id=uuid4(),
        title="Shared event",
        start_at=now,
        host_names=("Ada Lovelace",),
        entity_profiles=(direct_ada,),
    )
    grace = EventEntityProfile(
        name="Grace Hopper",
        role="host",
        kind="person",
        profile_url="https://www.linkedin.com/in/grace-hopper",
    )
    conflicting = _candidate(
        grace,
        host_names=("Grace Hopper", "Community Host"),
    )

    _, merged_hosts, _, _, retained = _conservative_shared_entities(
        existing,
        conflicting,
    )

    assert merged_hosts == ("Ada Lovelace",)
    assert retained == (direct_ada,)
