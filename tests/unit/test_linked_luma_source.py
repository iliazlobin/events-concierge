"""Only anonymous, exact supplied/publicly linked event pages admit private card metadata."""

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest
from selectolax.parser import HTMLParser

from events_concierge.adapters.luma_event.source import (
    LumaEventCatalogFetcher,
    LumaEventFetchError,
    linked_luma_event,
)
from events_concierge.domain.catalog_sources import CatalogSource, catalog_candidate_content_hash
from events_concierge.domain.enums import CatalogSourceMode

_URL = "https://luma.com/siliconafterdark"


def _data(visibility: str = "private") -> dict:
    return {
        "api_id": "evt-reviewed",
        "event": {
            "api_id": "evt-reviewed",
            "calendar_api_id": "cal-reviewed",
            "name": "Silicon After Dark",
            "url": "siliconafterdark",
            "visibility": visibility,
            "start_at": "2026-10-15T02:00:00Z",
            "end_at": "2026-10-15T06:00:00Z",
            "geo_address_visibility": "guests-only",
            "geo_address_info": {"city": "San Jose", "address": "WITHHELD"},
            "coordinate": {"latitude": 37.123, "longitude": -121.987},
        },
        "calendar": {"api_id": "cal-reviewed", "name": "SemiAnalysis"},
        "guest_info": {"email": "SECRET@example.test"},
        "registration_questions": [{"label": "SECRET-FORM"}],
        "description_mirror": {
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "PRIVATE-PROSE"}]}
            ],
        },
    }


def _html(data: dict) -> str:
    return (
        '<script id="__NEXT_DATA__">'
        + json.dumps({"props": {"pageProps": {"initialData": {"kind": "event", "data": data}}}})
        + "</script>"
    )


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="linked-event",
        display_name="Linked Event",
        publisher="SemiAnalysis",
        seed_url=_URL,
        approved_origins=("https://luma.com",),
        region="bayarea",
        mode=CatalogSourceMode.LUMA_EVENT_HTML,
        page_limit=1,
        enabled=True,
        reviewed_at=datetime(2026, 10, 13, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1500,
        collection_horizon_days=90,
    )


@pytest.mark.parametrize("visibility", ["private", "unlisted"])
def test_discoverable_private_cards_discard_hidden_location_and_non_card_payload(
    visibility,
) -> None:
    event = linked_luma_event(HTMLParser(_html(_data(visibility))), _URL, "linked-event")
    assert event.title == "Silicon After Dark" and event.organizer_name == "SemiAnalysis"
    assert event.visibility == visibility and event.city == "San Jose"
    assert event.location_withheld is True
    assert event.geo is None and event.venue_name is None and event.description == ""
    assert event.raw == {} and event.host_names == () and event.attendance_count is None
    assert not any(value in repr(event) for value in ("WITHHELD", "SECRET", "PRIVATE-PROSE"))
    assert catalog_candidate_content_hash(event) != catalog_candidate_content_hash(
        replace(event, visibility=None)
    )


def test_public_event_with_guest_only_address_also_withholds_hidden_coordinates() -> None:
    event = linked_luma_event(HTMLParser(_html(_data("public"))), _URL, "linked-event")
    assert event.visibility == "public"
    assert event.geo is None and event.venue_name is None and event.city == "San Jose"


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "another-link"),
        ("visibility", "members-only"),
        ("api_id", "evt-another"),
    ],
)
def test_identity_or_visibility_drift_fails_closed(field, value) -> None:
    data = _data()
    data["event"][field] = value
    with pytest.raises(LumaEventFetchError, match="changed its linked identity"):
        linked_luma_event(HTMLParser(_html(data)), _URL, "linked-event")


async def test_exact_seed_uses_one_anonymous_get_without_api_enumeration() -> None:
    seen = []

    def route(request):
        seen.append(str(request.url))
        assert request.method == "GET" and "cookie" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(200, text=_html(_data()))

    fetcher = LumaEventCatalogFetcher(
        user_agent="test",
        now=lambda: datetime(2026, 10, 13, tzinfo=UTC),
        transport=httpx.MockTransport(route),
    )
    [event] = await fetcher.fetch(_source())
    assert seen == [_URL] and event.visibility == "private"


@pytest.mark.parametrize(
    "changes",
    [
        {"seed_url": _URL + "?invite=secret"},
        {"seed_url": "https://api2.luma.com/event/get",
         "approved_origins": ("https://api2.luma.com",)},
        {"approved_origins": ("https://luma.com", "https://api2.luma.com")},
        {"page_limit": 2},
    ],
)
async def test_unreviewed_seed_or_profile_is_rejected_before_fetch(changes) -> None:
    with pytest.raises(LumaEventFetchError, match="exact reviewed"):
        await LumaEventCatalogFetcher(user_agent="test").fetch(replace(_source(), **changes))


async def test_login_redirect_has_no_fallback() -> None:
    fetcher = LumaEventCatalogFetcher(
        user_agent="test",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(302, headers={"Location": "/login"})
        ),
    )
    with pytest.raises(LumaEventFetchError, match="redirect"):
        await fetcher.fetch(_source())
