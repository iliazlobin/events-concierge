"""Request-level terminal effects that do not belong to a single CanonicalEvent lifecycle.

An empty ranked set has no child lifecycle to transition, but it still must leave the durable
request in a terminal state and publish exactly one user-visible no-result notification.  This
small application service keeps that request-scoped concern behind the repository port instead of
letting a Temporal activity write an outbox row directly (FR-5.0/6.6, ADR-003/007).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from ..ports.repositories import RequestRepository


class RequestTerminalStatus(StrEnum):
    """The idempotent result of a request-level no-candidate terminalization."""

    FINALIZED = "finalized"
    ALREADY_FINALIZED = "already_finalized"


@dataclass(frozen=True, slots=True)
class RequestTerminalResult:
    """A JSON-safe projection returned to the short-lived parent workflow."""

    status: RequestTerminalStatus


class RequestTerminalService:
    """Commit the request's no-result state and outbox row in one repository operation.

    ``transition_id`` is minted by the parent workflow and is stable across an activity ACK loss,
    so a retry cannot create another request terminal event (FR-8.3/8.9, NFR-8, ADR-003/007).
    """

    def __init__(self, requests: RequestRepository) -> None:
        self._requests = requests

    async def finalize_no_candidate(
        self, tenant_id: UUID, request_id: UUID, transition_id: str
    ) -> RequestTerminalResult:
        """Persist one request-scoped ``failed_no_candidate`` outcome (FR-5.0, FR-6.6)."""
        finalized = await self._requests.mark_failed_no_candidate(
            tenant_id, request_id, transition_id
        )
        return RequestTerminalResult(
            RequestTerminalStatus.FINALIZED
            if finalized
            else RequestTerminalStatus.ALREADY_FINALIZED
        )
