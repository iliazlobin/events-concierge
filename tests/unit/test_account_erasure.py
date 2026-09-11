"""Offline resumable account-erasure coordinator contracts (FR-10.5, NFR-10/11)."""

from __future__ import annotations

from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from events_concierge.adapters.disabled import DisabledCalendar, DisabledCredentialVault
from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.adapters.mock.object_store import MockFilesystemObjectStore
from events_concierge.adapters.mock.vault import MockVault
from events_concierge.application.account_erasure import (
    AccountErasureService,
    AccountErasureWorker,
)
from events_concierge.domain.account_erasure import (
    AccountErasureFailureStage,
    AccountErasureLease,
    AccountErasureSnapshot,
    AccountErasureStage,
    AccountErasureStatus,
)
from events_concierge.domain.credentials import Credential
from events_concierge.domain.enums import CredentialKind, Source
from events_concierge.domain.ids import calendar_event_id
from events_concierge.ports.calendar import CalendarEntry


class _MemoryErasureRepository:
    """One-command state machine that mirrors the SQL capability for coordinator tests."""

    def __init__(self, snapshot: AccountErasureSnapshot, operations: list[str]) -> None:
        self.snapshot = snapshot
        self.operations = operations
        self.finalize_calls = 0
        self.leases: list[AccountErasureLease] = []
        self.releases: list[tuple[AccountErasureFailureStage, int]] = []
        self.release_succeeds = True

    async def begin(self, tenant_id: UUID, request_id: UUID) -> AccountErasureSnapshot:
        assert tenant_id == self.snapshot.tenant_id
        assert request_id == self.snapshot.request_id
        self.operations.append("begin")
        return self.snapshot

    async def get(self, tenant_id: UUID) -> AccountErasureSnapshot | None:
        return self.snapshot if tenant_id == self.snapshot.tenant_id else None

    async def complete_stage(
        self,
        tenant_id: UUID,
        request_id: UUID,
        stage: AccountErasureStage,
        completed_count: int,
    ) -> AccountErasureSnapshot:
        assert tenant_id == self.snapshot.tenant_id
        assert request_id == self.snapshot.request_id
        expected = {
            AccountErasureStage.EXTERNAL_EFFECTS: 1,
            AccountErasureStage.WORKFLOWS: self.snapshot.workflow_target_count,
            AccountErasureStage.CALENDAR: self.snapshot.calendar_target_count,
            AccountErasureStage.BROWSER_SESSIONS: 1,
            AccountErasureStage.CREDENTIAL_VAULT: 1,
            AccountErasureStage.OBJECT_STORE: 1,
        }[stage]
        assert completed_count == expected
        self.operations.append(f"mark:{stage.value}")
        if stage is AccountErasureStage.EXTERNAL_EFFECTS:
            self.snapshot = replace(self.snapshot, external_effects_completed=True)
        elif stage is AccountErasureStage.WORKFLOWS:
            self.snapshot = replace(self.snapshot, workflows_completed=True)
        elif stage is AccountErasureStage.CALENDAR:
            self.snapshot = replace(self.snapshot, calendar_completed=True)
        elif stage is AccountErasureStage.BROWSER_SESSIONS:
            self.snapshot = replace(self.snapshot, browser_sessions_completed=True)
        elif stage is AccountErasureStage.CREDENTIAL_VAULT:
            self.snapshot = replace(self.snapshot, credential_vault_completed=True)
        else:
            self.snapshot = replace(self.snapshot, object_store_completed=True)
        return self.snapshot

    async def finalize(self, tenant_id: UUID, request_id: UUID) -> AccountErasureSnapshot:
        assert tenant_id == self.snapshot.tenant_id
        assert request_id == self.snapshot.request_id
        assert self.snapshot.external_stages_completed
        self.operations.append("finalize")
        self.finalize_calls += 1
        self.snapshot = replace(
            self.snapshot,
            status=AccountErasureStatus.COMPLETED,
            workflow_ids=(),
            canonical_event_ids=(),
        )
        return self.snapshot

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[AccountErasureLease]:
        assert limit > 0
        assert lease_seconds >= 5
        claimed, self.leases = self.leases[:limit], self.leases[limit:]
        return claimed

    async def release_lease(
        self,
        lease: AccountErasureLease,
        *,
        failure_stage: AccountErasureFailureStage,
        retry_after_seconds: int,
    ) -> bool:
        assert lease.tenant_id == self.snapshot.tenant_id
        self.releases.append((failure_stage, retry_after_seconds))
        return self.release_succeeds

    async def renew_lease(self, lease: AccountErasureLease, lease_seconds: int) -> bool:
        assert lease.tenant_id == self.snapshot.tenant_id
        assert lease_seconds >= 5
        return True


