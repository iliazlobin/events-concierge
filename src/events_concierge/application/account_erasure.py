"""Resumable account erasure spanning engine, calendar, vault, object store, and PostgreSQL.

The database write fence is committed before any external operation. Each later family is
idempotent and durably acknowledged only after the whole family succeeds, so a crash or lost ACK
causes a safe repeat rather than an omitted delete. Database deletion is the final atomic stage.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from uuid import UUID

from ..domain.account_erasure import (
    AccountErasureFailureStage,
    AccountErasureLease,
    AccountErasureResult,
    AccountErasureSnapshot,
    AccountErasureStage,
    AccountErasureStatus,
)
from ..domain.ids import calendar_event_id
from ..ports.account_erasure import (
    AccountErasureRepository,
    TenantExternalEffectDrainPort,
    TenantSessionRevocationPort,
    TenantWorkflowCancellationPort,
)
from ..ports.calendar import CalendarPort
from ..ports.credentials import CredentialVault
from ..ports.media_store import MediaStorePort
from ..ports.object_store import ObjectStorePort

_MIN_ERASURE_LEASE_SECONDS = 5


class AccountErasureService:
    """Drive one tenant's fenced, replay-safe erasure command to completion when possible."""

    def __init__(
        self,
        repository: AccountErasureRepository,
        external_effects: TenantExternalEffectDrainPort,
        workflows: TenantWorkflowCancellationPort,
        calendar: CalendarPort,
        sessions: TenantSessionRevocationPort,
        vault: CredentialVault,
        object_store: ObjectStorePort,
        media_store: MediaStorePort | None = None,
    ) -> None:
        self._repository = repository
        self._external_effects = external_effects
        self._workflows = workflows
        self._calendar = calendar
        self._sessions = sessions
        self._vault = vault
        self._object_store = object_store
        self._media_store = media_store

    async def erase(self, tenant_id: UUID, request_id: UUID) -> AccountErasureResult:
        """Begin or resume the fixed stage order, returning truthful aggregate progress only."""
        snapshot = await self._repository.begin(tenant_id, request_id)
        if snapshot.status is AccountErasureStatus.COMPLETED:
            return self._result(snapshot)

        failed: AccountErasureStage | None = None
        for run_stage in (
            self._drain_external_effects,
            self._cancel_workflows,
            self._delete_calendar_entries,
            self._purge_vault,
            self._purge_object_store,
            self._revoke_browser_sessions,
        ):
            snapshot, failed = await run_stage(snapshot)
            if failed is not None:
                break

        if failed is None and snapshot.external_stages_completed:
            snapshot = await self._repository.finalize(tenant_id, request_id)
        # No partial result can be promoted into an irreversible database purge.
        return self._result(snapshot, failed_stage=failed)

    async def _drain_external_effects(
        self, snapshot: AccountErasureSnapshot
    ) -> tuple[AccountErasureSnapshot, AccountErasureStage | None]:
        stage = AccountErasureStage.EXTERNAL_EFFECTS
        if snapshot.external_effects_completed:
            return snapshot, None
        try:
            await self._external_effects.drain_tenant_effects(snapshot.tenant_id)
            snapshot = await self._repository.complete_stage(
                snapshot.tenant_id,
                snapshot.request_id,
                stage,
                1,
            )
        except Exception:
            return snapshot, stage
        return snapshot, None

    async def _cancel_workflows(
        self, snapshot: AccountErasureSnapshot
    ) -> tuple[AccountErasureSnapshot, AccountErasureStage | None]:
        stage = AccountErasureStage.WORKFLOWS
        if snapshot.workflows_completed:
            return snapshot, None
        try:
            await self._workflows.quiesce_tenant(snapshot.tenant_id, snapshot.workflow_ids)
            snapshot = await self._repository.complete_stage(
                snapshot.tenant_id,
                snapshot.request_id,
                stage,
                len(snapshot.workflow_ids),
            )
        except Exception:
            return snapshot, stage
        return snapshot, None

    async def _delete_calendar_entries(
        self, snapshot: AccountErasureSnapshot
    ) -> tuple[AccountErasureSnapshot, AccountErasureStage | None]:
        stage = AccountErasureStage.CALENDAR
        if snapshot.calendar_completed:
            return snapshot, None
        try:
            for canonical_event_id in snapshot.canonical_event_ids:
                await self._calendar.delete_event(
                    snapshot.tenant_id,
                    calendar_event_id(snapshot.tenant_id, canonical_event_id),
                    canonical_event_id=canonical_event_id,
                )
            # The local lifecycle projection is not an exhaustive remote inventory. The provider
            # adapter must additionally enumerate every app-owned event in the tenant binding so
            # orphaned or ACK-lost writes cannot survive account erasure.
            if snapshot.calendar_binding_expected:
                await self._calendar.delete_tenant_events(snapshot.tenant_id)
            snapshot = await self._repository.complete_stage(
                snapshot.tenant_id,
                snapshot.request_id,
                stage,
                len(snapshot.canonical_event_ids),
            )
        except Exception:
            return snapshot, stage
        return snapshot, None

    async def _purge_vault(
        self, snapshot: AccountErasureSnapshot
    ) -> tuple[AccountErasureSnapshot, AccountErasureStage | None]:
        stage = AccountErasureStage.CREDENTIAL_VAULT
        if snapshot.credential_vault_completed:
            return snapshot, None
        try:
            # CredentialVault.delete_tenant owns provider ciphertext, tenant DEK destruction, and
            # ephemeral RelayInbox secret deletion at this boundary (ADR-006/011).
            await self._vault.delete_tenant(snapshot.tenant_id)
            snapshot = await self._repository.complete_stage(
                snapshot.tenant_id,
                snapshot.request_id,
                stage,
                1,
            )
        except Exception:
            return snapshot, stage
        return snapshot, None

    async def _revoke_browser_sessions(
        self, snapshot: AccountErasureSnapshot
    ) -> tuple[AccountErasureSnapshot, AccountErasureStage | None]:
        stage = AccountErasureStage.BROWSER_SESSIONS
        if snapshot.browser_sessions_completed:
            return snapshot, None
        try:
            await self._sessions.revoke_tenant_sessions(snapshot.tenant_id)
            snapshot = await self._repository.complete_stage(
                snapshot.tenant_id,
                snapshot.request_id,
                stage,
                1,
            )
        except Exception:
            return snapshot, stage
        return snapshot, None

    async def _purge_object_store(
        self, snapshot: AccountErasureSnapshot
    ) -> tuple[AccountErasureSnapshot, AccountErasureStage | None]:
        stage = AccountErasureStage.OBJECT_STORE
        if snapshot.object_store_completed:
            return snapshot, None
        try:
            await self._object_store.delete_tenant(snapshot.tenant_id)
            if self._media_store is not None:
                # Profile media is durable and lives in its own store, so the tenant-row cascade
                # that removes its index does not touch the bytes. Purging both under one stage
                # keeps erasure a single retryable unit; both calls are idempotent.
                await self._media_store.delete_tenant(snapshot.tenant_id)
            snapshot = await self._repository.complete_stage(
                snapshot.tenant_id,
                snapshot.request_id,
                stage,
                1,
            )
        except Exception:
            return snapshot, stage
        return snapshot, None

    @staticmethod
    def _result(
        snapshot: AccountErasureSnapshot,
        *,
        failed_stage: AccountErasureStage | None = None,
    ) -> AccountErasureResult:
        return AccountErasureResult(
            request_id=snapshot.request_id,
            status=snapshot.status,
            workflow_targets=snapshot.workflow_target_count,
            workflows_cancelled=(
                snapshot.workflow_target_count if snapshot.workflows_completed else 0
            ),
            calendar_targets=snapshot.calendar_target_count,
            calendar_deleted=(
                snapshot.calendar_target_count if snapshot.calendar_completed else 0
            ),
            browser_sessions_revoked=snapshot.browser_sessions_completed,
            credential_vault_purged=snapshot.credential_vault_completed,
            object_store_purged=snapshot.object_store_completed,
            external_effects_drained=snapshot.external_effects_completed,
            retained_audit_rows=snapshot.retained_audit_rows,
            failed_stage=failed_stage,
        )


