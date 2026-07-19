"""Offline external-artifact pre-erasure contract tests (FR-10.5, ADR-006/007/011)."""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.adapters.mock.erasure import MockTenantErasureInventory
from events_concierge.adapters.mock.object_store import MockFilesystemObjectStore
from events_concierge.adapters.mock.vault import MockVault
from events_concierge.application.external_artifact_purge import (
    ExternalArtifactPurgeService,
    ExternalArtifactPurgeStatus,
)
from events_concierge.domain.credentials import Credential
from events_concierge.domain.enums import CredentialKind, Source
from events_concierge.domain.ids import calendar_event_id
from events_concierge.ports.calendar import CalendarEntry
from events_concierge.ports.erasure import CalendarDeletionTarget


def _entry(tenant_id: UUID, canonical_event_id: UUID, title: str) -> CalendarEntry:
    return CalendarEntry(
        calendar_event_id=calendar_event_id(tenant_id, canonical_event_id),
        canonical_event_id=canonical_event_id,
        title=title,
        start_at=datetime(2026, 7, 17, 18, 0, tzinfo=UTC),
        end_at=datetime(2026, 7, 17, 20, 0, tzinfo=UTC),
        time_zone="America/Los_Angeles",
    )


def _credential(tenant_id: UUID, source: Source) -> Credential:
    return Credential(
        credential_id=uuid4(),
        tenant_id=tenant_id,
        source=source,
        kind=CredentialKind.SESSION_COOKIE,
        ciphertext=b"opaque-fixture-ciphertext",
        bound_origin="https://example.test",
    )


class _RecordingCalendar(MockCalendar):
    """Calendar double that records destructive-stage ordering without event content."""

    def __init__(self, operations: list[str]) -> None:
        super().__init__()
        self._operations = operations
        self.delete_attempts: list[str] = []

    async def delete_event(
        self,
        tenant_id: UUID,
        calendar_event_id: str,
        *,
        canonical_event_id: UUID | None = None,
    ) -> None:
        self._operations.append("calendar")
        self.delete_attempts.append(calendar_event_id)
        await super().delete_event(
            tenant_id,
            calendar_event_id,
            canonical_event_id=canonical_event_id,
        )


class _RecordingVault(MockVault):
    """Vault double that records only which stage ran."""

    def __init__(self, operations: list[str]) -> None:
        super().__init__()
        self._operations = operations

    async def delete_tenant(self, tenant_id: UUID) -> None:
        self._operations.append("vault")
        await super().delete_tenant(tenant_id)


class _RecordingObjectStore(MockFilesystemObjectStore):
    """Claim-store double that records only which stage ran."""

    def __init__(self, root: Path, operations: list[str]) -> None:
        super().__init__(root)
        self._operations = operations

    async def delete_tenant(self, tenant_id: UUID) -> None:
        self._operations.append("object_store")
        await super().delete_tenant(tenant_id)


class _StaticInventory:
    """Minimal malformed/duplicate inventory fixture for consumer fail-closed tests."""

    def __init__(self, targets: tuple[CalendarDeletionTarget, ...]) -> None:
        self._targets = targets

    async def list_calendar_deletion_targets(
        self, tenant_id: UUID
    ) -> tuple[CalendarDeletionTarget, ...]:
        del tenant_id
        return self._targets


async def test_purge_is_tenant_isolated_and_removes_all_known_external_artifacts(
    tmp_path: Path,
) -> None:
    """One tenant's calendar/vault/claim purge cannot touch another tenant (FR-1.3, FR-10.5)."""
    first_tenant, second_tenant = uuid4(), uuid4()
    first_events, second_event = (uuid4(), uuid4()), uuid4()
    operations: list[str] = []
    calendar = _RecordingCalendar(operations)
    inventory = MockTenantErasureInventory()
    vault = _RecordingVault(operations)
    object_store = _RecordingObjectStore(tmp_path / "claim-check", operations)
    service = ExternalArtifactPurgeService(inventory, calendar, vault, object_store)
    inventory.seed_calendar_targets(first_tenant, first_events)
    inventory.seed_calendar_targets(second_tenant, [second_event])
    for canonical_event_id in first_events:
        await calendar.upsert_event(
            first_tenant,
            _entry(first_tenant, canonical_event_id, "First tenant private event"),
        )
    await calendar.upsert_event(
        second_tenant,
        _entry(second_tenant, second_event, "Second tenant private event"),
    )
    await vault.store(_credential(first_tenant, Source.LUMA))
    await vault.store(_credential(first_tenant, Source.MEETUP))
    await vault.store(_credential(second_tenant, Source.MEETUP))
    await object_store.put(first_tenant, "temporal/one.payload", b"first tenant bytes")
    await object_store.put(second_tenant, "temporal/two.payload", b"second tenant bytes")

    result = await service.purge_tenant(first_tenant)

    assert result.status is ExternalArtifactPurgeStatus.COMPLETED
    assert result.calendar_targets == 2
    assert result.calendar_deleted == 2
    assert result.credential_vault_purged == 1
    assert result.object_store_purged == 1
    assert result.failed_operations == 0
    assert operations == ["calendar", "calendar", "vault", "object_store"]
    assert calendar.entries(first_tenant) == []
    assert [entry.canonical_event_id for entry in calendar.entries(second_tenant)] == [second_event]
    assert await vault.get(first_tenant, Source.LUMA) is None
    assert await vault.get(first_tenant, Source.MEETUP) is None
    assert await vault.get(second_tenant, Source.MEETUP) is not None
    assert not (tmp_path / "claim-check" / str(first_tenant)).exists()
    assert await object_store.get(second_tenant, "temporal/two.payload") == b"second tenant bytes"


