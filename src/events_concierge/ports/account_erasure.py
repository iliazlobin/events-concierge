"""Ports for the resumable FR-10.5 account-erasure coordinator."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from ..domain.account_erasure import (
    AccountErasureFailureStage,
    AccountErasureLease,
    AccountErasureSnapshot,
    AccountErasureStage,
)


class AccountErasureConflictError(RuntimeError):
    """A tenant's one durable erasure command is bound to another request id."""


class AccountErasureAccountNotFoundError(LookupError):
    """Neither a live account nor an erasure tombstone exists for the authenticated tenant."""


class AccountErasureInvariantError(RuntimeError):
    """Durable erasure state is internally inconsistent and must fail closed."""


class AccountErasureRepository(Protocol):
    """Persist the write fence, stage acknowledgements, purge, and final tombstone."""

    async def begin(self, tenant_id: UUID, request_id: UUID) -> AccountErasureSnapshot:
        """Install or replay one tenant write fence and return its immutable target inventory."""
        ...

    async def get(self, tenant_id: UUID) -> AccountErasureSnapshot | None:
        """Read a pending/completed tombstone under the caller's tenant context."""
        ...

    async def complete_stage(
        self,
        tenant_id: UUID,
        request_id: UUID,
        stage: AccountErasureStage,
        completed_count: int,
    ) -> AccountErasureSnapshot:
        """Acknowledge one fully converged external stage, accepting an exact replay only."""
        ...

    async def finalize(self, tenant_id: UUID, request_id: UUID) -> AccountErasureSnapshot:
        """Atomically purge database identity/behavioral data after every external stage."""
        ...

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[AccountErasureLease]:
        """Lease due cross-tenant tombstones through the bounded system capability."""
        ...

    async def release_lease(
        self,
        lease: AccountErasureLease,
        *,
        failure_stage: AccountErasureFailureStage,
        retry_after_seconds: int,
    ) -> bool:
        """Release only the exact live lease with a fixed, PII-free retry classification."""
        ...

    async def renew_lease(self, lease: AccountErasureLease, lease_seconds: int) -> bool:
        """Extend only the exact live worker lease while a bounded external stage is running."""
        ...


class TenantWorkflowCancellationPort(Protocol):
    """Idempotently close and erase tenant-owned Temporal histories before PII deletion."""

    async def cancel(self, workflow_id: str) -> None:
        """Return after the execution closes or is already absent/closed."""
        ...

    async def quiesce_tenant(self, tenant_id: UUID, known_workflow_ids: tuple[str, ...]) -> None:
        """Close, history-delete, verify, and quiet-scan every known/discovered execution."""
        ...


class TenantSessionRevocationPort(Protocol):
    """Atomically fence new BFF sessions and revoke every existing tenant session."""

    async def revoke_tenant_sessions(self, tenant_id: UUID) -> None:
        """Return only after issuance is fenced and all indexed sessions are absent."""
        ...


class TenantExternalEffectDrainPort(Protocol):
    """Prove every pre-fence tenant mutation has settled before external inventory sweeps."""

    async def drain_tenant_effects(self, tenant_id: UUID) -> None:
        """Return only after the complete guarded mutation inventory has no in-flight lease."""
        ...
