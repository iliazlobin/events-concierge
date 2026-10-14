"""Directory completeness, provider time, role privacy and closed read-only egress."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from events_concierge.adapters.langchain.source import LangChainCatalogFetcher, LangChainFetchError
from events_concierge.application.catalog_refresh import catalog_refresh_lease_seconds
from events_concierge.composition import build_container
from events_concierge.config import Settings
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, PriceStatus, RegistrationStatus, Source

_NOW = datetime(2026, 10, 13, 12, tzinfo=UTC)
_SEED = "https://www.langchain.com/events"
_URL = "https://events.langchain.com/register/deep-agents/"
_ORIGINS = (
    "https://www.langchain.com",
    "https://events.langchain.com",
    "https://luma.com",
)


def _source() -> CatalogSource:
    return CatalogSource(
        source_key="langchain-events",
        display_name="LangChain Events",
        publisher="LangChain",
        seed_url=_SEED,
        approved_origins=_ORIGINS,
        region="global",
        mode=CatalogSourceMode.LANGCHAIN_HTML,
        enabled=True,
        reviewed_at=_NOW,
        review_expires_at=None,
        refresh_interval_minutes=1440,
        min_interval_ms=1500,
        page_limit=1,
    )


def _directory(urls: list[str], *, day: str = "2026-10-21") -> str:
    return (
        '<div class="v2-events_cms-list">'
        + "".join(
            f'<div class="events_cms-item" data-date="{day}" style="display:none">'
            f'<a class="v2-events_element" href="{url}"><h2>Directory title</h2></a>'
            '<span class="event-location">Virtual</span></div>'
            for url in urls
        )
        + "</div>"
    )


def _payload(*, event_id: str = "a56c5885-316a-4e2b-849e-ae9c8e599b45") -> dict[str, object]:
    return {
        "organization": {"name": "LangChain"},
        "event": [
            {
                "id": event_id,
                "title": "Agent Workshop",
                "start_time": "2026-10-22T01:00:00Z",
                "end_time": "2026-10-22T02:00:00Z",
                "event_type": "Webinar",
                "paid": False,
                "show_registration_page": True,
                "show_speakers_reg_page": True,
            }
        ],
        "speakers": [
            {"hidden": False, "user": {"first_name": "Public", "last_name": "Speaker"}},
            {"hidden": True, "user": {"first_name": "Hidden", "last_name": "Speaker"}},
        ],
        "staffs": [{"email": "staff@example.test", "first_name": "Internal", "last_name": "Staff"}],
        "organizers": [{"user": {"email": "owner@example.test", "first_name": "Internal"}}],
        "registration_form_data": {"private_field": "never-index-this"},
    }


def _goldcast(payload: dict[str, object]) -> str:
    return "<script>window.uberdata = " + json.dumps(payload) + ";</script>"


def _fetcher(documents: dict[str, str], seen: list[str]) -> LangChainCatalogFetcher:
    def route(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        seen.append(str(request.url))
        assert str(request.url) in documents
        return httpx.Response(200, text=documents[str(request.url)])

    return LangChainCatalogFetcher(
        user_agent="test",
        now=lambda: _NOW,
        clock=lambda: 0,
        sleep=_no_sleep,
        transport=httpx.MockTransport(route),
    )


async def _no_sleep(_: float) -> None:
    pass


async def test_hidden_cms_cards_are_collected_using_provider_instants_and_public_roles() -> None:
    seen: list[str] = []
    [event] = await _fetcher({_SEED: _directory([_URL]), _URL: _goldcast(_payload())}, seen).fetch(
        _source()
    )
    assert seen == [_SEED, _URL]
    assert event.start_at == datetime(2026, 10, 22, 1, tzinfo=UTC)
    assert event.organizer_name == "LangChain"
    assert event.speaker_names == ("Public Speaker",)
    assert event.venue_name == "Online" and event.city is None
    assert event.price_status is PriceStatus.FREE
    assert event.registration_status is RegistrationStatus.UNKNOWN
    assert event.source is Source.PUBLIC_JSONLD
    assert event.entity_profiles[0].profile_url == "https://www.langchain.com/"
    assert event.raw == {}
    assert "staff@example.test" not in repr(event)
    assert "never-index-this" not in repr(event)


async def test_whole_directory_is_scanned_and_duplicate_links_cost_one_get() -> None:
    urls = [f"https://events.langchain.com/register/workshop-{i}/" for i in range(9)]
    documents = {_SEED: _directory([*urls, urls[0]])}
    documents.update(
        {
            url: _goldcast(_payload(event_id=f"a56c5885-316a-4e2b-849e-{i:012d}"))
            for i, url in enumerate(urls)
        }
    )
    seen: list[str] = []
    assert len(await _fetcher(documents, seen).fetch(_source())) == 9
    assert len(seen) == 10


async def test_external_conference_links_are_not_fetched() -> None:
    seen: list[str] = []
    events = await _fetcher(
        {_SEED: _directory(["https://conference.example.test/events/"])}, seen
    ).fetch(_source())
    assert events == [] and seen == [_SEED]


async def test_unreviewed_server_pagination_does_not_publish_a_partial_directory() -> None:
    html = _directory([_URL]) + '<a class="w-pagination-next" href="?events_page=2">Next</a>'
    seen: list[str] = []
    with pytest.raises(LangChainFetchError, match="unreviewed server page"):
        await _fetcher({_SEED: html}, seen).fetch(_source())
    assert seen == [_SEED]


@pytest.mark.parametrize(
    "source",
    [
        replace(_source(), seed_url=_SEED + "?page=2"),
        replace(_source(), source_key="other-vendor"),
        replace(_source(), approved_origins=(_ORIGINS[0],)),
        replace(_source(), handoff_only=False),
        replace(_source(), page_limit=2),
        replace(_source(), min_interval_ms=1499),
    ],
)
async def test_profile_changes_are_denied_before_network(source: CatalogSource) -> None:
    seen: list[str] = []
    with pytest.raises(LangChainFetchError, match="reviewed directory profile"):
        await _fetcher({}, seen).fetch(source)
    assert seen == []


@pytest.mark.parametrize(
    "url",
    [
        "http://events.langchain.com/register/abc/",
        "https://events.langchain.com:443/register/abc/",
        "https://owner@events.langchain.com/register/abc/",
        "https://events.langchain.com/register/abc/?private=1",
        "https://events.langchain.com/register/abc/#secret",
        "https://luma.com/calendar/abc",
    ],
)
async def test_unsafe_registration_links_make_no_detail_request(url: str) -> None:
    seen: list[str] = []
    with pytest.raises(LangChainFetchError):
        await _fetcher({_SEED: _directory([url])}, seen).fetch(_source())
    assert seen == [_SEED]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(302, headers={"location": "https://outside.example.test/"}),
        httpx.Response(403),
        httpx.Response(429),
        httpx.Response(503),
    ],
)
async def test_redirects_and_access_failures_are_never_followed(response: httpx.Response) -> None:
    seen: list[str] = []

    def route(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return response

    fetcher = LangChainCatalogFetcher(user_agent="test", transport=httpx.MockTransport(route))
    with pytest.raises(LangChainFetchError):
        await fetcher.fetch(_source())
    assert seen == [_SEED]


@pytest.mark.parametrize(
    "html",
    [
        "<html>Unexpected login page</html>",
        '<div class="v2-events_cms-list"><div class="events_cms-item"></div></div>',
        "x" * 5_000_001,
    ],
)
async def test_changed_or_oversized_directory_fails_instead_of_publishing_empty(html: str) -> None:
    with pytest.raises(LangChainFetchError):
        await _fetcher({_SEED: html}, []).fetch(_source())


@pytest.mark.parametrize(
    "field,value",
    [
        ("start_time", "2026-10-22T01:00:00"),
        ("end_time", "2026-10-21T01:00:00Z"),
        ("id", "not-a-provider-identity"),
    ],
)
async def test_invalid_event_identity_or_time_aborts_the_refresh(field: str, value: str) -> None:
    payload = _payload()
    event = payload["event"]
    assert isinstance(event, list)
    event[0][field] = value
    with pytest.raises(LangChainFetchError):
        await _fetcher({_SEED: _directory([_URL]), _URL: _goldcast(payload)}, []).fetch(_source())


async def test_wrong_organization_is_rejected() -> None:
    payload = _payload()
    payload["organization"] = {"name": "Unrelated Company"}
    with pytest.raises(LangChainFetchError, match="another organization"):
        await _fetcher({_SEED: _directory([_URL]), _URL: _goldcast(payload)}, []).fetch(_source())


async def test_linked_private_luma_records_retain_basic_metadata_and_visibility() -> None:
    url = "https://luma.com/reviewed-workshop"
    data = {
        "api_id": "evt-reviewed",
        "event": {
            "api_id": "evt-reviewed",
            "name": "Linked workshop",
            "start_at": "2026-10-22T01:00:00Z",
            "end_at": "2026-10-22T02:00:00Z",
            "url": "reviewed-workshop",
            "calendar_api_id": "cal-reviewed",
            "visibility": "private",
        },
        "calendar": {"api_id": "cal-reviewed"},
    }
    html = (
        '<script id="__NEXT_DATA__">'
        + json.dumps({"props": {"pageProps": {"initialData": {"kind": "event", "data": data}}}})
        + "</script>"
    )
    [event] = await _fetcher({_SEED: _directory([url]), url: html}, []).fetch(_source())
    assert event.visibility == "private" and event.title == "Linked workshop"
    assert event.geo is None and event.venue_name is None and event.description == ""


async def test_past_and_outside_horizon_cards_do_not_start_detail_requests() -> None:
    html = _directory([_URL], day="2026-01-01") + _directory([_URL], day="2027-12-01")
    seen: list[str] = []
    assert await _fetcher({_SEED: html}, seen).fetch(_source()) == []
    assert seen == [_SEED]


def test_refresh_lease_reserves_linked_registration_requests_and_completion() -> None:
    lease = catalog_refresh_lease_seconds(_source(), floor_seconds=60)
    assert lease is not None and 2000 < lease < 3600


def test_composition_wires_the_directory_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("events_concierge.composition.init_engine", lambda _, **kwargs: None)
    container = build_container(
        Settings(
            discovery_sources="public_jsonld", cohere_api_key=None, google_calendar_enabled=False
        )
    )
    assert isinstance(
        container.catalog_refresh._fetchers[CatalogSourceMode.LANGCHAIN_HTML],
        LangChainCatalogFetcher,
    )
