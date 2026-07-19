"""Composition selection tests for explicit Cohere and Google Calendar activation (FR-4.2/9.6)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from types import ModuleType
from uuid import UUID, uuid4

import httpx
import pytest

from events_concierge.adapters.google_calendar.calendar import GoogleCalendarAdapter
from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.adapters.mock.ranking_feedback import InMemoryRankingFeedback
from events_concierge.adapters.ranking.cohere import CohereRerankCrossEncoder
from events_concierge.adapters.ranking.ranker import (
    DeterministicCrossEncoder,
    InMemoryRankingProfiles,
    PersonalizedRanker,
)
from events_concierge.composition import build_container
from events_concierge.config import Settings
from events_concierge.domain.events import CanonicalEvent
from events_concierge.domain.request import EventRequest, RequestConstraints
from events_concierge.ports.google_calendar import GoogleCalendarAccess, GoogleCalendarBinding


class FixtureGoogleAccess:
    """Provisioning seam for a fake tenant-scoped short-lived Google bearer token."""

    async def get_access(self, tenant_id: UUID) -> GoogleCalendarAccess:
        del tenant_id
        return GoogleCalendarAccess("fixture-google-token")


class FixtureGoogleBindings:
    """Provisioning seam for one fake app-created secondary calendar binding."""

    def __init__(self) -> None:
        self._binding = GoogleCalendarBinding(
            "events-concierge@group.calendar.google.com",
            ("events-concierge@group.calendar.google.com",),
        )

    async def get_binding(self, tenant_id: UUID) -> GoogleCalendarBinding | None:
        del tenant_id
        return self._binding

    async def upsert_binding(self, tenant_id: UUID, binding: GoogleCalendarBinding) -> None:
        del tenant_id
        self._binding = binding


class FixtureRanker:
    """Direct composition override proving an owning bootstrap has higher authority than defaults."""

    async def rerank(
        self, request: EventRequest, candidates: list[CanonicalEvent]
    ) -> list[tuple[CanonicalEvent, float]]:
        del request
        return [(candidate, 0.0) for candidate in candidates]


def _without_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep graph-selection tests free of a database connection or external service."""
    monkeypatch.setattr("events_concierge.composition.init_engine", lambda _: None)


def test_default_composition_keeps_deterministic_ranking_and_mock_calendar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No key/flag preserves the fully offline graph regardless of cloud mock defaults."""
    _without_engine(monkeypatch)

    container = build_container(
        Settings(
            discovery_sources="",
            cohere_api_key=None,
            google_calendar_enabled=False,
        )
    )

    assert isinstance(container.calendar, MockCalendar)
    assert isinstance(container.ranker, PersonalizedRanker)
    assert isinstance(container.ranker._cross_encoder, DeterministicCrossEncoder)


async def test_explicit_provider_settings_wire_real_adapters_with_fake_transports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Configured adapters are reachable without a network or a global Google credential."""
    _without_engine(monkeypatch)
    seen: list[str] = []

    def cohere_handler(request: httpx.Request) -> httpx.Response:
        seen.append("cohere")
        assert request.method == "POST"
        assert str(request.url) == "https://api.cohere.com/v2/rerank"
        assert request.headers["authorization"] == "Bearer fixture-cohere-key"
        return httpx.Response(200, json={"results": [{"index": 0, "relevance_score": 0.8}]})

    def google_handler(request: httpx.Request) -> httpx.Response:
        seen.append("google")
        assert request.method == "POST"
        assert str(request.url) == "https://www.googleapis.com/calendar/v3/freeBusy"
        assert request.headers["authorization"] == "Bearer fixture-google-token"
        return httpx.Response(
            200,
            json={
                "calendars": {"events-concierge@group.calendar.google.com": {"busy": []}}
            },
        )

    settings = Settings(
        discovery_sources="",
        cohere_api_key="fixture-cohere-key",
        google_calendar_enabled=True,
    )
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(cohere_handler)) as cohere_client,
        httpx.AsyncClient(transport=httpx.MockTransport(google_handler)) as google_client,
    ):
        container = build_container(
            settings,
            ranking_profiles=InMemoryRankingProfiles(),
            ranking_feedback_repo=InMemoryRankingFeedback(),
            cohere_client=cohere_client,
            google_calendar_access=FixtureGoogleAccess(),
            google_calendar_bindings=FixtureGoogleBindings(),
            google_calendar_client=google_client,
        )
        event = CanonicalEvent(
            canonical_event_id=uuid4(),
            title="Fixture jazz event",
            description="offline configured provider fixture",
            start_at=datetime(2026, 8, 1, 18, 0, tzinfo=UTC),
        )
        request = EventRequest(
            request_id=uuid4(),
            tenant_id=uuid4(),
            raw_text="jazz",
            constraints=RequestConstraints(),
        )

        assert isinstance(container.ranker, PersonalizedRanker)
        assert isinstance(container.ranker._cross_encoder, CohereRerankCrossEncoder)
        assert isinstance(container.calendar, GoogleCalendarAdapter)
        assert len(await container.ranker.rerank(request, [event])) == 1
        assert await container.calendar.free_busy(
            request.tenant_id,
            event.start_at,
            event.start_at + timedelta(hours=1),
        ) == []

    assert seen == ["cohere", "google"]


def test_enabled_google_calendar_fails_closed_without_a_tenant_access_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flag cannot silently create a global token path or fall back to a mock calendar."""
    _without_engine(monkeypatch)

    with pytest.raises(ValueError, match="GoogleCalendarAccessPort"):
        build_container(
            Settings(
                discovery_sources="",
                cohere_api_key=None,
                google_calendar_enabled=True,
            )
        )


def test_google_calendar_access_factory_makes_settings_selection_reachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The normal settings path can load owner-provisioned per-tenant access without a bearer env var."""
    _without_engine(monkeypatch)
    module_name = "events_concierge_test_google_access_factory"
    module = ModuleType(module_name)

    def build_access() -> FixtureGoogleAccess:
        return FixtureGoogleAccess()

    module.__dict__["build_access"] = build_access
    monkeypatch.setitem(sys.modules, module_name, module)

    container = build_container(
        Settings(
            discovery_sources="",
            cohere_api_key=None,
            google_calendar_enabled=True,
            google_calendar_access_factory=f"{module_name}:build_access",
        )
    )

    assert isinstance(container.calendar, GoogleCalendarAdapter)


def test_explicit_root_overrides_take_precedence_over_provider_selectors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment-owned DI override remains more specific than a process-wide selector."""
    _without_engine(monkeypatch)
    ranker = FixtureRanker()
    calendar = MockCalendar()

    container = build_container(
        Settings(
            discovery_sources="",
            cohere_api_key="  ",
            google_calendar_enabled=True,
        ),
        ranker=ranker,
        calendar=calendar,
    )

    assert container.ranker is ranker
    assert container.calendar is calendar


def test_provider_settings_parse_from_environment_and_reject_blank_cohere_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EC_ settings drive selection, while an accidental whitespace model secret fails closed."""
    monkeypatch.setenv("EC_COHERE_API_KEY", "environment-cohere-key")
    monkeypatch.setenv("EC_GOOGLE_CALENDAR_ENABLED", "true")
    settings = Settings()

    assert settings.cohere_api_key is not None
    assert settings.cohere_api_key.get_secret_value() == "environment-cohere-key"
    assert settings.google_calendar_enabled is True

    _without_engine(monkeypatch)
    with pytest.raises(ValueError, match="Cohere API key"):
        build_container(
            Settings(
                discovery_sources="",
                cohere_api_key="\t ",
                google_calendar_enabled=False,
            )
        )
