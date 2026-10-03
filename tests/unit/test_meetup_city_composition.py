"""Composition coverage for the reviewed Meetup city catalog mode."""

from __future__ import annotations

import pytest

from events_concierge.adapters.meetup_city.source import MeetupCityCatalogFetcher
from events_concierge.adapters.meetup_group.source import MeetupGroupCalendarCatalogFetcher
from events_concierge.composition import build_container
from events_concierge.config import Settings
from events_concierge.domain.enums import CatalogSourceMode


def test_public_catalog_composition_wires_meetup_city_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("events_concierge.composition.init_engine", lambda _, **kwargs: None)

    container = build_container(
        Settings(
            discovery_sources="public_jsonld",
            cohere_api_key=None,
            google_calendar_enabled=False,
        )
    )

    assert isinstance(
        container.catalog_refresh._fetchers[CatalogSourceMode.MEETUP_CITY_JSONLD],
        MeetupCityCatalogFetcher,
    )
    assert isinstance(
        container.catalog_refresh._fetchers[CatalogSourceMode.MEETUP_GROUP_ICS],
        MeetupGroupCalendarCatalogFetcher,
    )