@dataclass(frozen=True, slots=True)
class AccountErasureWorkerStats:
    """PII-free aggregate evidence from one durable resume pass."""

    claimed: int = 0
    completed: int = 0
    rescheduled: int = 0
    lost_leases: int = 0


class AccountErasureWorker:
    """Resume fenced account erasures independently of the initiating browser request."""

    def __init__(
        self,
        repository: AccountErasureRepository,
        service: AccountErasureService,
        *,
        lease_seconds: int,
        retry_base_seconds: int = 30,
        retry_max_seconds: int = 3600,
    ) -> None:
        if lease_seconds < _MIN_ERASURE_LEASE_SECONDS:
            raise ValueError("account erasure lease_seconds must be at least five")
        if retry_base_seconds < 1 or retry_max_seconds < retry_base_seconds:
            raise ValueError("account erasure retry bounds are invalid")
        self._repository = repository
        self._service = service
        self._lease_seconds = lease_seconds
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds

    async def run_once(self, *, limit: int) -> AccountErasureWorkerStats:
        """Claim and resume a bounded batch; expired/lost leases remain safely replayable."""
        leases = await self._repository.claim_batch(limit, self._lease_seconds)
        completed = 0
        rescheduled = 0
        lost_leases = 0
        for lease in leases:
            failure_stage = AccountErasureFailureStage.DATABASE
            try:
                result = await self._erase_with_heartbeat(lease)
            except _AccountErasureLeaseLostError:
                lost_leases += 1
                continue
            except Exception:
                failure_stage = AccountErasureFailureStage.DATABASE
            else:
                if result.status is AccountErasureStatus.COMPLETED:
                    completed += 1
                    continue
                if result.failed_stage is not None:
                    failure_stage = AccountErasureFailureStage(result.failed_stage.value)

            released = await self._repository.release_lease(
                lease,
                failure_stage=failure_stage,
                retry_after_seconds=self._retry_delay(lease.attempt_count),
            )
            if released:
                rescheduled += 1
            else:
                lost_leases += 1
        return AccountErasureWorkerStats(
            claimed=len(leases),
            completed=completed,
            rescheduled=rescheduled,
            lost_leases=lost_leases,
        )

    def _retry_delay(self, attempt_count: int) -> int:
        exponent = max(0, min(attempt_count - 1, 10))
        delay = self._retry_base_seconds << exponent
        return min(self._retry_max_seconds, delay)

    async def _erase_with_heartbeat(self, lease: AccountErasureLease) -> AccountErasureResult:
        operation = asyncio.create_task(self._service.erase(lease.tenant_id, lease.request_id))
        heartbeat_seconds = self._lease_seconds / 3
        try:
            while True:
                done, _ = await asyncio.wait({operation}, timeout=heartbeat_seconds)
                if operation in done:
                    return await operation
                if not await self._repository.renew_lease(lease, self._lease_seconds):
                    operation.cancel()
                    with suppress(asyncio.CancelledError):
                        await operation
                    raise _AccountErasureLeaseLostError
        finally:
            if not operation.done():
                operation.cancel()
                with suppress(asyncio.CancelledError):
                    await operation


class _AccountErasureLeaseLostError(RuntimeError):
    """Stop external progress immediately when another worker may own the durable command."""
