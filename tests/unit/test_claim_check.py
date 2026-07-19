"""Offline claim-check tests for oversized Temporal payloads (FR-8.5, AC-58, ADR-010)."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
    StorageDriverWorkflowInfo,
)

from events_concierge.adapters.mock.object_store import MockFilesystemObjectStore
from events_concierge.domain.ids import registration_workflow_id, request_workflow_id
from events_concierge.ports.object_store import ObjectStoreNotFoundError
from events_concierge.workflows.claim_check import (
    ClaimCheckStorageDriver,
    build_claim_check_data_converter,
)


def _workflow_context(workflow_id: str) -> StorageDriverStoreContext:
    return StorageDriverStoreContext(
        target=StorageDriverWorkflowInfo(
            namespace="default",
            id=workflow_id,
            run_id=str(uuid4()),
            type="RegistrationWorkflow",
        )
    )


async def test_converter_offloads_large_value_and_a_second_instance_restores_it(
    tmp_path: Path,
) -> None:
    """A >2 MiB value becomes a small claim and survives the API/worker process boundary (AC-58)."""
    tenant_id, event_id = uuid4(), uuid4()
    root = tmp_path / "claim-check"
    writer = build_claim_check_data_converter(
        MockFilesystemObjectStore(root),
        threshold_bytes=256 * 1024,
    )._with_store_context(_workflow_context(registration_workflow_id(tenant_id, event_id)))
    reader = build_claim_check_data_converter(
        MockFilesystemObjectStore(root),
        threshold_bytes=256 * 1024,
    )
    large_value = "x" * (2 * 1024 * 1024 + 1)

    [claim] = await writer.encode([large_value])
    [restored] = await reader.decode([claim], [str])

    assert claim.external_payloads[0].size_bytes > 2 * 1024 * 1024
    assert claim.ByteSize() < 256 * 1024
    assert large_value not in claim.data.decode("utf-8", errors="ignore")
    assert restored == large_value


@pytest.mark.parametrize("parent", [True, False])
async def test_driver_accepts_only_the_two_deterministic_workflow_identity_shapes(
    tmp_path: Path,
    parent: bool,
) -> None:
    """The tenant prefix is taken only from validated parent or child workflow ids (FR-1.3)."""
    tenant_id, trailing_id = uuid4(), uuid4()
    workflow_id = (
        request_workflow_id(tenant_id, trailing_id)
        if parent
        else registration_workflow_id(tenant_id, trailing_id)
    )
    driver = ClaimCheckStorageDriver(MockFilesystemObjectStore(tmp_path / "store"))
    payload = Payload(data=b"opaque payload")

    [claim] = await driver.store(_workflow_context(workflow_id), [payload])

    assert claim.claim_data["tenant_id"] == str(tenant_id)
    assert "opaque payload" not in str(dict(claim.claim_data))


async def test_driver_rejects_unscoped_workflow_id_and_detects_tampered_bytes(
    tmp_path: Path,
) -> None:
    """Malformed identities fail closed and a changed object cannot be deserialized (FR-1.4, FR-8.5)."""
    tenant_id, event_id = uuid4(), uuid4()
    root = tmp_path / "store"
    store = MockFilesystemObjectStore(root)
    driver = ClaimCheckStorageDriver(store)
    payload = Payload(data=b"payload to protect")

    with pytest.raises(ValueError, match="deterministic workflow id"):
        await driver.store(_workflow_context("unknown-workflow"), [payload])

    [claim] = await driver.store(
        _workflow_context(registration_workflow_id(tenant_id, event_id)),
        [payload],
    )
    object_path = root / str(tenant_id) / claim.claim_data["key"]
    object_path.write_bytes(b"tampered")

    with pytest.raises(ValueError, match="integrity"):
        await driver.retrieve(StorageDriverRetrieveContext(), [claim])

    malformed = StorageDriverClaim(claim_data={"tenant_id": str(tenant_id), "key": "bad"})
    with pytest.raises(ValueError, match="incomplete"):
        await driver.retrieve(StorageDriverRetrieveContext(), [malformed])


async def test_filesystem_store_cannot_cross_tenant_prefixes_and_purges_one_tenant(
    tmp_path: Path,
) -> None:
    """A future erasure call removes only its tenant's opaque payloads (FR-1.3, FR-10.5)."""
    first_tenant, second_tenant = uuid4(), uuid4()
    store = MockFilesystemObjectStore(tmp_path / "store")
    key = "temporal/opaque.payload"
    await store.put(first_tenant, key, b"first tenant only")
    await store.put(second_tenant, key, b"second tenant survives")

    with pytest.raises(ObjectStoreNotFoundError):
        await store.get(second_tenant, "temporal/unknown.payload")
    assert await store.get(first_tenant, key) == b"first tenant only"

    await store.delete_tenant(first_tenant)

    with pytest.raises(ObjectStoreNotFoundError):
        await store.get(first_tenant, key)
    assert await store.get(second_tenant, key) == b"second tenant survives"
