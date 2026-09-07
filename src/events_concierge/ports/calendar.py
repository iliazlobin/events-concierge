"""CalendarPort: the launch Google adapter is the floor; Graph/CalDAV are feature-flagged port
contracts (FR-9.5). Writes are idempotent upserts keyed by the deterministic calendar id (FR-9.2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ..domain.conflict import BusyBlock


class CalendarBindingUnavailableError(RuntimeError):
    """The tenant has no usable owner-provisioned calendar binding.

    This is a user/operator action outcome, not a transient transport failure.  Application
    services use the marker without depending on a concrete Google adapter.
    """


class CalendarReconsentRequiredError(RuntimeError):
    """The calendar authorization is permanently unusable until the user grants consent again."""


@dataclass(frozen=True, slots=True)
class CalendarEntry:
    calendar_event_id: str
    canonical_event_id: UUID
    title: str
    start_at: datetime
    end_at: datetime | None
    time_zone: str  # always an IANA zone, never a bare offset (FR-9.5)
    location: str | None = None
    # Reserved for source/registration metadata sent as Google extendedProperties.private (FR-9.3).
    private_metadata: Mapping[str, str] = field(default_factory=dict)


class CalendarPort(Protocol):
    async def free_busy(
        self, tenant_id: UUID, window_start: datetime, window_end: datetime
    ) -> list[BusyBlock]:
        """Read busy blocks across the user's connected calendars for the conflict gate (FR-4.5)."""
        ...

    async def upsert_event(self, tenant_id: UUID, entry: CalendarEntry) -> None:
        """Insert, and on a 409 duplicate switch to patch -- an idempotent upsert (FR-9.2)."""
        ...

    async def delete_event(
        self,
        tenant_id: UUID,
        calendar_event_id: str,
        *,
        canonical_event_id: UUID | None = None,
    ) -> None:
        """Remove a concierge entry, resolving a prior FR-9.3 fuzzy merge when its canonical ID is known."""
        ...

    async def delete_tenant_events(self, tenant_id: UUID) -> None:
        """Exhaustively delete every provider event carrying this app's ownership metadata."""
        ...
