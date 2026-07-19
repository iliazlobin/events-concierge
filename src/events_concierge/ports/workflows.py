"""Outbound durable-workflow start boundary used by the ADR-003 request start-outbox."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID


class RequestWorkflowStarter(Protocol):
    """Start one parent EventRequest workflow, treating engine duplicate rejection as success.

    The application relay owns durability and retry.  This narrow port owns only the external
    engine call so tests can prove lost-ack recovery without a live Temporal namespace (AC-48).
    """

    async def start(self, tenant_id: UUID, request_id: UUID) -> None:
        """Ensure the deterministic parent workflow exists or raise a retryable engine error."""
        ...


class CatalogRefreshWorkflowStarter(Protocol):
    """Start P15a's one-GET catalog continuation without exposing a Temporal client.

    The caller supplies the already-owner-reviewed source key and stable ledger run key.  A
    duplicate open execution is a successful replay; a failed execution may be restarted with the
    same pair after the durable ledger recorded its failure (FR-10.3/10.4, NFR-8, ADR-003/005).
    """

    async def start(self, source_key: str, run_key: str) -> None:
        """Ensure the deterministic catalog refresh workflow exists or raise an engine error."""
        ...


class CatalogPagedRefreshWorkflowStarter(Protocol):
    """Start P15b/P15c/P15d/P15e's durable per-page catalog continuation without Temporal (ADR-003/005)."""

    async def start(self, source_key: str, run_key: str) -> None:
        """Ensure one paged source/run continuation is open or raise an engine error."""
        ...


class RegistrationLifecycleSignaler(Protocol):
    """Signal an already-active per-event registration workflow (FR-6.7, FR-8.8).

    The API first resolves the RLS-scoped lifecycle row, which yields the actual attempt-suffixed
    workflow id.  This port owns only the engine signal; it must raise rather than falsely accept a
    command when Temporal is unavailable, because no command outbox exists at this boundary yet.
    """

    async def signal_unrsvp(self, workflow_id: str, request_id: str) -> None:
        """Durably enqueue one idempotent un-RSVP command on the matching child workflow."""
        ...


class WorkflowLivenessInspector(Protocol):
    """Read only whether a durable execution still owns a lifecycle (ADR-007)."""

    async def is_open(self, workflow_id: str) -> bool:
        """Return false only for an authoritative closed/not-found workflow; propagate uncertainty."""
        ...
