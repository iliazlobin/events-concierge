"""PostgreSQL adapter for the fenced, resumable account-erasure capability."""

from __future__ import annotations

from typing import Protocol, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.account_erasure import (
    AccountErasureFailureStage,
    AccountErasureLease,
    AccountErasureSnapshot,
    AccountErasureStage,
    AccountErasureStatus,
)
from ...infra.db import system_session_scope, tenant_session_scope
from ...ports.account_erasure import (
    AccountErasureAccountNotFoundError,
    AccountErasureConflictError,
    AccountErasureInvariantError,
)

_MAX_CLAIM_BATCH = 100
_MIN_LEASE_SECONDS = 5
_MAX_LEASE_SECONDS = 3600
_MAX_RETRY_SECONDS = 3600


class _AccountErasureLeaseRow(Protocol):
    tenant_id: UUID
    request_id: UUID
    attempt_count: int
    lease_token: UUID


class PostgresAccountErasureRepository:
    """Use owner-defined SQL capabilities while keeping every caller tenant-derived."""

    async def begin(self, tenant_id: UUID, request_id: UUID) -> AccountErasureSnapshot:
        async with tenant_session_scope(tenant_id) as session:
            outcome = str(
                (
                    await session.execute(
                        text("SELECT public.fn_begin_account_erasure(:request_id)"),
                        {"request_id": request_id},
                    )
                ).scalar_one()
            )
            if outcome == "conflict":
                raise AccountErasureConflictError(
                    "account erasure is already bound to another request id"
                )
            if outcome == "not_found":
                raise AccountErasureAccountNotFoundError("account not found")
            if outcome not in {"started", "replayed"}:
                raise AccountErasureInvariantError(
                    "account erasure begin capability returned an invalid outcome"
                )
            snapshot = await self._load(session, tenant_id)
        if snapshot is None:
            raise AccountErasureInvariantError("account erasure begin did not persist a tombstone")
        return snapshot

    async def get(self, tenant_id: UUID) -> AccountErasureSnapshot | None:
        async with tenant_session_scope(tenant_id) as session:
            return await self._load(session, tenant_id)

    async def complete_stage(
        self,
        tenant_id: UUID,
        request_id: UUID,
        stage: AccountErasureStage,
        completed_count: int,
    ) -> AccountErasureSnapshot:
        if completed_count < 0:
            raise ValueError("completed erasure count cannot be negative")
        async with tenant_session_scope(tenant_id) as session:
            outcome = str(
                (
                    await session.execute(
                        text(
                            """SELECT public.fn_complete_account_erasure_stage(
                                   :request_id, :stage, :completed_count
                               )"""
                        ),
                        {
                            "request_id": request_id,
                            "stage": stage.value,
                            "completed_count": completed_count,
                        },
                    )
                ).scalar_one()
            )
            if outcome == "conflict":
                raise AccountErasureConflictError("account erasure stage replay conflicts")
            if outcome != "completed":
                raise AccountErasureInvariantError(
                    "account erasure stage capability returned an invalid outcome"
                )
            snapshot = await self._load(session, tenant_id)
        if snapshot is None:
            raise AccountErasureInvariantError("account erasure stage lost its tombstone")
        return snapshot

    async def finalize(self, tenant_id: UUID, request_id: UUID) -> AccountErasureSnapshot:
        async with tenant_session_scope(tenant_id) as session:
            outcome = str(
                (
                    await session.execute(
                        text("SELECT public.fn_finalize_account_erasure(:request_id)"),
                        {"request_id": request_id},
                    )
                ).scalar_one()
            )
            if outcome == "conflict":
                raise AccountErasureConflictError("account erasure finalization conflicts")
            if outcome != "completed":
                raise AccountErasureInvariantError(
                    "account erasure cannot finalize before every external stage"
                )
            snapshot = await self._load(session, tenant_id)
        if snapshot is None or snapshot.status is not AccountErasureStatus.COMPLETED:
            raise AccountErasureInvariantError("account erasure finalization lost its tombstone")
        return snapshot

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[AccountErasureLease]:
        if not 1 <= limit <= _MAX_CLAIM_BATCH:
            raise ValueError("account erasure claim limit must be between 1 and 100")
        if not _MIN_LEASE_SECONDS <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise ValueError("account erasure lease_seconds must be between 5 and 3600")
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """SELECT tenant_id, request_id, attempt_count, lease_token
                           FROM public.fn_claim_account_erasure_batch(
                               :limit, :lease_seconds
                           )"""
                    ),
                    {"limit": limit, "lease_seconds": lease_seconds},
                )
            ).all()
        return [self._lease(cast(_AccountErasureLeaseRow, row)) for row in rows]

    async def release_lease(
        self,
        lease: AccountErasureLease,
        *,
        failure_stage: AccountErasureFailureStage,
        retry_after_seconds: int,
    ) -> bool:
        if not 1 <= retry_after_seconds <= _MAX_RETRY_SECONDS:
            raise ValueError("account erasure retry delay must be between 1 and 3600 seconds")
        async with system_session_scope() as session:
            return bool(
                (
                    await session.execute(
                        text(
                            """SELECT public.fn_release_account_erasure_lease(
                                   :tenant_id, :request_id, :lease_token,
                                   :failure_stage, :retry_after_seconds
                               )"""
                        ),
                        {
                            "tenant_id": lease.tenant_id,
                            "request_id": lease.request_id,
                            "lease_token": lease.lease_token,
                            "failure_stage": failure_stage.value,
                            "retry_after_seconds": retry_after_seconds,
                        },
                    )
                ).scalar_one()
            )

    async def renew_lease(self, lease: AccountErasureLease, lease_seconds: int) -> bool:
        if not _MIN_LEASE_SECONDS <= lease_seconds <= _MAX_LEASE_SECONDS:
            raise ValueError("account erasure lease_seconds must be between 5 and 3600")
        async with system_session_scope() as session:
            return bool(
                (
                    await session.execute(
                        text(
                            """SELECT public.fn_renew_account_erasure_lease(
                                   :tenant_id, :request_id, :lease_token, :lease_seconds
                               )"""
                        ),
                        {
                            "tenant_id": lease.tenant_id,
                            "request_id": lease.request_id,
                            "lease_token": lease.lease_token,
                            "lease_seconds": lease_seconds,
                        },
                    )
                ).scalar_one()
            )

    @staticmethod
    async def _load(
        session: AsyncSession, tenant_id: UUID
    ) -> AccountErasureSnapshot | None:
        row = (
            await session.execute(
                text(
                    """SELECT request_id, status, workflow_target_count,
                              calendar_target_count, calendar_binding_expected,
                              external_effects_drained_at, workflows_cancelled_at,
                              calendar_purged_at, browser_sessions_revoked_at,
                              credential_vault_purged_at,
                              object_store_purged_at, retained_audit_rows,
                              last_failure_stage
                       FROM public.account_erasure_requests
                       WHERE tenant_id = :tenant_id"""
                ),
                {"tenant_id": tenant_id},
            )
        ).first()
        if row is None:
            return None

        status = AccountErasureStatus(str(row.status))
        workflow_ids: tuple[str, ...] = ()
        canonical_event_ids: tuple[UUID, ...] = ()
        if status is AccountErasureStatus.ERASING:
            workflow_ids = tuple(
                str(workflow_row.workflow_id)
                for workflow_row in (
                    await session.execute(
                        text(
                            """SELECT target.workflow_id
                               FROM public.account_erasure_workflow_targets AS target
                               WHERE target.tenant_id = :tenant_id
                               ORDER BY target.workflow_id"""
                        ),
                        {"tenant_id": tenant_id},
                    )
                ).all()
            )
            canonical_event_ids = tuple(
                event_row.canonical_event_id
                for event_row in (
                    await session.execute(
                        text(
                            """SELECT target.canonical_event_id
                               FROM public.account_erasure_calendar_targets AS target
                               WHERE target.tenant_id = :tenant_id
                               ORDER BY target.canonical_event_id"""
                        ),
                        {"tenant_id": tenant_id},
                    )
                ).all()
            )
            if len(workflow_ids) != int(row.workflow_target_count):
                raise AccountErasureInvariantError(
                    "fenced workflow inventory differs from its durable count"
                )
            if len(canonical_event_ids) != int(row.calendar_target_count):
                raise AccountErasureInvariantError(
                    "fenced calendar inventory differs from its durable count"
                )

        return AccountErasureSnapshot(
            tenant_id=tenant_id,
            request_id=row.request_id,
            status=status,
            workflow_ids=workflow_ids,
            canonical_event_ids=canonical_event_ids,
            workflow_target_count=int(row.workflow_target_count),
            calendar_target_count=int(row.calendar_target_count),
            calendar_binding_expected=bool(row.calendar_binding_expected),
            external_effects_completed=row.external_effects_drained_at is not None,
            workflows_completed=row.workflows_cancelled_at is not None,
            calendar_completed=row.calendar_purged_at is not None,
            browser_sessions_completed=row.browser_sessions_revoked_at is not None,
            credential_vault_completed=row.credential_vault_purged_at is not None,
            object_store_completed=row.object_store_purged_at is not None,
            retained_audit_rows=int(row.retained_audit_rows),
            last_failure_stage=(
                AccountErasureFailureStage(str(row.last_failure_stage))
                if row.last_failure_stage is not None
                else None
            ),
        )

    @staticmethod
    def _lease(row: _AccountErasureLeaseRow) -> AccountErasureLease:
        return AccountErasureLease(
            tenant_id=row.tenant_id,
            request_id=row.request_id,
            attempt_count=int(row.attempt_count),
            lease_token=row.lease_token,
        )
