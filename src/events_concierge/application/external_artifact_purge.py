"""Bounded external-artifact pre-erasure work, deliberately short of account erasure.

The service removes only concierge-created calendar entries plus tenant-scoped vault and
claim-check objects.  It never reads event content and returns status/counts only.  A future
account-erasure coordinator must add the RLS database crypto-shred, RelayInbox deletion, audit
tombstone, workflow fencing, legal-retention handling, and SLA/audit evidence required for a full
FR-10.5 claim (ADR-006/007/011).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from ..domain.ids import calendar_event_id
from ..ports.calendar import CalendarPort
from ..ports.credentials import CredentialVault
from ..ports.erasure import TenantErasureInventoryPort
from ..ports.object_store import ObjectStorePort


class ExternalArtifactPurgeStatus(StrEnum):
    """Truthful completion state for a best-effort, replay-safe external purge (FR-10.5)."""

    COMPLETED = "completed"
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class ExternalArtifactPurgeResult:
    """Non-PII result projection for an external-artifact pre-erasure attempt (FR-10.5, ADR-007).

    Counts describe successfully completed idempotent operations, rather than proving a full
    account-erasure request.  No target IDs, event titles, URLs, errors, raw text, or tenant data
    are returned so this projection is safe to persist in a non-PII operational audit.
    """

    status: ExternalArtifactPurgeStatus
    calendar_targets: int
    calendar_deleted: int
    credential_vault_purged: int
    object_store_purged: int
    failed_operations: int


class ExternalArtifactPurgeService:
    """Staged delete of known external artifacts with explicit partial-result semantics.

    Deterministic CalendarPort IDs and idempotent tenant-prefix deletion make a retry safe after a
    crash or a remote acknowledgement loss.  Calendar deletion completes before credential purge,
    so an unavailable calendar cannot be stranded by an irreversible credential revoke.  Later
    artifact families run only after earlier stages succeed; the result cannot claim completion
    unless every inventory/delete/purge operation succeeds (FR-10.5, ADR-006/007/011).
    """

    def __init__(
        self,
        inventory: TenantErasureInventoryPort,
        calendar: CalendarPort,
        vault: CredentialVault,
        object_store: ObjectStorePort,
    ) -> None:
        self._inventory = inventory
        self._calendar = calendar
        self._vault = vault
        self._object_store = object_store

    async def purge_tenant(self, tenant_id: UUID) -> ExternalArtifactPurgeResult:
        """Run staged external cleanup and expose only aggregate completion counts."""
        try:
            targets = await self._inventory.list_calendar_deletion_targets(tenant_id)
        except Exception:
            return self._result(failed_operations=1)
        if any(target.tenant_id != tenant_id for target in targets):
            # A malformed inventory must not convert an opaque cross-tenant identifier into a
            # CalendarPort mutation.  Returning no target details keeps this fail-closed result
            # non-PII while making retry/audit state truthful (FR-1.3, FR-10.5, ADR-007).
            return self._result(failed_operations=1)

        canonical_event_ids = tuple(
            sorted({target.canonical_event_id for target in targets}, key=str)
        )
        calendar_deleted = 0
        for canonical_event_id in canonical_event_ids:
            try:
                await self._calendar.delete_event(
                    tenant_id,
                    calendar_event_id(tenant_id, canonical_event_id),
                    canonical_event_id=canonical_event_id,
                )
            except Exception:
                return self._result(
                    calendar_targets=len(canonical_event_ids),
                    calendar_deleted=calendar_deleted,
                    failed_operations=1,
                )
            else:
                calendar_deleted += 1

        try:
            await self._vault.delete_tenant(tenant_id)
        except Exception:
            return self._result(
                calendar_targets=len(canonical_event_ids),
                calendar_deleted=calendar_deleted,
                failed_operations=1,
            )

        try:
            await self._object_store.delete_tenant(tenant_id)
        except Exception:
            return self._result(
                calendar_targets=len(canonical_event_ids),
                calendar_deleted=calendar_deleted,
                credential_vault_purged=1,
                failed_operations=1,
            )

        return self._result(
            calendar_targets=len(canonical_event_ids),
            calendar_deleted=calendar_deleted,
            credential_vault_purged=1,
            object_store_purged=1,
        )

    @staticmethod
    def _result(
        *,
        calendar_targets: int = 0,
        calendar_deleted: int = 0,
        credential_vault_purged: int = 0,
        object_store_purged: int = 0,
        failed_operations: int = 0,
    ) -> ExternalArtifactPurgeResult:
        """Build the intentionally PII-free aggregate result (FR-10.5, ADR-007)."""
        return ExternalArtifactPurgeResult(
            status=(
                ExternalArtifactPurgeStatus.COMPLETED
                if failed_operations == 0
                else ExternalArtifactPurgeStatus.PARTIAL
            ),
            calendar_targets=calendar_targets,
            calendar_deleted=calendar_deleted,
            credential_vault_purged=credential_vault_purged,
            object_store_purged=object_store_purged,
            failed_operations=failed_operations,
        )
