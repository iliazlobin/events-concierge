"""Offline contract tests for the G2-gated Meetup API SourcePort scaffold."""

from __future__ import annotations

import json
import math
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from events_concierge.adapters.meetup.source import (
    HttpxMeetupApi,
    MeetupReconsentRequiredError,
    MeetupSource,
)
from events_concierge.domain.enums import GroupCondition, Modality, RsvpState
from events_concierge.domain.policy import SourceQuarantineSignal
from events_concierge.ports.sources import (
    RegisterOutcome,
    RegistrationTarget,
    SourceAccessDeniedError,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "sources" / "meetup"


class StaticTokenProvider:
    async def get_access_token(self, tenant_id: object) -> str:
        del tenant_id
        return "fixture-token"


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text())


async def test_injected_meetup_client_gets_an_explicit_finite_request_timeout() -> None:
    seen_timeout: object = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_timeout
        seen_timeout = request.extensions.get("timeout")
        return httpx.Response(200, json=_fixture("membership-member.json"))

    target = RegistrationTarget("fixture-meetup-1", "https://www.meetup.com/example/events/1")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=None
    ) as client:
        api = HttpxMeetupApi(StaticTokenProvider(), client=client, timeout_s=2.5)
        assert await api.read_membership_state(uuid4(), target) is GroupCondition.MEMBER

    assert seen_timeout == {"connect": 2.5, "read": 2.5, "write": 2.5, "pool": 2.5}


@pytest.mark.parametrize("timeout_s", (0.0, 0.09, 60.01, math.inf, math.nan))
def test_meetup_timeout_must_be_bounded_and_finite(timeout_s: float) -> None:
    with pytest.raises(ValueError, match="finite and between"):
        HttpxMeetupApi(StaticTokenProvider(), timeout_s=timeout_s)


async def test_meetup_source_uses_read_before_one_create_rsvp_without_member_id() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://api.meetup.com/gql-ext"
        assert request.headers["authorization"] == "Bearer fixture-token"
        body = json.loads(request.content)
        assert isinstance(body, dict)
        requests.append(body)
        query = str(body["query"])
        if "ReadEventMembership" in query:
            return httpx.Response(200, json=_fixture("membership-member.json"))
        if "ReadEventRsvp" in query:
            return httpx.Response(200, json=_fixture("rsvp-not-present.json"))
        if "CreateEventRsvp" in query:
            return httpx.Response(200, json=_fixture("create-rsvp-confirmed.json"))
        raise AssertionError(f"unexpected GraphQL operation: {query}")

    target = RegistrationTarget(
        source_event_id="fixture-meetup-1",
        registration_url="https://www.meetup.com/example/events/fixture-meetup-1",
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = MeetupSource(HttpxMeetupApi(StaticTokenProvider(), client=client))
        assert await source.read_membership_state(uuid4(), target) is GroupCondition.MEMBER
        assert (
            await source.read_registration_state(uuid4(), target, Modality.API)
            is RsvpState.NOT_PRESENT
        )
        result = await source.register(uuid4(), target, Modality.API, "workflow-owned-key")

    assert result.outcome is RegisterOutcome.CONFIRMED
    assert len(requests) == 3
    mutation = requests[-1]
    assert "createEventRsvp" in str(mutation["query"])
    assert "member_id" not in str(mutation["query"])
    assert mutation["variables"] == {"eventId": "fixture-meetup-1"}
    assert "response: YES" in str(mutation["query"])


async def test_meetup_confirmed_read_never_requires_a_second_mutation() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert "ReadEventRsvp" in str(json.loads(request.content)["query"])
        return httpx.Response(200, json=_fixture("rsvp-confirmed.json"))

    target = RegistrationTarget("fixture-meetup-1", "https://www.meetup.com/example/events/fixture")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = MeetupSource(HttpxMeetupApi(StaticTokenProvider(), client=client))
        state = await source.read_registration_state(uuid4(), target, Modality.API)

    assert state is RsvpState.CONFIRMED
    assert calls == 1


async def test_meetup_rejected_oauth_token_is_typed_reconsent_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(401, json={"error": "invalid token"})

    target = RegistrationTarget("fixture-meetup-1", "https://www.meetup.com/example/events/fixture")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = MeetupSource(HttpxMeetupApi(StaticTokenProvider(), client=client))
        with pytest.raises(MeetupReconsentRequiredError):
            await source.read_membership_state(uuid4(), target)


async def test_meetup_forbidden_is_a_closed_source_quarantine_signal() -> None:
    """403 is never retried as a tenant re-consent failure (FR-10.3, AC-72)."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(403, json={"error": "forbidden"})

    target = RegistrationTarget("fixture-meetup-1", "https://www.meetup.com/example/events/fixture")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        source = MeetupSource(HttpxMeetupApi(StaticTokenProvider(), client=client))
        with pytest.raises(SourceAccessDeniedError) as raised:
            await source.read_membership_state(uuid4(), target)

    assert raised.value.signal is SourceQuarantineSignal.FORBIDDEN