class _WorkflowCanceller:
    def __init__(self, operations: list[str], *, fail_once: str | None = None) -> None:
        self.operations = operations
        self.fail_once = fail_once

    async def cancel(self, workflow_id: str) -> None:
        self.operations.append(f"workflow:{workflow_id}")
        if workflow_id == self.fail_once:
            self.fail_once = None
            raise RuntimeError("opaque workflow cancellation failure")

    async def quiesce_tenant(
        self,
        tenant_id: UUID,
        known_workflow_ids: tuple[str, ...],
    ) -> None:
        del tenant_id
        for workflow_id in known_workflow_ids:
            await self.cancel(workflow_id)


class _RecordingExternalEffects:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations

    async def drain_tenant_effects(self, tenant_id: UUID) -> None:
        del tenant_id
        self.operations.append("external_effects")


class _FailingExternalEffects:
    async def drain_tenant_effects(self, tenant_id: UUID) -> None:
        del tenant_id
        raise RuntimeError("opaque external-effect drain failure")


class _RecordingCalendar(MockCalendar):
    def __init__(self, operations: list[str], *, fail_once: UUID | None = None) -> None:
        super().__init__()
        self.operations = operations
        self.fail_once = fail_once

    async def delete_event(
        self,
        tenant_id: UUID,
        calendar_event_id_value: str,
        *,
        canonical_event_id: UUID | None = None,
    ) -> None:
        self.operations.append(f"calendar:{canonical_event_id}")
        if canonical_event_id == self.fail_once:
            self.fail_once = None
            raise RuntimeError("opaque calendar deletion failure")
        await super().delete_event(
            tenant_id,
            calendar_event_id_value,
            canonical_event_id=canonical_event_id,
        )


class _RecordingVault(MockVault):
    def __init__(self, operations: list[str]) -> None:
        super().__init__()
        self.operations = operations

    async def delete_tenant(self, tenant_id: UUID) -> None:
        self.operations.append("vault")
        await super().delete_tenant(tenant_id)


class _RecordingSessions:
    def __init__(self, operations: list[str]) -> None:
        self.operations = operations

    async def revoke_tenant_sessions(self, tenant_id: UUID) -> None:
        del tenant_id
        self.operations.append("browser_sessions")


class _RecordingObjectStore(MockFilesystemObjectStore):
    def __init__(self, root: Path, operations: list[str]) -> None:
        super().__init__(root)
        self.operations = operations

    async def delete_tenant(self, tenant_id: UUID) -> None:
        self.operations.append("object_store")
        await super().delete_tenant(tenant_id)


def _snapshot(
    tenant_id: UUID, request_id: UUID, event_ids: tuple[UUID, ...]
) -> AccountErasureSnapshot:
    workflow_ids = (
        f"req:{tenant_id}:{request_id}",
        *(f"{tenant_id}:{event_id}" for event_id in event_ids),
    )
    return AccountErasureSnapshot(
        tenant_id=tenant_id,
        request_id=request_id,
        status=AccountErasureStatus.ERASING,
        workflow_ids=workflow_ids,
        canonical_event_ids=event_ids,
        workflow_target_count=len(workflow_ids),
        calendar_target_count=len(event_ids),
        calendar_binding_expected=True,
        external_effects_completed=False,
        workflows_completed=False,
        calendar_completed=False,
        browser_sessions_completed=False,
        credential_vault_completed=False,
        object_store_completed=False,
        retained_audit_rows=3,
        last_failure_stage=None,
    )


def _entry(tenant_id: UUID, event_id: UUID) -> CalendarEntry:
    return CalendarEntry(
        calendar_event_id=calendar_event_id(tenant_id, event_id),
        canonical_event_id=event_id,
        title="private title",
        start_at=datetime(2026, 7, 22, 18, tzinfo=UTC),
        end_at=datetime(2026, 7, 22, 20, tzinfo=UTC),
        time_zone="America/Los_Angeles",
    )


