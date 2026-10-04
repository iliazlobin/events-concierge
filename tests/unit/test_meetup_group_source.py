"""Public export completeness, visibility, identity, timing, and egress boundaries."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.meetup_group.source import (
    MeetupGroupCalendarCatalogFetcher,
    MeetupGroupFetchError,
    _candidates,
    reviewed_group_slug,
)
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus, Source
from events_concierge.ports.sources import (
    SourceAccessDeniedError,
    SourceRateLimitedError,
    SourceTransientError,
)

_NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
_URL = "https://www.meetup.com/public-builders/events/ical/"


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="meetup-group-public-builders", display_name="Public Builders",
        publisher="Meetup Group", seed_url=_URL, approved_origins=("https://www.meetup.com",),
        region="bay_area_9_county", mode=CatalogSourceMode.MEETUP_GROUP_ICS,
        enabled=True, reviewed_at=_NOW, review_expires_at=None,
        refresh_interval_minutes=360, min_interval_ms=1500, page_limit=101,
    )


def _event(identity: str = "316001", *, extra: str = "", visibility: str = "PUBLIC") -> str:
    return (
        "BEGIN:VEVENT\r\n" + f"UID:event_{identity}@meetup.com\r\n"
        f"URL;VALUE=URI:https://www.meetup.com/public-builders/events/{identity}/\r\n"
        "DTSTART;TZID=America/Los_Angeles:20261005T180000\r\n"
        "DTEND;TZID=America/Los_Angeles:20261005T200000\r\n"
        "SUMMARY:Public builders meetup\r\n" + f"CLASS:{visibility}\r\n"
        "STATUS:CONFIRMED\r\n" + extra + "END:VEVENT\r\n"
    )


def _feed(*events: str) -> str:
    return "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n" + "".join(events) + "END:VCALENDAR\r\n"


@pytest.mark.parametrize("url", [
    _URL + "?token=private", _URL + "#fragment", _URL.replace("www.meetup.com", "meetup.com"),
    _URL.replace("https:", "http:"), _URL.replace("events/ical/", "events/ical"),
    _URL.replace("events/ical/", "events/rss/"), _URL.replace("public-builders", "../private"),
    _URL.replace("www.meetup.com", "user@www.meetup.com"),
    _URL.replace("www.meetup.com", "www.meetup.com:443"),
])
def test_export_identity_refuses_other_endpoints_and_tokens(url: str) -> None:
    assert reviewed_group_slug(url) is None


def test_explicit_public_occurrences_use_native_timezone_and_drop_private_payloads() -> None:
    candidates = _candidates(_feed(
        _event(extra="ATTENDEE:mailto:private@example.com\r\nDESCRIPTION:private roster\r\n"),
        _event("316002", visibility="PRIVATE"), _event("316003", visibility="CONFIDENTIAL"),
    ), "public-builders", _NOW)
    assert len(candidates) == 1
    assert candidates[0].source is Source.PUBLIC_JSONLD
    assert candidates[0].source_event_id == "meetup:316001"
    assert candidates[0].start_at == datetime(2026, 10, 6, 1, tzinfo=UTC)
    assert candidates[0].end_at == datetime(2026, 10, 6, 3, tzinfo=UTC)
    assert "private" not in str(candidates[0].raw)
    assert candidates[0].description == ""


def test_unknown_visibility_and_cancelled_or_past_occurrences_are_excluded() -> None:
    payload = _feed(_event().replace("CLASS:PUBLIC\r\n", ""),
        _event("316002").replace("STATUS:CONFIRMED", "STATUS:CANCELLED"),
        _event("316003").replace("20261005", "20261001"))
    assert _candidates(payload, "public-builders", _NOW) == []


def test_dated_occurrence_ids_preserve_identity_without_expanding_recurrence() -> None:
    events = _candidates(_feed(_event("jbxnztyjcpbmb"), _event("jbxnztyjcpbvb")),
                         "public-builders", _NOW)
    assert [event.source_event_id for event in events] == [
        "meetup:jbxnztyjcpbmb", "meetup:jbxnztyjcpbvb",
    ]
    assert events[0].registration_url.endswith("/events/jbxnztyjcpbmb/")


@pytest.mark.parametrize("identity", ["0", "0012", "abc", "jbxnztyjcpbm", "JBXNZTYJCPBMB",
                                     "jbxnztyjcpbm1", "1" * 21, "316001?member=1"])
def test_unknown_event_identity_shapes_fail_closed(identity: str) -> None:
    with pytest.raises(MeetupGroupFetchError):
        _candidates(_feed(_event(identity)), "public-builders", _NOW)


@pytest.mark.parametrize("payload", [
    _feed(_event()).replace("END:VCALENDAR\r\n", ""),
    _feed(_event()) + "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
    _feed(_event(), _event()),
    _feed(_event()).replace("events/316001/", "events/316002/"),
    _feed(_event()).replace("/public-builders/events/", "/another-group/events/"),
    _feed(_event()).replace("www.meetup.com/public-builders", "unreviewed.example/public-builders"),
    _feed(_event()).replace("CLASS:PUBLIC", "CLASS:PUBLIC\r\nCLASS:PUBLIC"),
    _feed(_event(extra="RRULE:FREQ=DAILY;COUNT=20\r\n")),
    _feed(_event(extra="RECURRENCE-ID:20261005T010000Z\r\n")),
    _feed(_event()).replace("TZID=America/Los_Angeles", "TZID=Invalid/Zone"),
    _feed(_event()).replace(";TZID=America/Los_Angeles", ""),
    _feed(_event()).replace("DTEND;TZID=America/Los_Angeles:20261005T200000", "DTEND:20260101T000000Z"),
    _feed(_event()).replace("20261005T180000", "20260308T023000"),
    _feed(_event()).replace("20261005T180000", "20261101T013000"),
    _feed(*(_event(str(316000 + n)) for n in range(101))),
    _feed(_event()).replace("SUMMARY:Public builders meetup", "SUMMARY:" + "x" * 20001),
])
def test_incomplete_or_ambiguous_feeds_fail_as_a_whole(payload: str) -> None:
    with pytest.raises(MeetupGroupFetchError):
        _candidates(payload, "public-builders", _NOW)


def test_timezone_components_do_not_expand_event_recurrence_and_folded_titles_work() -> None:
    tz = ("BEGIN:VTIMEZONE\r\nTZID:America/Los_Angeles\r\nBEGIN:STANDARD\r\n"
          "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU\r\nEND:STANDARD\r\nEND:VTIMEZONE\r\n")
    payload = _feed(_event()).replace("VERSION:2.0\r\n", "VERSION:2.0\r\n" + tz)
    payload = payload.replace("SUMMARY:Public builders meetup", "SUMMARY:Public builders\r\n  meetup\\, friends")
    assert _candidates(payload, "public-builders", _NOW)[0].title == "Public builders meetup, friends"


async def test_export_and_all_public_details_share_pacing_and_safe_enrichment() -> None:
    calls: list[str] = []
    delays: list[float] = []
    clock = [0.0]
    async def sleep(seconds: float) -> None:
        delays.append(seconds)
        clock[0] += seconds
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if str(request.url) == _URL:
            return httpx.Response(200, text=_feed(_event(), _event("jbxnztyjcpbmb"),
                _event("316003", visibility="PRIVATE")), headers={"Content-Type":"text/calendar"})
        detail = {"@context":"https://schema.org","@type":"Event","name":"Public builders meetup",
            "url":str(request.url),"startDate":"2026-10-06T01:00:00Z","endDate":"2026-10-06T03:00:00Z",
            "description":"Public workshop", "offers":{"price":0,"priceCurrency":"USD"},
            "location":{"@type":"Place","name":"Civic Hall",
                "address":{"@type":"PostalAddress","addressLocality":"San Francisco"},
                "geo":{"latitude":37.79,"longitude":-122.39}}}
        html = '<script type="application/ld+json">' + json.dumps(detail) + '</script>'
        html += '<script id="__NEXT_DATA__" type="application/json">{"private_attendee":"secret"}</script>'
        return httpx.Response(200, text=html, headers={"Content-Type":"text/html"})
    fetcher = MeetupGroupCalendarCatalogFetcher(user_agent="test",now=lambda:_NOW,
        clock=lambda:clock[0],sleep=sleep,transport=httpx.MockTransport(handler))
    candidates = await fetcher.fetch(_source())
    assert len(calls) == 3 and len(candidates) == 2
    assert delays == [1.5,1.5]
    assert all(c.geo is not None and c.city == "San Francisco" for c in candidates)
    assert all(c.price_status is PriceStatus.FREE for c in candidates)
    assert all("private_attendee" not in str(c.raw) and "secret" not in str(c.raw) for c in candidates)


async def test_horizon_filters_before_details_and_detail_failure_keeps_valid_export() -> None:
    calls: list[str] = []
    async def sleep(_: float) -> None:
        pass
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == _URL:
            return httpx.Response(200,text=_feed(_event(),_event("316002").replace("20261005","20270201")),
                headers={"Content-Type":"text/calendar"})
        return httpx.Response(404)
    fetcher = MeetupGroupCalendarCatalogFetcher(user_agent="test",now=lambda:_NOW,
        sleep=sleep,transport=httpx.MockTransport(handler))
    result = await fetcher.fetch(_source())
    assert len(calls) == 2 and len(result) == 1
    assert result[0].raw["detail_enrichment_status"] == "http_error"


@pytest.mark.parametrize(("status","headers","error"), [
    (403,{},SourceAccessDeniedError), (429,{"Retry-After":"120"},SourceRateLimitedError),
    (429,{"Retry-After":"Sat, 03 Oct 2026 12:03:00 GMT"},SourceRateLimitedError),
    (503,{},SourceTransientError), (302,{"Location":"https://unreviewed.example"},MeetupGroupFetchError),
    (200,{"Content-Type":"text/html"},MeetupGroupFetchError),
    (200,{"Content-Type":"text/calendar","Content-Length":"2000001"},MeetupGroupFetchError),
])
async def test_provider_failures_never_continue_or_follow_redirects(
    status: int, headers: dict[str,str], error: type[Exception],
) -> None:
    calls: list[str] = []
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status,text=_feed(_event()),headers=headers)
    fetcher = MeetupGroupCalendarCatalogFetcher(user_agent="test",now=lambda:_NOW,
        transport=httpx.MockTransport(handler))
    with pytest.raises(error) as caught:
        await fetcher.fetch(_source())
    assert calls == [_URL]
    if status == 429:
        assert caught.value.retry_after_seconds == (120 if headers["Retry-After"] == "120" else 180)


@pytest.mark.parametrize(("status", "error"), [(403, SourceAccessDeniedError), (429, SourceRateLimitedError)])
async def test_detail_access_and_throttle_signals_stop_further_egress(status: int, error: type[Exception]) -> None:
    calls: list[str] = []
    async def sleep(_: float) -> None:
        pass
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == _URL:
            return httpx.Response(200,text=_feed(_event(), _event("316002")),
                headers={"Content-Type":"text/calendar"})
        return httpx.Response(status,headers={"Retry-After":"120"})
    fetcher = MeetupGroupCalendarCatalogFetcher(user_agent="test",now=lambda:_NOW,
        sleep=sleep,transport=httpx.MockTransport(handler))
    with pytest.raises(error):
        await fetcher.fetch(_source())
    assert len(calls) == 2


async def test_transport_timeout_uses_durable_backoff() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("test transport timeout", request=request)
    fetcher = MeetupGroupCalendarCatalogFetcher(user_agent="test", now=lambda:_NOW,
                                               transport=httpx.MockTransport(handler))
    with pytest.raises(SourceTransientError) as caught:
        await fetcher.fetch(_source())
    assert caught.value.retry_after_seconds == 30


async def test_streaming_feed_cannot_outlive_its_total_request_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"BEGIN:VCALENDAR\r\n"
            await asyncio.sleep(1)
            yield b"END:VCALENDAR\r\n"
    monkeypatch.setattr("events_concierge.adapters.meetup_group.source._FETCH_TIMEOUT_S", 0.01)
    fetcher = MeetupGroupCalendarCatalogFetcher(user_agent="test", now=lambda:_NOW,
        transport=httpx.MockTransport(lambda _:httpx.Response(200,stream=SlowStream(),
            headers={"Content-Type":"text/calendar"})))
    with pytest.raises(SourceTransientError):
        await fetcher.fetch(_source())


@pytest.mark.parametrize("changes", [
    {"page_limit":100}, {"min_interval_ms":1000}, {"handoff_only":False},
    {"seed_url":_URL + "?member=1"}, {"mode":CatalogSourceMode.MEETUP_CITY_JSONLD},
    {"approved_origins":("https://www.meetup.com","https://other.example")},
])
async def test_unreviewed_contract_changes_perform_zero_requests(changes: dict[str,object]) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        pytest.fail("unreviewed contract issued a request")
    fetcher = MeetupGroupCalendarCatalogFetcher(user_agent="test",now=lambda:_NOW,
        transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError):
        await fetcher.fetch(replace(_source(),**changes))
