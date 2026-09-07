"""Composition coverage for the reviewed Luma host-calendar catalog mode."""

from __future__ import annotations

import pytest

from events_concierge.adapters.luma_calendar import LumaCalendarCatalogFetcher
from events_concierge.composition import build_container
from events_concierge.config import Settings
from events_concierge.domain.enums import CatalogSourceMode


def test_public_catalog_composition_wires_luma_calendar_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole host-calendar fleet runs through this one lazily imported binding.

    ``luma_calendar_json`` backs every reviewed Luma host calendar, so an unwired mode would take
    the fleet's entire depth-first coverage down at once while every registry assertion still
    passed.
    """
    monkeypatch.setattr("events_concierge.composition.init_engine", lambda _, **kwargs: None)

    container = build_container(
        Settings(
            discovery_sources="public_jsonld",
            cohere_api_key=None,
            google_calendar_enabled=False,
        )
    )

    assert isinstance(
        container.catalog_refresh._fetchers[CatalogSourceMode.LUMA_CALENDAR_JSON],
        LumaCalendarCatalogFetcher,
    )
