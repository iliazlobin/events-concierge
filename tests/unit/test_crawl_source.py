"""Public-crawl source filtering tests (FR-3.1, FR-5.10)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from events_concierge.adapters.crawl.source import PublicJsonLdSource
from events_concierge.composition import build_container
from events_concierge.config import Settings
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, Source
from events_concierge.domain.events import CandidateEvent
from events_concierge.domain.request import RequestConstraints
from events_concierge.ports.sources import SourceTransientError


async def test_public_crawl_returns_all_future_fixture_events_by_default() -> None:
    """Paid and unknown public listings remain discoverable unless a user asks for free-only (FR-4.6)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = PublicJsonLdSource(
        user_agent="test",
        fixture_events=[
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id="past",
                title="Past free event",
                start_at=now - timedelta(minutes=1),
                registration_url="https://example.test/past",
                is_free=True,
            ),
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id="paid",
                title="Future paid event",
                start_at=now + timedelta(hours=1),
                registration_url="https://example.test/paid",
                is_free=False,
            ),
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id="free",
                title="Future free event",
                start_at=now + timedelta(hours=2),
                registration_url="https://example.test/free",
                is_free=True,
            ),
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id="unknown",
                title="Future price unknown event",
                start_at=now + timedelta(hours=3),
                registration_url="https://example.test/unknown",
            ),
        ],
        now=lambda: now,
    )

    events = await source.discover(RequestConstraints())

    assert [event.source_event_id for event in events] == ["paid", "free", "unknown"]


async def test_public_crawl_free_only_filter_requires_verified_free_price() -> None:
    """A free-only request excludes both paid and unverified price states (FR-4.6)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = PublicJsonLdSource(
        user_agent="test",
        fixture_events=[
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id="free",
                title="Future free event",
                start_at=now + timedelta(hours=1),
                registration_url="https://example.test/free",
                is_free=True,
            ),
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id="paid",
                title="Future paid event",
                start_at=now + timedelta(hours=2),
                registration_url="https://example.test/paid",
                is_free=False,
            ),
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id="unknown",
                title="Future price unknown event",
                start_at=now + timedelta(hours=3),
                registration_url="https://example.test/unknown",
            ),
        ],
        now=lambda: now,
    )

    events = await source.discover(RequestConstraints(budget_free=True))

    assert [event.source_event_id for event in events] == ["free"]


async def test_public_crawl_paces_repeated_fetches_for_the_same_host() -> None:
    """The human-cadence floor applies across calls, not merely between one batch's seed URLs."""
    current = 100.0
    slept: list[float] = []

    def clock() -> float:
        return current

    async def sleep(delay: float) -> None:
        nonlocal current
        slept.append(delay)
        current += delay

    source = PublicJsonLdSource(
        user_agent="test",
        min_interval_ms=1500,
        clock=clock,
        sleep=sleep,
    )

    await source._wait_for_host_slot("https://luma.com/genai-sf")
    await source._wait_for_host_slot("https://luma.com/another-calendar")
    await source._wait_for_host_slot("https://events.example.test/calendar")

    assert slept == [1.5]


