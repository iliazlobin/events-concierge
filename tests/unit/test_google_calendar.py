"""Offline Google Calendar adapter contracts (FR-4.5, FR-9.1/9.2/9.5/9.6)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from events_concierge.adapters.google_calendar.calendar import (
    GoogleCalendarAdapter,
    GoogleCalendarAmbiguousMatchError,
    GoogleCalendarBindingNotFoundError,
    GoogleCalendarError,
    GoogleCalendarReconsentRequiredError,
    GoogleCalendarRetryableError,
)
from events_concierge.ports.calendar import CalendarEntry
from events_concierge.ports.google_calendar import (
    GOOGLE_CALENDAR_CANONICAL_EVENT_ID_KEY,
    GOOGLE_CALENDAR_SCOPES,
    GoogleCalendarAccess,
    GoogleCalendarBinding,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "calendar"
_WRITE_CALENDAR_ID = "events-concierge@group.calendar.google.com"
_FREE_BUSY_IDS = ("primary-fixture", _WRITE_CALENDAR_ID)


class FixtureAccess:
    async def get_access(self, tenant_id: UUID) -> GoogleCalendarAccess:
        del tenant_id
        return GoogleCalendarAccess("fixture-access-token")


class FixtureBindings:
    def __init__(self, binding: GoogleCalendarBinding | None) -> None:
        self._binding = binding

    async def get_binding(self, tenant_id: UUID) -> GoogleCalendarBinding | None:
        del tenant_id
        return self._binding

    async def upsert_binding(self, tenant_id: UUID, binding: GoogleCalendarBinding) -> None:
        del tenant_id
        self._binding = binding


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text())


def _binding() -> GoogleCalendarBinding:
    return GoogleCalendarBinding(
        write_calendar_id=_WRITE_CALENDAR_ID,
        free_busy_calendar_ids=_FREE_BUSY_IDS,
    )


def test_google_binding_rejects_primary_write_calendar() -> None:
    """A concierge write target is always an app-created secondary calendar (FR-9.6)."""
    with pytest.raises(ValueError, match="secondary calendar"):
        GoogleCalendarBinding("primary", ("primary",))

    binding = _binding()
    assert binding.write_calendar_id == _WRITE_CALENDAR_ID
    assert binding.write_calendar_id != "primary"


def _entry() -> CalendarEntry:
    return CalendarEntry(
        calendar_event_id="0123456789abcdefghijklmnopqrstuv",
        canonical_event_id=uuid4(),
        title="Fixture event",
        start_at=datetime(2026, 7, 15, 18, 0, tzinfo=UTC),
        end_at=datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
        time_zone="America/Los_Angeles",
        location="Fixture venue",
        private_metadata={
            "events_concierge.registration_state": "registered",
            "events_concierge.source_event_ids": '["meetup:fixture-event"]',
        },
    )


async def test_google_free_busy_uses_explicit_bound_calendar_ids() -> None:
    tenant_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://www.googleapis.com/calendar/v3/freeBusy"
        assert request.headers["authorization"] == "Bearer fixture-access-token"
        assert json.loads(request.content) == {
            "timeMin": "2026-07-15T17:00:00+00:00",
            "timeMax": "2026-07-15T19:00:00+00:00",
            "timeZone": "UTC",
            "items": [{"id": calendar_id} for calendar_id in _FREE_BUSY_IDS],
        }
        return httpx.Response(200, json=_fixture("freebusy-success.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        blocks = await calendar.free_busy(
            tenant_id,
            datetime(2026, 7, 15, 17, 0, tzinfo=UTC),
            datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
        )

    assert len(blocks) == 1
    assert blocks[0].start == datetime(2026, 7, 15, 17, 30, tzinfo=UTC)
    assert blocks[0].end == datetime(2026, 7, 15, 18, 30, tzinfo=UTC)


async def test_google_upsert_inserts_then_patches_only_on_conflict() -> None:
    requests: list[httpx.Request] = []
    entry = _entry()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        if request.method == "POST":
            return httpx.Response(409, json=_fixture("insert-conflict.json"))
        if request.method == "PATCH":
            return httpx.Response(200, json={"id": entry.calendar_event_id})
        raise AssertionError(f"unexpected Google Calendar method: {request.method}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        await calendar.upsert_event(uuid4(), entry)

    assert [request.method for request in requests] == ["GET", "GET", "POST", "PATCH"]
    assert dict(requests[0].url.params) == {
        "privateExtendedProperty": (
            f"{GOOGLE_CALENDAR_CANONICAL_EVENT_ID_KEY}={entry.canonical_event_id}"
        ),
        "singleEvents": "true",
        "showDeleted": "false",
        "maxResults": "2",
    }
    assert dict(requests[1].url.params) == {
        "singleEvents": "true",
        "showDeleted": "false",
        "orderBy": "startTime",
        "timeMin": "2026-07-15T17:45:00+00:00",
        "timeMax": "2026-07-15T18:15:00+00:00",
        "maxResults": "250",
    }
    assert str(requests[2].url) == (
        "https://www.googleapis.com/calendar/v3/calendars/"
        "events-concierge%40group.calendar.google.com/events?sendUpdates=none"
    )
    assert str(requests[3].url) == (
        "https://www.googleapis.com/calendar/v3/calendars/"
        "events-concierge%40group.calendar.google.com/events/"
        "0123456789abcdefghijklmnopqrstuv?sendUpdates=none"
    )
    insert_payload = json.loads(requests[2].content)
    assert insert_payload["id"] == entry.calendar_event_id
    payload = json.loads(requests[3].content)
    assert "id" not in payload
    assert payload["start"] == {
        "dateTime": "2026-07-15T11:00:00-07:00",
        "timeZone": "America/Los_Angeles",
    }
    assert payload["end"] == {
        "dateTime": "2026-07-15T13:00:00-07:00",
        "timeZone": "America/Los_Angeles",
    }
    assert payload["extendedProperties"]["private"] == {
        GOOGLE_CALENDAR_CANONICAL_EVENT_ID_KEY: str(entry.canonical_event_id),
        "events_concierge.registration_state": "registered",
        "events_concierge.source_event_ids": '["meetup:fixture-event"]',
    }


async def test_google_upsert_stops_after_a_successful_insert() -> None:
    """A first calendar write makes no redundant patch request (FR-9.2)."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        assert request.method == "POST"
        return httpx.Response(200, json={"id": _entry().calendar_event_id})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        await calendar.upsert_event(uuid4(), _entry())

    assert [request.method for request in requests] == ["GET", "GET", "POST"]