async def test_erasure_fences_then_orders_external_cleanup_before_atomic_database_purge(
    tmp_path: Path,
) -> None:
    tenant_id, request_id = uuid4(), uuid4()
    event_ids = (uuid4(), uuid4())
    operations: list[str] = []
    repository = _MemoryErasureRepository(_snapshot(tenant_id, request_id, event_ids), operations)
    workflows = _WorkflowCanceller(operations)
    calendar = _RecordingCalendar(operations)
    vault = _RecordingVault(operations)
    object_store = _RecordingObjectStore(tmp_path / "claims", operations)
    service = AccountErasureService(
        repository,
        _RecordingExternalEffects(operations),
        workflows,
        calendar,
        _RecordingSessions(operations),
        vault,
        object_store,
    )

    for event_id in event_ids:
        await calendar.upsert_event(tenant_id, _entry(tenant_id, event_id))
    await vault.store(
        Credential(
            credential_id=uuid4(),
            tenant_id=tenant_id,
            source=Source.MEETUP,
            kind=CredentialKind.OAUTH_REFRESH,
            ciphertext=b"opaque",
            bound_origin="https://www.meetup.com",
        )
    )
    await object_store.put(tenant_id, "temporal/private.payload", b"private bytes")

    result = await service.erase(tenant_id, request_id)

    assert result.status is AccountErasureStatus.COMPLETED
    assert result.failed_stage is None
    assert result.external_effects_drained is True
    assert result.workflow_targets == 3
    assert result.workflows_cancelled == 3
    assert result.calendar_targets == 2
    assert result.calendar_deleted == 2
    assert result.credential_vault_purged is True
    assert result.object_store_purged is True
    assert result.retained_audit_rows == 3
    assert repository.finalize_calls == 1
    assert operations == [
        "begin",
        "external_effects",
        "mark:external_effects",
        f"workflow:req:{tenant_id}:{request_id}",
        f"workflow:{tenant_id}:{event_ids[0]}",
        f"workflow:{tenant_id}:{event_ids[1]}",
        "mark:workflows",
        f"calendar:{event_ids[0]}",
        f"calendar:{event_ids[1]}",
        "mark:calendar",
        "vault",
        "mark:credential_vault",
        "object_store",
        "mark:object_store",
        "browser_sessions",
        "mark:browser_sessions",
        "finalize",
    ]
    assert calendar.entries(tenant_id) == []
    assert await vault.get(tenant_id, Source.MEETUP) is None
    assert not (tmp_path / "claims" / str(tenant_id)).exists()
    assert {field.name for field in fields(result)} == {
        "request_id",
        "status",
        "workflow_targets",
        "workflows_cancelled",
        "calendar_targets",
        "calendar_deleted",
        "browser_sessions_revoked",
        "credential_vault_purged",
        "object_store_purged",
        "external_effects_drained",
        "retained_audit_rows",
        "failed_stage",
    }
    assert "private" not in repr(result).lower()


async def test_partial_stage_is_not_acknowledged_and_exact_retry_resumes_safely(
    tmp_path: Path,
) -> None:
    tenant_id, request_id, event_id = uuid4(), uuid4(), uuid4()
    operations: list[str] = []
    repository = _MemoryErasureRepository(_snapshot(tenant_id, request_id, (event_id,)), operations)
    workflows = _WorkflowCanceller(operations)
    calendar = _RecordingCalendar(operations, fail_once=event_id)
    vault = _RecordingVault(operations)
    object_store = _RecordingObjectStore(tmp_path / "claims", operations)
    service = AccountErasureService(
        repository,
        _RecordingExternalEffects(operations),
        workflows,
        calendar,
        _RecordingSessions(operations),
        vault,
        object_store,
    )
    await calendar.upsert_event(tenant_id, _entry(tenant_id, event_id))

    partial = await service.erase(tenant_id, request_id)

    assert partial.status is AccountErasureStatus.ERASING
    assert partial.failed_stage is AccountErasureStage.CALENDAR
    assert partial.workflows_cancelled == 2
    assert partial.calendar_deleted == 0
    assert repository.finalize_calls == 0
    assert "mark:calendar" not in operations
    first_workflow_operations = [item for item in operations if item.startswith("workflow:")]

    completed = await service.erase(tenant_id, request_id)

    assert completed.status is AccountErasureStatus.COMPLETED
    assert completed.failed_stage is None
    assert [
        item for item in operations if item.startswith("workflow:")
    ] == first_workflow_operations
    assert operations.count(f"calendar:{event_id}") == 2
    assert operations.count("mark:calendar") == 1
    assert repository.finalize_calls == 1


@pytest.mark.parametrize("binding_expected", [True, False])
async def test_discovery_erasure_stays_pending_without_external_cleanup_evidence(
    tmp_path: Path,
    binding_expected: bool,
) -> None:
    tenant_id, request_id = uuid4(), uuid4()
    operations: list[str] = []
    snapshot = replace(
        _snapshot(tenant_id, request_id, ()),
        calendar_binding_expected=binding_expected,
    )
    repository = _MemoryErasureRepository(snapshot, operations)
    service = AccountErasureService(
        repository,
        _RecordingExternalEffects(operations),
        _WorkflowCanceller(operations),
        DisabledCalendar(),
        _RecordingSessions(operations),
        DisabledCredentialVault(),
        _RecordingObjectStore(tmp_path / "claims", operations),
    )

    result = await service.erase(tenant_id, request_id)
    expected = (
        AccountErasureStage.CALENDAR if binding_expected else AccountErasureStage.CREDENTIAL_VAULT
    )
    assert result.status is AccountErasureStatus.ERASING and result.failed_stage is expected
    assert not result.credential_vault_purged and not result.object_store_purged
    assert repository.finalize_calls == 0
    assert "mark:credential_vault" not in operations
    assert "object_store" not in operations
    if binding_expected:
        assert "mark:calendar" not in operations
    retry = await service.erase(tenant_id, request_id)
    assert retry.status is AccountErasureStatus.ERASING and retry.failed_stage is expected
    assert repository.finalize_calls == 0


