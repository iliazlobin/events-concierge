"""Narrow Meetup API seams used by the fixture-backed SourcePort adapter.

The G2 spike must validate the live GraphQL schema, quota scope, and idempotency behavior before
this adapter is enabled.  These types therefore describe only the source facts the registration
saga needs: membership, current RSVP state, and the one ``createEventRsvp`` mutation (FR-5.2/5.3,
ADR-003/005).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..domain.enums import GroupCondition, RsvpState
from .sources import RegistrationTarget


class MeetupAccessTokenPort(Protocol):
    """Return a short-lived user OAuth token without exposing vault details to the adapter."""

    async def get_access_token(self, tenant_id: UUID) -> str:
        """Resolve the requesting tenant's Meetup bearer token (FR-2.2/3.5)."""
        ...


@dataclass(frozen=True, slots=True)
class MeetupMutationResult:
    """Normalized create-RSVP outcome; the caller maps it to the generic SourcePort result."""

    state: RsvpState
    detail: str = ""


class MeetupApiPort(Protocol):
    """Authenticated, read-only/mutate primitives for sanctioned Meetup API use (FR-5.2/5.3)."""

    async def read_membership_state(
        self, tenant_id: UUID, target: RegistrationTarget
    ) -> GroupCondition:
        """Read the viewer's group condition for the target event before lane selection."""
        ...

    async def read_rsvp_state(self, tenant_id: UUID, target: RegistrationTarget) -> RsvpState:
        """Read the viewer RSVP state before a retry can reach ``createEventRsvp``."""
        ...

    async def create_event_rsvp(
        self, tenant_id: UUID, target: RegistrationTarget
    ) -> MeetupMutationResult:
        """Issue exactly one sanctioned RSVP mutation; no adapter-level retry loop is allowed."""
        ...