async def test_inventory_tenant_mismatch_fails_closed_before_any_external_effect(
    tmp_path: Path,
) -> None:
    """A cross-tenant opaque target is rejected before calendar, vault, or object-store mutation."""
    tenant_id, foreign_tenant, canonical_event_id = uuid4(), uuid4(), uuid4()
    operations: list[str] = []
    calendar = _RecordingCalendar(operations)
    vault = _RecordingVault(operations)
    object_store = _RecordingObjectStore(tmp_path / "claim-check", operations)
    service = ExternalArtifactPurgeService(
        _StaticInventory((CalendarDeletionTarget(foreign_tenant, canonical_event_id),)),
        calendar,
        vault,
        object_store,
    )
    await calendar.upsert_event(
        tenant_id,
        _entry(tenant_id, canonical_event_id, "Tenant-private event"),
    )
    await vault.store(_credential(tenant_id, Source.LUMA))
    await object_store.put(tenant_id, "temporal/opaque.payload", b"tenant-private bytes")

    result = await service.purge_tenant(tenant_id)

    assert result.status is ExternalArtifactPurgeStatus.PARTIAL
    assert result.failed_operations == 1
    assert result.calendar_targets == 0
    assert operations == []
    assert len(calendar.entries(tenant_id)) == 1
    assert await vault.get(tenant_id, Source.LUMA) is not None
    assert (tmp_path / "claim-check" / str(tenant_id)).exists()


async def test_duplicate_inventory_target_is_deleted_once_with_the_deterministic_calendar_id(
    tmp_path: Path,
) -> None:
    """Consumer-side dedupe prevents one malformed inventory from issuing duplicate deletes."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    operations: list[str] = []
    calendar = _RecordingCalendar(operations)
    vault = _RecordingVault(operations)
    object_store = _RecordingObjectStore(tmp_path / "claim-check", operations)
    target = CalendarDeletionTarget(tenant_id, canonical_event_id)
    service = ExternalArtifactPurgeService(
        _StaticInventory((target, target)), calendar, vault, object_store
    )
    await calendar.upsert_event(
        tenant_id,
        _entry(tenant_id, canonical_event_id, "Private event title"),
    )

    result = await service.purge_tenant(tenant_id)

    assert result.status is ExternalArtifactPurgeStatus.COMPLETED
    assert result.calendar_targets == 1
    assert result.calendar_deleted == 1
    assert calendar.delete_attempts == [calendar_event_id(tenant_id, canonical_event_id)]
    assert operations == ["calendar", "vault", "object_store"]


async def test_purge_replay_is_idempotent_and_result_contains_no_event_content(
    tmp_path: Path,
) -> None:
    """A replay safely repeats deterministic deletes and returns only status/counts (FR-10.5)."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    calendar = MockCalendar()
    inventory = MockTenantErasureInventory()
    vault = MockVault()
    object_store = MockFilesystemObjectStore(tmp_path / "claim-check")
    service = ExternalArtifactPurgeService(inventory, calendar, vault, object_store)
    inventory.seed_calendar_targets(tenant_id, [canonical_event_id])
    await calendar.upsert_event(
        tenant_id,
        _entry(tenant_id, canonical_event_id, "Private event title that must not leave the port"),
    )
    await vault.store(_credential(tenant_id, Source.LUMA))
    await object_store.put(tenant_id, "temporal/opaque.payload", b"private raw request text")

    first = await service.purge_tenant(tenant_id)
    replay = await service.purge_tenant(tenant_id)

    assert first == replay
    assert first.status is ExternalArtifactPurgeStatus.COMPLETED
    assert {field.name for field in fields(first)} == {
        "status",
        "calendar_targets",
        "calendar_deleted",
        "credential_vault_purged",
        "object_store_purged",
        "failed_operations",
    }
    serialized = repr(first)
    assert "Private event title" not in serialized
    assert "https://" not in serialized
    assert "private raw request text" not in serialized


class _FailingCalendar(MockCalendar):
    """Records every delete while failing one opaque deterministic calendar target."""

    def __init__(self, failing_event_id: str) -> None:
        super().__init__()
        self._failing_event_id = failing_event_id
        self.delete_attempts: list[str] = []

    async def delete_event(
        self,
        tenant_id: UUID,
        calendar_event_id: str,
        *,
        canonical_event_id: UUID | None = None,
    ) -> None:
        self.delete_attempts.append(calendar_event_id)
        if calendar_event_id == self._failing_event_id:
            raise RuntimeError("fixture calendar provider failure")
        await super().delete_event(
            tenant_id,
            calendar_event_id,
            canonical_event_id=canonical_event_id,
        )