async def test_google_upsert_rejects_a_bare_offset_time_zone_before_remote_io() -> None:
    """Calendar writes require a named IANA zone, never an offset-only source value (FR-9.5)."""
    entry = replace(_entry(), time_zone="-07:00")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"invalid calendar entry reached Google: {request.method} {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        with pytest.raises(ValueError, match="IANA zone"):
            await calendar.upsert_event(uuid4(), entry)


async def test_google_upsert_patches_the_unique_canonical_property_match() -> None:
    """The FR-9.3 primary key wins before fuzzy matching or deterministic insertion."""
    entry = _entry()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            assert request.url.params["privateExtendedProperty"] == (
                f"{GOOGLE_CALENDAR_CANONICAL_EVENT_ID_KEY}={entry.canonical_event_id}"
            )
            return httpx.Response(200, json=_fixture("canonical-match.json"))
        assert request.method == "PATCH"
        assert str(request.url) == (
            "https://www.googleapis.com/calendar/v3/calendars/"
            "events-concierge%40group.calendar.google.com/events/"
            "provider-canonical-event?sendUpdates=none"
        )
        return httpx.Response(200, json={"id": "provider-canonical-event"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        await calendar.upsert_event(uuid4(), entry)

    assert [request.method for request in requests] == ["GET", "PATCH"]
    payload = json.loads(requests[1].content)
    assert "id" not in payload
    assert payload["extendedProperties"]["private"][GOOGLE_CALENDAR_CANONICAL_EVENT_ID_KEY] == str(
        entry.canonical_event_id
    )


async def test_google_upsert_patches_a_unique_human_created_fuzzy_match() -> None:
    """Normalized title, +/-15m start, and venue token overlap absorb one manual duplicate (AC-66)."""
    entry = _entry()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and "privateExtendedProperty" in request.url.params:
            return httpx.Response(200, json={"items": []})
        if request.method == "GET":
            assert request.url.params["timeMin"] == "2026-07-15T17:45:00+00:00"
            assert request.url.params["timeMax"] == "2026-07-15T18:15:00+00:00"
            return httpx.Response(200, json=_fixture("fuzzy-unique-match.json"))
        assert request.method == "PATCH"
        assert str(request.url) == (
            "https://www.googleapis.com/calendar/v3/calendars/"
            "events-concierge%40group.calendar.google.com/events/"
            "human-created-event?sendUpdates=none"
        )
        return httpx.Response(200, json={"id": "human-created-event"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        await calendar.upsert_event(uuid4(), entry)

    assert [request.method for request in requests] == ["GET", "GET", "PATCH"]
    assert "id" not in json.loads(requests[-1].content)


async def test_google_upsert_fails_closed_when_fuzzy_match_is_ambiguous() -> None:
    """Two manual near-duplicates must never select one arbitrary event to patch (FR-9.3)."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if "privateExtendedProperty" in request.url.params:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json=_fixture("fuzzy-ambiguous.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        with pytest.raises(GoogleCalendarAmbiguousMatchError):
            await calendar.upsert_event(uuid4(), _entry())

    assert [request.method for request in requests] == ["GET", "GET"]


async def test_google_upsert_fails_closed_when_canonical_query_is_ambiguous() -> None:
    """A corrupt primary key has no safer fallback than refusing to mutate (FR-9.3)."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_fixture("canonical-ambiguous.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        with pytest.raises(GoogleCalendarAmbiguousMatchError):
            await calendar.upsert_event(uuid4(), _entry())

    assert [request.method for request in requests] == ["GET"]


async def test_google_delete_treats_not_found_as_idempotent_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.params["sendUpdates"] == "none"
        return httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        await calendar.delete_event(uuid4(), "0123456789abcdefghijklmnopqrstuv")


async def test_google_delete_resolves_the_canonical_match_before_deterministic_fallback() -> None:
    """A later cancellation deletes the human event previously absorbed by FR-9.3 fuzzy matching."""
    entry = _entry()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            assert request.url.params["privateExtendedProperty"] == (
                f"{GOOGLE_CALENDAR_CANONICAL_EVENT_ID_KEY}={entry.canonical_event_id}"
            )
            return httpx.Response(200, json=_fixture("canonical-match.json"))
        assert request.method == "DELETE"
        assert str(request.url) == (
            "https://www.googleapis.com/calendar/v3/calendars/"
            "events-concierge%40group.calendar.google.com/events/"
            "provider-canonical-event?sendUpdates=none"
        )
        return httpx.Response(204)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        await calendar.delete_event(
            uuid4(),
            entry.calendar_event_id,
            canonical_event_id=entry.canonical_event_id,
        )

    assert [request.method for request in requests] == ["GET", "DELETE"]


async def test_google_delete_uses_deterministic_id_when_the_canonical_lookup_has_no_match() -> None:
    """A missed or already-deleted canonical match preserves idempotent deterministic deletion."""
    entry = _entry()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(200, json={"items": []})
        assert request.method == "DELETE"
        assert request.url.path.endswith(f"/events/{entry.calendar_event_id}")
        return httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        await calendar.delete_event(
            uuid4(),
            entry.calendar_event_id,
            canonical_event_id=entry.canonical_event_id,
        )

    assert [request.method for request in requests] == ["GET", "DELETE"]


async def test_google_auth_failure_requires_explicit_reconsent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(400, json=_fixture("invalid-grant.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        with pytest.raises(GoogleCalendarReconsentRequiredError):
            await calendar.upsert_event(uuid4(), _entry())


async def test_google_forbidden_auth_error_requires_explicit_reconsent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(403, json=_fixture("forbidden-auth.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        with pytest.raises(GoogleCalendarReconsentRequiredError):
            await calendar.upsert_event(uuid4(), _entry())


async def test_google_forbidden_rate_limit_is_not_misclassified_as_reconsent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(403, json=_fixture("forbidden-rate-limit.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        with pytest.raises(GoogleCalendarRetryableError) as raised:
            await calendar.upsert_event(uuid4(), _entry())

    assert type(raised.value) is GoogleCalendarRetryableError


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("dailyLimitExceeded", "RESOURCE_EXHAUSTED"),
        ("rateLimitExceeded", "RESOURCE_EXHAUSTED"),
        ("quotaExceeded", "RESOURCE_EXHAUSTED"),
    ],
)
async def test_google_forbidden_quota_reason_is_retryable(reason: str, status: str) -> None:
    """All documented 403 quota reasons retry without asking the user to consent again (FR-9.7)."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            403,
            json={
                "error": {
                    "status": status,
                    "errors": [{"domain": "usageLimits", "reason": reason}],
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        with pytest.raises(GoogleCalendarRetryableError):
            await calendar.upsert_event(uuid4(), _entry())


async def test_google_forbidden_resource_exhausted_status_is_retryable() -> None:
    """The documented Google status remains enough when a response omits a reason (FR-9.7)."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(403, json={"error": {"status": "RESOURCE_EXHAUSTED"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        with pytest.raises(GoogleCalendarRetryableError):
            await calendar.upsert_event(uuid4(), _entry())


async def test_google_forbidden_without_auth_or_quota_signal_remains_generic_failure() -> None:
    """A non-specific forbidden response must not prompt re-consent or imply a retry guarantee (FR-9.7)."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(403, json={"error": {"status": "PERMISSION_DENIED"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        with pytest.raises(GoogleCalendarError) as raised:
            await calendar.upsert_event(uuid4(), _entry())

    assert type(raised.value) is GoogleCalendarError


async def test_google_too_many_requests_is_retryable_without_reconsent() -> None:
    """HTTP 429 shares the transient retry path even when Google omits structured error JSON (FR-9.7)."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(429, text="temporarily rate limited")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        calendar = GoogleCalendarAdapter(
            FixtureAccess(), FixtureBindings(_binding()), client=client
        )
        with pytest.raises(GoogleCalendarRetryableError):
            await calendar.upsert_event(uuid4(), _entry())


async def test_google_missing_binding_fails_before_a_remote_call() -> None:
    calendar = GoogleCalendarAdapter(FixtureAccess(), FixtureBindings(None))

    with pytest.raises(GoogleCalendarBindingNotFoundError):
        await calendar.free_busy(
            uuid4(),
            datetime(2026, 7, 15, 17, 0, tzinfo=UTC),
            datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
        )


def test_google_scope_contract_is_calendar_family_only() -> None:
    assert GOOGLE_CALENDAR_SCOPES == (
        "https://www.googleapis.com/auth/calendar.events",
        "https://www.googleapis.com/auth/calendar.freebusy",
    )
    forbidden = ("gmail", "drive", "chat", "photos")
    assert all(not any(term in scope for term in forbidden) for scope in GOOGLE_CALENDAR_SCOPES)