async def test_catalog_fetch_rejects_an_unapproved_redirect_before_requesting_it() -> None:
    """A reviewed source cannot bounce the crawler onto an unapproved platform origin (FR-10.3)."""
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302,
            headers={"location": "https://unapproved.example.test/events"},
            request=request,
        )

    source = PublicJsonLdSource(
        user_agent="test",
        min_interval_ms=1,
        transport=httpx.MockTransport(handler),
    )
    catalog_source = CatalogSource(
        source_key="reviewed-calendar",
        display_name="Reviewed calendar",
        publisher="Test publisher",
        seed_url="https://events.example.test/calendar",
        approved_origins=("https://events.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )

    with pytest.raises(ValueError, match="reviewed origin policy"):
        await source.fetch(catalog_source)
    assert requested == ["https://events.example.test/calendar"]


def test_crawl_seed_setting_requires_explicit_owner_approved_urls() -> None:
    settings = Settings(
        crawl_seed_urls=" https://luma.com/genai-sf, https://events.example.test/calendar "
    )

    assert settings.crawl_seeds == [
        "https://luma.com/genai-sf",
        "https://events.example.test/calendar",
    ]


def _reviewed_source() -> CatalogSource:
    return CatalogSource(
        source_key="reviewed-calendar",
        display_name="Reviewed calendar",
        publisher="Test publisher",
        seed_url="https://events.example.test/calendar",
        approved_origins=("https://events.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )


@pytest.mark.parametrize("failure", ["timeout", "http_503", "http_404", "http_429"])
async def test_catalog_fetch_failure_is_not_an_empty_success(failure: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if failure == "timeout":
            raise httpx.ReadTimeout("unavailable", request=request)
        return httpx.Response(int(failure.removeprefix("http_")), request=request)

    source = PublicJsonLdSource(user_agent="test", transport=httpx.MockTransport(handler))
    expected = SourceTransientError if failure in {"timeout", "http_503"} else ValueError
    with pytest.raises(expected, match="catalog HTTP request"):
        await source.fetch(_reviewed_source())


@pytest.mark.parametrize("location", [None, "/calendar"])
async def test_catalog_rejects_missing_or_looping_redirects(location: str | None) -> None:
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            302, headers={"location": location} if location else {}, request=request
        )

    source = PublicJsonLdSource(user_agent="test", transport=httpx.MockTransport(handler))
    with pytest.raises(ValueError, match="reviewed origin policy"):
        await source.fetch(_reviewed_source())
    assert len(requested) == (5 if location else 1)


@pytest.mark.parametrize(
    "html",
    [
        '<script type="application/ld+json">{broken</script>',
        '<script type="application/ld+json">{"@type":"Event","name":"No date"}</script>',
        '<script type="application/ld+json">{"@type":"Event","name":"Bad date","startDate":"oops"}</script>',
    ],
)
async def test_catalog_parse_failure_is_not_an_empty_success(html: str) -> None:
    source = PublicJsonLdSource(
        user_agent="test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=html, request=request)
        ),
    )
    with pytest.raises(ValueError, match="catalog JSON-LD"):
        await source.fetch(_reviewed_source())


@pytest.mark.parametrize(
    "html",
    [
        "<html><body>No upcoming events</body></html>",
        '<script type="application/ld+json">[]</script>',
    ],
)
async def test_catalog_valid_empty_document_remains_successful(html: str) -> None:
    source = PublicJsonLdSource(
        user_agent="test",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, text=html, request=request)
        ),
    )
    assert await source.fetch(_reviewed_source()) == []


async def test_legacy_discovery_remains_best_effort_after_failed_seed() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/failed":
            return httpx.Response(503, request=request)
        return httpx.Response(
            200,
            text='<script type="application/ld+json">{broken</script>'
            '<script type="application/ld+json">{"@type":"Event","name":"Available",'
            '"startDate":"2030-01-01T12:00:00Z"}</script>',
            request=request,
        )

    source = PublicJsonLdSource(
        user_agent="test",
        min_interval_ms=1,
        seed_urls=["https://events.example.test/failed", "https://events.example.test/good"],
        transport=httpx.MockTransport(handler),
    )
    assert [event.title for event in await source.discover(RequestConstraints())] == ["Available"]


async def test_disabled_public_jsonld_source_never_fetches_a_populated_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Excluding the source disables even an explicit live seed (FR-3.9/FR-10.1)."""

    async def must_not_discover(
        self: PublicJsonLdSource, constraints: RequestConstraints
    ) -> list[CandidateEvent]:
        del self, constraints
        raise AssertionError("disabled public_jsonld source was invoked")

    monkeypatch.setattr("events_concierge.composition.init_engine", lambda _, **kwargs: None)
    monkeypatch.setattr(PublicJsonLdSource, "discover", must_not_discover)
    container = build_container(
        Settings(
            discovery_sources="luma",
            crawl_seed_urls="https://luma.com/genai-sf",
        )
    )

    assert await container.discovery.discover(RequestConstraints()) == []