async def test_calendar_failure_stops_before_irreversible_later_stages_and_is_not_complete(
    tmp_path: Path,
) -> None:
    """An unavailable calendar cannot be stranded by credential/object deletion (FR-10.5)."""
    tenant_id, first_event, second_event = uuid4(), uuid4(), uuid4()
    failing_event, second_event = sorted((first_event, second_event), key=str)
    failing_id = calendar_event_id(tenant_id, failing_event)
    calendar = _FailingCalendar(failing_id)
    inventory = MockTenantErasureInventory()
    vault = MockVault()
    object_store = MockFilesystemObjectStore(tmp_path / "claim-check")
    service = ExternalArtifactPurgeService(inventory, calendar, vault, object_store)
    inventory.seed_calendar_targets(tenant_id, [failing_event, second_event])
    for canonical_event_id in (failing_event, second_event):
        await calendar.upsert_event(
            tenant_id,
            _entry(tenant_id, canonical_event_id, "Private event title"),
        )
    await vault.store(_credential(tenant_id, Source.LUMA))
    await object_store.put(tenant_id, "temporal/opaque.payload", b"private bytes")

    result = await service.purge_tenant(tenant_id)

    assert result.status is ExternalArtifactPurgeStatus.PARTIAL
    assert result.calendar_targets == 2
    assert result.calendar_deleted == 0
    assert result.credential_vault_purged == 0
    assert result.object_store_purged == 0
    assert result.failed_operations == 1
    assert calendar.delete_attempts == [failing_id]
    assert await vault.get(tenant_id, Source.LUMA) is not None
    assert (tmp_path / "claim-check" / str(tenant_id)).exists()


class _FailingObjectStore(MockFilesystemObjectStore):
    """A post-calendar outage seam proving the result remains aggregate-only and retryable."""

    async def delete_tenant(self, tenant_id: UUID) -> None:
        del tenant_id
        raise RuntimeError("fixture object-store failure")


class _FailingVault(MockVault):
    """A post-calendar outage seam proving object storage waits for credential purge."""

    def __init__(self, operations: list[str]) -> None:
        super().__init__()
        self._operations = operations

    async def delete_tenant(self, tenant_id: UUID) -> None:
        del tenant_id
        self._operations.append("vault")
        raise RuntimeError("fixture vault failure")


async def test_vault_failure_is_partial_and_stops_before_object_store_deletion(
    tmp_path: Path,
) -> None:
    """The staged order never lets a failed credential purge advance to claim-store deletion."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    operations: list[str] = []
    calendar = _RecordingCalendar(operations)
    inventory = MockTenantErasureInventory()
    vault = _FailingVault(operations)
    object_store = _RecordingObjectStore(tmp_path / "claim-check", operations)
    service = ExternalArtifactPurgeService(inventory, calendar, vault, object_store)
    inventory.seed_calendar_targets(tenant_id, [canonical_event_id])
    await calendar.upsert_event(
        tenant_id,
        _entry(tenant_id, canonical_event_id, "Private event title"),
    )
    await vault.store(_credential(tenant_id, Source.LUMA))
    await object_store.put(tenant_id, "temporal/opaque.payload", b"private bytes")

    result = await service.purge_tenant(tenant_id)

    assert result.status is ExternalArtifactPurgeStatus.PARTIAL
    assert result.calendar_targets == 1
    assert result.calendar_deleted == 1
    assert result.credential_vault_purged == 0
    assert result.object_store_purged == 0
    assert result.failed_operations == 1
    assert operations == ["calendar", "vault"]
    assert calendar.entries(tenant_id) == []
    assert await vault.get(tenant_id, Source.LUMA) is not None
    assert (tmp_path / "claim-check" / str(tenant_id)).exists()


async def test_later_object_store_failure_is_partial_after_calendar_and_vault_succeed(
    tmp_path: Path,
) -> None:
    """A later failed stage never claims complete and leaves only that stage for a safe replay."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    calendar = MockCalendar()
    inventory = MockTenantErasureInventory()
    vault = MockVault()
    object_store = _FailingObjectStore(tmp_path / "claim-check")
    service = ExternalArtifactPurgeService(inventory, calendar, vault, object_store)
    inventory.seed_calendar_targets(tenant_id, [canonical_event_id])
    await calendar.upsert_event(
        tenant_id,
        _entry(tenant_id, canonical_event_id, "Private event title"),
    )
    await vault.store(_credential(tenant_id, Source.LUMA))
    await object_store.put(tenant_id, "temporal/opaque.payload", b"private bytes")

    result = await service.purge_tenant(tenant_id)

    assert result.status is ExternalArtifactPurgeStatus.PARTIAL
    assert result.calendar_targets == 1
    assert result.calendar_deleted == 1
    assert result.credential_vault_purged == 1
    assert result.object_store_purged == 0
    assert result.failed_operations == 1
    assert calendar.entries(tenant_id) == []
    assert await vault.get(tenant_id, Source.LUMA) is None
    assert (tmp_path / "claim-check" / str(tenant_id)).exists()
