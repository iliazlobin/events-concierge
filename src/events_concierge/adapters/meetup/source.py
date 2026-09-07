"""Fixture-testable sanctioned Meetup API SourcePort adapter.

The shape deliberately keeps the operation names and state mapping explicit, but it remains
disabled from default composition until the owner executes G2 with a Meetup Pro OAuth consumer.
G2 must verify the live GraphQL schema, authorization behavior, quota accounting, and retry
semantics before this adapter can be activated (FR-5.2/5.3, ADR-005).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Final
from uuid import UUID

import httpx

from ...domain.enums import GroupCondition, Modality, RsvpState, Source
from ...domain.events import CandidateEvent
from ...domain.policy import SourceQuarantineSignal
from ...domain.request import RequestConstraints
from ...ports.meetup import (
    MeetupAccessTokenPort,
    MeetupApiPort,
    MeetupMutationResult,
)
from ...ports.sources import (
    RegisterOutcome,
    RegisterResult,
    RegistrationTarget,
    SourceAccessDeniedError,
    SourceCapability,
    SourceReconsentRequiredError,
)

# Meetup's February 2025 GraphQL migration moved third-party clients to ``gql-ext``. Keep the
# endpoint fixed in trusted deployment code; neither a target event nor a token may select it.
_DEFAULT_ENDPOINT: Final = "https://api.meetup.com/gql-ext"
_HTTP_UNAUTHORIZED: Final = 401
_HTTP_FORBIDDEN: Final = 403
_MIN_TIMEOUT_SECONDS: Final = 0.1
_MAX_TIMEOUT_SECONDS: Final = 60.0

_MEMBERSHIP_QUERY: Final = """
query ReadEventMembership($eventId: ID!) {
  event(id: $eventId) {
    group { viewerMembership { state } }
  }
}
"""
_RSVP_QUERY: Final = """
query ReadEventRsvp($eventId: ID!) {
  event(id: $eventId) {
    viewerRsvp { response }
  }
}
"""
_CREATE_RSVP_MUTATION: Final = """
mutation CreateEventRsvp($eventId: ID!) {
  createEventRsvp(input: { eventId: $eventId, response: YES }) {
    rsvp { response }
  }
}
"""


class MeetupApiError(RuntimeError):
    """A fixture/transport/API failure that must stop autonomous Meetup mutation."""


class MeetupReconsentRequiredError(MeetupApiError, SourceReconsentRequiredError):
    """The OAuth token was rejected; callers must route to re-consent/handoff (FR-2.3/2.12)."""


class HttpxMeetupApi:
    """Provisional Meetup GraphQL transport with injectable HTTP client (G2-gated).

    The requests deliberately omit ``member_id`` and never send a guessed vendor idempotency field.
    Workflow-owned idempotency remains a local audit/recovery invariant until G2 measures the vendor's
    semantics.  The methods perform one HTTP request each; retry/pacing belongs above this adapter.
    """

    def __init__(
        self,
        tokens: MeetupAccessTokenPort,
        *,
        client: httpx.AsyncClient | None = None,
        endpoint: str = _DEFAULT_ENDPOINT,
        timeout_s: float = 10.0,
    ) -> None:
        if not endpoint.startswith("https://"):
            raise ValueError("Meetup GraphQL endpoint must use HTTPS")
        if (
            isinstance(timeout_s, bool)
            or not math.isfinite(timeout_s)
            or not _MIN_TIMEOUT_SECONDS <= timeout_s <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError("Meetup timeout must be finite and between 0.1 and 60 seconds")
        self._tokens = tokens
        self._client = client
        self._endpoint = endpoint
        self._timeout_s = timeout_s

    async def read_membership_state(
        self, tenant_id: UUID, target: RegistrationTarget
    ) -> GroupCondition:
        payload = await self._execute(tenant_id, _MEMBERSHIP_QUERY, target.source_event_id)
        return _membership_from_payload(payload)

    async def read_rsvp_state(self, tenant_id: UUID, target: RegistrationTarget) -> RsvpState:
        payload = await self._execute(tenant_id, _RSVP_QUERY, target.source_event_id)
        return _rsvp_from_payload(payload, path=("event", "viewerRsvp", "response"))

    async def create_event_rsvp(
        self, tenant_id: UUID, target: RegistrationTarget
    ) -> MeetupMutationResult:
        payload = await self._execute(tenant_id, _CREATE_RSVP_MUTATION, target.source_event_id)
        state = _rsvp_from_payload(payload, path=("createEventRsvp", "rsvp", "response"))
        return MeetupMutationResult(state=state)

    async def _execute(self, tenant_id: UUID, operation: str, event_id: str) -> object:
        token = await self._tokens.get_access_token(tenant_id)
        if not token.strip():
            raise MeetupReconsentRequiredError("empty Meetup access token")
        if self._client is not None:
            return await self._post(self._client, token, operation, event_id)
        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            return await self._post(client, token, operation, event_id)

    async def _post(
        self, client: httpx.AsyncClient, token: str, operation: str, event_id: str
    ) -> object:
        response = await client.post(
            self._endpoint,
            headers={"Authorization": f"Bearer {token}"},
            json={"query": operation, "variables": {"eventId": event_id}},
            timeout=self._timeout_s,
        )
        if response.status_code == _HTTP_UNAUTHORIZED:
            raise MeetupReconsentRequiredError("Meetup rejected the OAuth token")
        if response.status_code == _HTTP_FORBIDDEN:
            # A 401 is a tenant credential problem; a confirmed 403 is the explicit FR-10.3
            # source-ban/forbidden signal. G2 may refine this only with live evidence, never by
            # silently retrying the blocked surface.
            raise SourceAccessDeniedError(SourceQuarantineSignal.FORBIDDEN)
        response.raise_for_status()
        payload: object = response.json()
        if not isinstance(payload, dict):
            raise MeetupApiError("Meetup GraphQL response must be an object")
        errors = payload.get("errors")
        if isinstance(errors, list) and errors:
            raise MeetupApiError("Meetup GraphQL response contains errors")
        if not isinstance(payload.get("data"), dict):
            raise MeetupApiError("Meetup GraphQL response must contain data")
        return payload


class MeetupSource:
    """SourcePort using the sanctioned API only; not wired into default composition until G2."""

    capability = SourceCapability(
        source=Source.MEETUP,
        supports_api=True,
        supports_browser_discovery=False,
        supports_autonomous_register=True,
    )

    def __init__(
        self, api: MeetupApiPort, *, fixture_events: list[CandidateEvent] | None = None
    ) -> None:
        self._api = api
        self._fixture_events = list(fixture_events or [])

    async def discover(self, constraints: RequestConstraints) -> list[CandidateEvent]:
        """Replay fixture discovery only; tenant OAuth discovery remains disabled pending G2."""
        return [event for event in self._fixture_events if _matches(event, constraints)]

    async def read_membership_state(
        self, tenant_id: UUID, target: RegistrationTarget
    ) -> GroupCondition:
        return await self._api.read_membership_state(tenant_id, target)

    async def read_registration_state(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality
    ) -> RsvpState:
        if modality is not Modality.API:
            return RsvpState.AMBIGUOUS
        return await self._api.read_rsvp_state(tenant_id, target)

    async def register(
        self,
        tenant_id: UUID,
        target: RegistrationTarget,
        modality: Modality,
        idempotency_key: str,
    ) -> RegisterResult:
        """Perform exactly one API mutation after the application-level read-before-mutate guard."""
        del idempotency_key
        if modality is not Modality.API:
            return RegisterResult(RegisterOutcome.NEEDS_HANDOFF, "Meetup allows API RSVP only")
        result = await self._api.create_event_rsvp(tenant_id, target)
        return _register_result(result)


def _matches(candidate: CandidateEvent, constraints: RequestConstraints) -> bool:
    if constraints.time_window is not None and not constraints.time_window.contains(
        candidate.start_at
    ):
        return False
    return constraints.accepts_price(candidate.price_status)


def _membership_from_payload(payload: object) -> GroupCondition:
    value = _value_at(payload, ("data", "event", "group", "viewerMembership", "state"))
    if not isinstance(value, str):
        return GroupCondition.UNKNOWN
    return {
        "MEMBER": GroupCondition.MEMBER,
        "NON_MEMBER": GroupCondition.NON_MEMBER,
        "OPEN_INSTANT_JOIN": GroupCondition.OPEN_INSTANT_JOIN,
        "APPROVAL_GATED": GroupCondition.APPROVAL_GATED,
        "DUES_REQUIRED": GroupCondition.DUES_REQUIRED,
    }.get(value.upper(), GroupCondition.UNKNOWN)


def _rsvp_from_payload(payload: object, *, path: tuple[str, ...]) -> RsvpState:
    value = _value_at(payload, ("data", *path))
    if value is None:
        return RsvpState.NOT_PRESENT
    if not isinstance(value, str):
        return RsvpState.AMBIGUOUS
    normalized = value.upper()
    if normalized in {"YES", "GOING", "CONFIRMED"}:
        return RsvpState.CONFIRMED
    if normalized in {"PENDING", "WAITLIST", "REQUESTED"}:
        return RsvpState.PENDING_CONFIRMATION
    if normalized in {"NO", "NONE", "NOT_PRESENT"}:
        return RsvpState.NOT_PRESENT
    return RsvpState.AMBIGUOUS


def _value_at(payload: object, path: tuple[str, ...]) -> object | None:
    current = payload
    for key in path:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _register_result(result: MeetupMutationResult) -> RegisterResult:
    if result.state is RsvpState.CONFIRMED:
        return RegisterResult(RegisterOutcome.CONFIRMED, result.detail)
    if result.state is RsvpState.PENDING_CONFIRMATION:
        return RegisterResult(RegisterOutcome.PENDING_CONFIRMATION, result.detail)
    if result.state is RsvpState.NOT_PRESENT:
        return RegisterResult(
            RegisterOutcome.FAILED, result.detail or "Meetup RSVP was not recorded"
        )
    return RegisterResult(
        RegisterOutcome.NEEDS_HANDOFF, result.detail or "Meetup RSVP state ambiguous"
    )
