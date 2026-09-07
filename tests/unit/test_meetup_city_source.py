"""Closed-contract tests for anonymous Meetup city-page JSON-LD ingestion."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.meetup_city.source import (
    MeetupCityCatalogFetcher,
    MeetupCityFetchError,
)
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import (
    CatalogSourceMode,
    PriceStatus,
    RegistrationStatus,
    Source,
)

_NOW = datetime(2026, 7, 30, 12, 0, tzinfo=UTC)
_CITY_URLS = {
    "meetup-sf": (
        "Meetup San Francisco",
        "https://www.meetup.com/find/us--ca--san-francisco/",
        "bay_area_9_county",
    ),
    "meetup-nyc": (
        "Meetup New York",
        "https://www.meetup.com/find/us--ny--new-york/",
        "new_york_metro",
    ),
}


def _source(source_key: str = "meetup-sf") -> CatalogSource:
    display_name, url, region = _CITY_URLS[source_key]
    return CatalogSource(
        source_key=source_key,
        display_name=display_name,
        publisher="Meetup City",
        seed_url=url,
        approved_origins=("https://www.meetup.com",),
        region=region,
        mode=CatalogSourceMode.MEETUP_CITY_JSONLD,
        enabled=True,
        reviewed_at=_NOW,
        review_expires_at=None,
        refresh_interval_minutes=120,
        min_interval_ms=1_500,
        page_limit=41,
        handoff_only=True,
        source_revision=2,
    )


def _event(
    event_id: str,
    *,
    title: str = "Public builders meetup",
    start_at: str = "2026-08-01T01:00:00.000Z",
    description: str = "",
    event_url: str | None = None,
    offers: object = None,
    image: object = None,
    status: object = "https://schema.org/EventScheduled",
) -> dict[str, object]:
    value: dict[str, object] = {
        "@context": "https://schema.org",
        "@type": "Event",
        "name": title,
        "url": event_url or f"https://www.meetup.com/public-builders/events/{event_id}/",
        "description": description,
        "startDate": start_at,
        "endDate": "2026-08-01T03:00:00.000Z",
        "eventStatus": status,
        "eventAttendanceMode": "https://schema.org/OfflineEventAttendanceMode",
        "location": {
            "@type": "Place",
            "name": "Civic Hall",
            "address": {
                "@type": "PostalAddress",
                "streetAddress": "1 Market St",
                "addressLocality": "San Francisco",
                "addressRegion": "CA",
                "addressCountry": "US",
            },
            "geo": {"latitude": 37.793, "longitude": -122.395},
        },
        "organizer": {
            "@type": "Organization",
            "name": "Public Builders",
            "url": "https://www.meetup.com/public-builders/",
        },
    }
    if offers is not None:
        value["offers"] = offers
    if image is not None:
        value["image"] = image
    return value


def _html(
    *jsonld_payloads: object,
    next_data: object | None = None,
    body: str = "",
) -> str:
    scripts = "".join(
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        for payload in jsonld_payloads
    )
    application = (
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(next_data)}</script>'
        if next_data is not None
        else ""
    )
    return f"<html><head>{scripts}</head><body>{application}{body}</body></html>"


def _detail_html(
    event: dict[str, object],
    *,
    details: tuple[str, ...] = (),
    hosted_by: tuple[str, str] | None = None,
    next_data: object | None = None,
    extra_jsonld: tuple[object, ...] = (),
) -> str:
    host = ""
    if hosted_by is not None:
        displayed_name, full_name = hosted_by
        host = (
            f'<a data-event-label="hosted-by" aria-label="Hosted by {displayed_name}" '
            f'href="{event["url"]}attendees/">'
            f'<img alt="Photo of the user {full_name}">'
            f'<p>Hosted by <span>{displayed_name}</span></p></a>'
        )
    section = ""
    if details:
        section = (
            "<section><h2>Details</h2>"
            + "".join(f"<p>{line}</p>" for line in details)
            + "</section><h2>Attendees</h2><p>Never part of Details</p>"
        )
    return _html(event, *extra_jsonld, next_data=next_data, body=host + section)


def _response(request: httpx.Request, html: str, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code,
        headers={"Content-Type": "text/html; charset=utf-8"},
        content=html.encode(),
        request=request,
    )


async def test_meetup_city_parses_only_future_public_event_jsonld_and_deduplicates_urls() -> None:
    paid_offer = {
        "@type": "Offer",
        "price": "25.00",
        "priceCurrency": "usd",
        "availability": "https://schema.org/InStock",
    }
    duplicate_sparse = _event("315000001", offers=paid_offer)
    duplicate_rich = _event(
        "315000001",
        description="A public description from schema.org.",
        offers=paid_offer,
        image={"@type": "ImageObject", "contentUrl": "https://secure.meetupstatic.com/e/1.jpg"},
    )
    past = _event("315000002", start_at="2026-07-29T01:00:00.000Z")
    external = _event(
        "315000003",
        event_url="https://events.example.test/public-builders/events/315000003/",
    )
    organizer = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "Meetup",
    }
    html = _html(
        organizer,
        [duplicate_sparse, past],
        [duplicate_rich, external],
        next_data={
            "events": [
                {
                    "@type": "Event",
                    "name": "Private application payload",
                    "rsvps": [{"member": {"name": "Never parsed"}}],
                }
            ]
        },
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == _CITY_URLS["meetup-sf"][1]:
            return _response(request, html)
        return _response(request, _detail_html(duplicate_rich))

    fetcher = MeetupCityCatalogFetcher(
        user_agent="events-concierge-test/1.0",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    events = await fetcher.fetch(_source())

    assert len(requests) == 2
    assert requests[0].method == "GET"
    assert str(requests[0].url) == _CITY_URLS["meetup-sf"][1]
    assert str(requests[1].url) == duplicate_rich["url"]
    assert requests[0].headers["accept"] == "text/html"
    assert requests[0].headers["user-agent"] == "events-concierge-test/1.0"
    assert len(events) == 1
    event = events[0]
    assert event.source is Source.PUBLIC_JSONLD
    assert event.source_event_id == "meetup:315000001"
    assert event.registration_url == ("https://www.meetup.com/public-builders/events/315000001/")
    assert event.title == "Public builders meetup"
    assert event.start_at == datetime(2026, 8, 1, 1, 0, tzinfo=UTC)
    assert event.end_at == datetime(2026, 8, 1, 3, 0, tzinfo=UTC)
    assert event.venue_name == "Civic Hall"
    assert event.city == "San Francisco"
    assert event.geo is not None
    assert (event.geo.lat, event.geo.lon) == (37.793, -122.395)
    assert event.description == "A public description from schema.org."
    assert event.organizer_name == "Public Builders"
    assert event.price_status is PriceStatus.PAID
    assert (event.price_min_cents, event.price_max_cents, event.price_currency) == (
        2_500,
        2_500,
        "USD",
    )
    assert event.registration_status is RegistrationStatus.OPEN
    assert event.host_names == ()
    assert event.attendance_count is None
    assert event.raw == {
        "event_id": "315000001",
        "event_url": "https://www.meetup.com/public-builders/events/315000001/",
        "event_status": "eventscheduled",
        "attendance_mode": "offlineeventattendancemode",
        "image_url": "https://secure.meetupstatic.com/e/1.jpg",
        "organizer_url": "https://www.meetup.com/public-builders/",
        "street_address": "1 Market St",
        "address_region": "CA",
        "address_country": "US",
        "detail_enrichment_status": "validated_no_new_evidence",
        "detail_evidence_codes": ["identity:root_event_jsonld"],
        "detail_request_attempted": 1,
        "detail_request_succeeded": 1,
        "detail_identity_matched": 1,
        "detail_details_section_found": 0,
        "detail_applied_field_count": 0,
    }
    assert "rsvps" not in event.raw
    assert "Never parsed" not in repr(event)


@pytest.mark.parametrize("source_key", ("meetup-sf", "meetup-nyc"))
async def test_each_reviewed_city_profile_performs_one_exact_anonymous_get(
    source_key: str,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _response(request, _html({"@type": "Organization", "name": "Meetup"}))

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    assert await fetcher.fetch(_source(source_key)) == []
    assert seen == [_CITY_URLS[source_key][1]]


async def test_free_sold_out_offer_and_relative_image_are_preserved_only_when_explicit() -> None:
    event = _event(
        "315000004",
        offers={
            "@type": "Offer",
            "price": 0,
            "availability": "https://schema.org/SoldOut",
        },
        image="/images/fallbacks/group-cover.webp",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(request, _html([event]))

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    [candidate] = await fetcher.fetch(_source())

    assert candidate.price_status is PriceStatus.FREE
    assert candidate.price_min_cents is None
    assert candidate.registration_status is RegistrationStatus.SOLD_OUT
    assert candidate.raw["image_url"] == (
        "https://www.meetup.com/images/fallbacks/group-cover.webp"
    )


@pytest.mark.parametrize(
    "source",
    (
        replace(_source(), seed_url="https://www.meetup.com/find/us--ca--oakland/"),
        replace(
            _source(),
            approved_origins=("https://www.meetup.com", "https://meetup.com"),
        ),
        replace(_source(), page_limit=2),
        replace(_source(), handoff_only=False),
        replace(_source(), mode=CatalogSourceMode.PUBLIC_JSONLD),
    ),
)
async def test_registry_edits_cannot_open_a_generic_meetup_client(source: CatalogSource) -> None:
    fetcher = MeetupCityCatalogFetcher(user_agent="test", now=lambda: _NOW)

    with pytest.raises(ValueError):
        await fetcher.fetch(source)


async def test_redirect_is_rejected_without_a_followup_request() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            302,
            headers={
                "Location": _CITY_URLS["meetup-sf"][1],
                "Content-Type": "text/html",
            },
            request=request,
        )

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(MeetupCityFetchError, match="reviewed endpoint"):
        await fetcher.fetch(_source())
    assert calls == 1


async def test_declared_or_streamed_oversized_response_fails_the_whole_refresh() -> None:
    oversized = "x" * 2_000_001

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(request, oversized)

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(MeetupCityFetchError, match="response-size limit"):
        await fetcher.fetch(_source())


async def test_malformed_event_jsonld_fails_instead_of_publishing_a_partial_city() -> None:
    html = (
        '<script type="application/ld+json">'
        f"{json.dumps([_event('315000005')])}"
        "</script>"
        '<script type="application/ld+json">{invalid</script>'
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(request, html)

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(MeetupCityFetchError, match="malformed JSON-LD block 2"):
        await fetcher.fetch(_source())


async def test_detail_requests_are_deterministically_sorted_capped_and_paced() -> None:
    listed = [_event(str(315000100 + index)) for index in range(42)]
    by_url = {str(event["url"]): event for event in listed}
    requests: list[str] = []
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requests.append(url)
        if url == _CITY_URLS["meetup-sf"][1]:
            return _response(request, _html(list(reversed(listed))))
        return _response(request, _detail_html(by_url[url]))

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        clock=lambda: 0.0,
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )
    events = await fetcher.fetch(_source())

    expected_urls = sorted(by_url)
    assert requests == [_CITY_URLS["meetup-sf"][1], *expected_urls[:40]]
    assert len(requests) == 41
    assert sleeps == [1.5] * 40
    assert [event.registration_url for event in events] == expected_urls
    assert all(event.raw["detail_request_attempted"] == 1 for event in events[:40])
    assert all(
        event.raw["detail_enrichment_status"] == "request_cap_not_fetched"
        and event.raw["detail_request_attempted"] == 0
        for event in events[40:]
    )


async def test_visible_details_can_supply_strong_free_and_conservative_where_evidence() -> None:
    listed = _event("315000200")
    listed.pop("location")
    detail = dict(listed)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _CITY_URLS["meetup-sf"][1]:
            return _response(request, _html([listed]))
        return _response(
            request,
            _detail_html(
                detail,
                details=(
                    "COST: FREE! Donations keep the venue open.",
                    "A longer public explanation rendered in the Details section.",
                    "WHERE: Civic Hall, 1 Market St, San Francisco, CA 94105, USA",
                ),
            ),
        )

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    [candidate] = await fetcher.fetch(_source())

    assert candidate.price_status is PriceStatus.FREE
    assert candidate.price_min_cents is None
    assert candidate.registration_status is RegistrationStatus.UNKNOWN
    assert candidate.description == ("A longer public explanation rendered in the Details section.")
    assert candidate.venue_name == "Civic Hall"
    assert candidate.city == "San Francisco"
    assert candidate.raw["street_address"] == "1 Market St"
    assert candidate.raw["address_region"] == "CA"
    assert candidate.raw["address_country"] == "US"
    assert candidate.raw["detail_enrichment_status"] == "enriched"
    assert candidate.raw["detail_request_attempted"] == 1
    assert candidate.raw["detail_request_succeeded"] == 1
    assert candidate.raw["detail_identity_matched"] == 1
    assert candidate.raw["detail_details_section_found"] == 1
    assert candidate.raw["detail_applied_field_count"] == 7
    assert candidate.raw["detail_evidence_codes"] == [
        "description:visible_details",
        "identity:root_event_jsonld",
        "location:visible_details_where",
        "price:visible_details_exact_free",
        "visible_details:bounded_section",
        "visible_details:exact_free",
        "visible_details:where_us_address",
    ]


async def test_structured_city_and_detail_fields_win_over_visible_hints() -> None:
    listed = _event(
        "315000201",
        description="City structured description.",
        offers={
            "@type": "Offer",
            "price": "25.00",
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
        },
    )
    detail = dict(listed)
    detail["description"] = (
        "A substantially longer structured Event JSON-LD description from the detail page."
    )
    detail["offers"] = {
        "@type": "Offer",
        "price": 0,
        "availability": "https://schema.org/SoldOut",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _CITY_URLS["meetup-sf"][1]:
            return _response(request, _html([listed]))
        return _response(
            request,
            _detail_html(
                detail,
                details=(
                    "COST: FREE!",
                    "WHERE: Other Hall, 99 Other St, Oakland, CA 94612",
                    "Visible prose cannot displace a structured description.",
                ),
                next_data={"attendees": [{"name": "Private Person"}]},
            ),
        )

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    [candidate] = await fetcher.fetch(_source())

    assert candidate.description == detail["description"]
    assert candidate.price_status is PriceStatus.PAID
    assert candidate.price_min_cents == candidate.price_max_cents == 2_500
    assert candidate.price_currency == "USD"
    assert candidate.registration_status is RegistrationStatus.OPEN
    assert candidate.venue_name == "Civic Hall"
    assert candidate.city == "San Francisco"
    assert candidate.raw["street_address"] == "1 Market St"
    assert candidate.raw["detail_applied_field_count"] == 1
    assert "price:visible_details_exact_free" not in candidate.raw["detail_evidence_codes"]
    assert "Private Person" not in repr(candidate)


async def test_explicit_host_assertion_retains_person_separately_from_organizing_group() -> None:
    listed = _event(
        "315000209",
        title="Chess at Alamo Square Park",
        description="Friendly regular chess and Bughouse available.",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _CITY_URLS["meetup-sf"][1]:
            return _response(request, _html([listed]))
        return _response(
            request,
            _detail_html(listed, hosted_by=("Jessica A.", "Jessica Aiello")).replace(
                "</body>",
                '<h2>Attendees</h2><a data-testid="attendee-card">Private Person</a></body>',
            ),
        )

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    [candidate] = await fetcher.fetch(_source())

    assert candidate.organizer_name == "Public Builders"
    assert candidate.host_names == ("Jessica Aiello",)
    assert candidate.raw["detail_enrichment_status"] == "enriched"
    assert candidate.raw["detail_applied_field_count"] == 1
    assert "host:visible_hosted_by" in candidate.raw["detail_evidence_codes"]
    assert "Private Person" not in repr(candidate)


async def test_host_assertion_requires_the_identity_matched_event_attendee_link() -> None:
    listed = _event("315000210")
    detail = _detail_html(listed, hosted_by=("Jessica A.", "Jessica Aiello")).replace(
        f'{listed["url"]}attendees/',
        "https://www.meetup.com/other-group/events/999/attendees/",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _CITY_URLS["meetup-sf"][1]:
            return _response(request, _html([listed]))
        return _response(request, detail)

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    [candidate] = await fetcher.fetch(_source())

    assert candidate.host_names == ()
    assert "host:visible_hosted_by" not in candidate.raw["detail_evidence_codes"]


async def test_richer_visible_details_description_beats_short_structured_teasers() -> None:
    listed = _event("315000205", description="Short city teaser.")
    detail = dict(listed)
    detail["description"] = "Short detail teaser."
    visible_description = (
        "This complete server-rendered Details description is substantially richer than either "
        "structured teaser while remaining bounded public text."
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _CITY_URLS["meetup-sf"][1]:
            return _response(request, _html([listed]))
        return _response(
            request,
            _detail_html(detail, details=(visible_description,)),
        )

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    [candidate] = await fetcher.fetch(_source())

    assert candidate.description == visible_description
    assert "description:visible_details" in candidate.raw["detail_evidence_codes"]
    assert candidate.raw["detail_applied_field_count"] == 1


async def test_identity_mismatch_cannot_enrich_from_visible_or_application_data() -> None:
    listed = _event("315000202")
    listed.pop("location")
    mismatched = _event("315000999", title=str(listed["name"]))

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _CITY_URLS["meetup-sf"][1]:
            return _response(request, _html([listed]))
        return _response(
            request,
            _detail_html(
                mismatched,
                details=("Admission: Free", "Private-looking visible text."),
                next_data={
                    "@type": "Event",
                    "name": listed["name"],
                    "url": listed["url"],
                    "startDate": listed["startDate"],
                    "description": "Never trusted application description",
                    "attendees": [{"member": {"name": "Never Parsed"}}],
                    "rsvps": 900,
                },
            ),
        )

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    [candidate] = await fetcher.fetch(_source())

    assert candidate.price_status is PriceStatus.UNKNOWN
    assert candidate.description == ""
    assert candidate.venue_name is None
    assert candidate.raw["detail_enrichment_status"] == "identity_mismatch"
    assert candidate.raw["detail_request_attempted"] == 1
    assert candidate.raw["detail_request_succeeded"] == 0
    assert candidate.raw["detail_identity_matched"] == 0
    assert candidate.raw["detail_applied_field_count"] == 0
    assert "Never Parsed" not in repr(candidate)
    assert "Never trusted" not in repr(candidate)


async def test_detail_redirect_is_best_effort_with_safe_status_only() -> None:
    listed = _event("315000203")
    detail_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal detail_calls
        if str(request.url) == _CITY_URLS["meetup-sf"][1]:
            return _response(request, _html([listed]))
        detail_calls += 1
        return httpx.Response(
            302,
            headers={
                "Location": "https://www.meetup.com/other/events/315000203/",
                "Content-Type": "text/html",
            },
            request=request,
        )

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    [candidate] = await fetcher.fetch(_source())

    assert detail_calls == 1
    assert candidate.raw["detail_enrichment_status"] == "redirect_refused"
    assert candidate.raw["detail_request_attempted"] == 1
    assert candidate.raw["detail_request_succeeded"] == 0
    assert "Location" not in candidate.raw


async def test_declared_oversized_detail_keeps_the_validated_city_candidate() -> None:
    listed = _event("315000207", description="Trusted city description.")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _CITY_URLS["meetup-sf"][1]:
            return _response(request, _html([listed]))
        return httpx.Response(
            200,
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "Content-Length": "2000001",
            },
            content=b"x",
            request=request,
        )

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    [candidate] = await fetcher.fetch(_source())

    assert candidate.description == "Trusted city description."
    assert candidate.raw["detail_enrichment_status"] == "response_too_large"
    assert candidate.raw["detail_request_attempted"] == 1
    assert candidate.raw["detail_request_succeeded"] == 0


async def test_exact_visible_usd_amount_is_bounded_but_attend_button_is_not_registration() -> None:
    listed = _event("315000204")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _CITY_URLS["meetup-sf"][1]:
            return _response(request, _html([listed]))
        return _response(
            request,
            _html(
                listed,
                body=(
                    "<section><h2>Details</h2>"
                    "<p>Admission: $12.50/pp, taxes included.</p>"
                    "<button>Attend</button></section>"
                ),
            ),
        )

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    [candidate] = await fetcher.fetch(_source())

    assert candidate.price_status is PriceStatus.PAID
    assert candidate.price_min_cents == candidate.price_max_cents == 1_250
    assert candidate.price_currency == "USD"
    assert candidate.registration_status is RegistrationStatus.UNKNOWN
    assert candidate.raw["detail_evidence_codes"] == [
        "identity:root_event_jsonld",
        "price:visible_details_exact_usd",
        "visible_details:bounded_section",
        "visible_details:exact_usd",
    ]


async def test_empty_details_heading_is_observable_without_inventing_event_metadata() -> None:
    listed = _event("315000206")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _CITY_URLS["meetup-sf"][1]:
            return _response(request, _html([listed]))
        return _response(
            request,
            _html(listed, body="<h2>Details</h2><button>Attend</button><h2>Attendees</h2>"),
        )

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    [candidate] = await fetcher.fetch(_source())

    assert candidate.registration_status is RegistrationStatus.UNKNOWN
    assert candidate.raw["detail_enrichment_status"] == "validated_no_new_evidence"
    assert candidate.raw["detail_details_section_found"] == 1
    assert candidate.raw["detail_applied_field_count"] == 0
    assert candidate.raw["detail_evidence_codes"] == [
        "identity:root_event_jsonld",
        "visible_details:bounded_section",
    ]


async def test_valueless_presentation_attributes_do_not_abort_visible_details() -> None:
    listed = _event("315000208")

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == _CITY_URLS["meetup-sf"][1]:
            return _response(request, _html([listed]))
        return _response(
            request,
            _html(
                listed,
                body=(
                    "<section class><h2>Details</h2>"
                    "<p class aria-hidden style>COST: FREE!</p>"
                    "<h2>Attendees</h2></section>"
                ),
            ),
        )

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        transport=httpx.MockTransport(handler),
    )
    [candidate] = await fetcher.fetch(_source())

    assert candidate.price_status is PriceStatus.FREE
    assert candidate.price_min_cents is None
    assert candidate.price_max_cents is None
    assert candidate.raw["detail_enrichment_status"] == "enriched"
    assert candidate.raw["detail_details_section_found"] == 1


async def test_repeated_city_fetches_observe_the_per_host_pacing_floor() -> None:
    readings = iter((0.0, 0.0, 1.5))
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def handler(request: httpx.Request) -> httpx.Response:
        return _response(request, _html({"@type": "Organization", "name": "Meetup"}))

    fetcher = MeetupCityCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        clock=lambda: next(readings),
        sleep=sleep,
        transport=httpx.MockTransport(handler),
    )

    assert await fetcher.fetch(_source()) == []
    assert await fetcher.fetch(_source()) == []
    assert sleeps == [1.5]
