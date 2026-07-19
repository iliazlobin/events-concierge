"""Opaque external-artifact inventory boundary for the bounded FR-10.5 pre-erasure slice.

This port deliberately exposes only canonical identifiers needed to remove concierge-created
calendar entries.  It is not a data-subject erasure coordinator: database crypto-shredding,
RelayInbox purging, retention exceptions, and workflow fencing remain separate owner-gated work
(FR-10.5, ADR-006/007/011).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class CalendarDeletionTarget:
    """One opaque, deterministic concierge-calendar target.

    The application rejects a target whose tenant does not match the request, then derives the only
    accepted remote event id from ``tenant_id`` plus this canonical ID using
    ``domain.ids.calendar_event_id``.  Titles, venues, source URLs, user email, and raw request
    text are intentionally absent (FR-10.5, ADR-007).
    """

    tenant_id: UUID
    canonical_event_id: UUID


class TenantErasureInventoryPort(Protocol):
    """Enumerate only the tenant's concierge-owned external deletion capabilities (FR-10.5)."""

    async def list_calendar_deletion_targets(
        self, tenant_id: UUID
    ) -> tuple[CalendarDeletionTarget, ...]:
        """Return unique, deterministic, tenant-scoped targets without event content.

        A production implementation will enumerate retained non-PII lifecycle associations under
        RLS.  Every target's ``tenant_id`` must equal this argument; consumers fail closed before
        effects if it does not.  The port must not expose calendar titles, deep links, source
        payloads, or user PII to the pre-erasure coordinator (FR-1.3, FR-10.5, ADR-007).
        """
        ...
