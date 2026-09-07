"""Tenant-isolated read port for the consumer product surface."""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol
from uuid import UUID

from ..domain.consumer import (
    ConsumerIdentity,
    ConsumerRegistrationSummary,
    ConsumerRequestSummary,
    ConsumerTaskSummary,
)


class ConsumerRequestOutcomeLinkStatus(StrEnum):
    """Converged result of recording an immutable selected lifecycle link."""

    LINKED = "linked"
    REPLAYED = "replayed"


class ConsumerRequestOutcomeConflictError(ValueError):
    """A request was replayed with a different selected lifecycle identity."""


class ConsumerReadPort(Protocol):
    """Bounded projections plus the narrow immutable link that makes request outcomes readable."""

    async def get_identity(self, tenant_id: UUID) -> ConsumerIdentity | None: ...

    async def list_requests(
        self, tenant_id: UUID, *, offset: int, limit: int
    ) -> tuple[ConsumerRequestSummary, ...]:
        """Return bounded recent asks and only explicitly linked selected lifecycle outcomes."""
        ...

    async def link_request_outcome(
        self,
        tenant_id: UUID,
        request_id: UUID,
        lifecycle_id: UUID,
    ) -> ConsumerRequestOutcomeLinkStatus:
        """Record or exactly replay one request's immutable selected lifecycle."""
        ...

    async def list_registrations(
        self, tenant_id: UUID, *, offset: int, limit: int
    ) -> tuple[ConsumerRegistrationSummary, ...]: ...

    async def list_tasks(
        self,
        tenant_id: UUID,
        *,
        actionable_only: bool,
        offset: int,
        limit: int,
    ) -> tuple[ConsumerTaskSummary, ...]: ...