async def test_completed_replay_performs_no_external_effect_twice(tmp_path: Path) -> None:
    tenant_id, request_id = uuid4(), uuid4()
    operations: list[str] = []
    completed_snapshot = replace(
        _snapshot(tenant_id, request_id, ()),
        status=AccountErasureStatus.COMPLETED,
        workflow_ids=(),
        workflows_completed=True,
        external_effects_completed=True,
        calendar_completed=True,
        browser_sessions_completed=True,
        credential_vault_completed=True,
        object_store_completed=True,
    )
    repository = _MemoryErasureRepository(completed_snapshot, operations)
    service = AccountErasureService(
        repository,
        _RecordingExternalEffects(operations),
        _WorkflowCanceller(operations),
        _RecordingCalendar(operations),
        _RecordingSessions(operations),
        _RecordingVault(operations),
        _RecordingObjectStore(tmp_path / "claims", operations),
    )

    result = await service.erase(tenant_id, request_id)

    assert result.status is AccountErasureStatus.COMPLETED
    assert operations == ["begin"]
    assert repository.finalize_calls == 0


async def test_durable_worker_reschedules_fixed_failure_then_completes_without_browser(
    tmp_path: Path,
) -> None:
    tenant_id, request_id, event_id = uuid4(), uuid4(), uuid4()
    operations: list[str] = []
    repository = _MemoryErasureRepository(_snapshot(tenant_id, request_id, (event_id,)), operations)
    repository.leases.append(AccountErasureLease(tenant_id, request_id, 1, uuid4()))
    workflows = _WorkflowCanceller(
        operations,
        fail_once=f"req:{tenant_id}:{request_id}",
    )
    calendar = _RecordingCalendar(operations)
    await calendar.upsert_event(tenant_id, _entry(tenant_id, event_id))
    worker = AccountErasureWorker(
        repository,
        AccountErasureService(
            repository,
            _RecordingExternalEffects(operations),
            workflows,
            calendar,
            _RecordingSessions(operations),
            _RecordingVault(operations),
            _RecordingObjectStore(tmp_path / "claims", operations),
        ),
        lease_seconds=120,
    )

    pending = await worker.run_once(limit=10)

    assert pending.claimed == 1
    assert pending.completed == 0
    assert pending.rescheduled == 1
    assert pending.lost_leases == 0
    assert repository.releases == [(AccountErasureFailureStage.WORKFLOWS, 30)]
    assert repository.snapshot.status.value == AccountErasureStatus.ERASING.value

    repository.leases.append(AccountErasureLease(tenant_id, request_id, 2, uuid4()))
    completed = await worker.run_once(limit=10)

    assert completed.claimed == 1
    assert completed.completed == 1
    assert completed.rescheduled == 0
    assert repository.snapshot.status.value == AccountErasureStatus.COMPLETED.value
    assert repository.releases == [(AccountErasureFailureStage.WORKFLOWS, 30)]


async def test_durable_worker_reschedules_external_effect_drain_failure(
    tmp_path: Path,
) -> None:
    tenant_id, request_id = uuid4(), uuid4()
    operations: list[str] = []
    repository = _MemoryErasureRepository(_snapshot(tenant_id, request_id, ()), operations)
    repository.leases.append(AccountErasureLease(tenant_id, request_id, 1, uuid4()))
    worker = AccountErasureWorker(
        repository,
        AccountErasureService(
            repository,
            _FailingExternalEffects(),
            _WorkflowCanceller(operations),
            _RecordingCalendar(operations),
            _RecordingSessions(operations),
            _RecordingVault(operations),
            _RecordingObjectStore(tmp_path / "claims", operations),
        ),
        lease_seconds=120,
    )

    pending = await worker.run_once(limit=10)

    assert pending.claimed == 1
    assert pending.completed == 0
    assert pending.rescheduled == 1
    assert pending.lost_leases == 0
    assert repository.releases == [(AccountErasureFailureStage.EXTERNAL_EFFECTS, 30)]
    assert operations == ["begin"]
