"""Oversized public catalog payloads survive replay without tenant storage authority."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock
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
from events_concierge.config import Settings
from events_concierge.domain.ids import (
    catalog_paged_refresh_workflow_id,
    catalog_refresh_workflow_id,
    request_workflow_id,
)
from events_concierge.workflows import temporal_client
from events_concierge.workflows.catalog_claim_check import (
    CatalogClaimCheckStorageDriver,
    build_catalog_claim_check_data_converter,
)
from events_concierge.workflows.claim_check import build_claim_check_data_converter


def _context(workflow_id: str, workflow_type: str | None = None) -> StorageDriverStoreContext:
    return StorageDriverStoreContext(
        target=StorageDriverWorkflowInfo(
            namespace="catalog",
            id=workflow_id,
            type=workflow_type,
        )
    )


@pytest.mark.parametrize("paged", [False, True])
async def test_large_catalog_payload_restores_across_clients_but_not_tenant_converter(
    tmp_path: Path,
    paged: bool,
) -> None:
    workflow_id = (
        catalog_paged_refresh_workflow_id("public-source", "receipt-run")
        if paged
        else catalog_refresh_workflow_id("public-source", "receipt-run")
    )
    root = tmp_path / "catalog"
    writer = build_catalog_claim_check_data_converter(
        MockFilesystemObjectStore(root),
        256 * 1024,
    )._with_store_context(
        _context(
            workflow_id,
            "CatalogPagedRefreshWorkflow" if paged else "CatalogRefreshWorkflow",
        )
    )
    value = "public catalog result " * 110000
    [claim] = await writer.encode([value])
    assert claim.external_payloads[0].size_bytes > 2 * 1024 * 1024
    assert claim.ByteSize() < 256 * 1024
    reader = build_catalog_claim_check_data_converter(MockFilesystemObjectStore(root), 256 * 1024)
    assert await reader.decode([claim], [str]) == [value]
    tenant_reader = build_claim_check_data_converter(MockFilesystemObjectStore(root), 256 * 1024)
    with pytest.raises(ValueError):
        await tenant_reader.decode([claim], [str])


@pytest.mark.parametrize(
    "workflow_id,workflow_type",
    [
        (request_workflow_id(uuid4(), uuid4()), None),
        ("catalog:../tenant:" + "a" * 64, None),
        ("catalog:public-source:" + "a" * 63, None),
        (catalog_refresh_workflow_id("public-source", "run"), "RegistrationWorkflow"),
    ],
)
async def test_catalog_driver_rejects_tenant_and_malformed_workflow_contexts(
    tmp_path: Path,
    workflow_id: str,
    workflow_type: str | None,
) -> None:
    driver = CatalogClaimCheckStorageDriver(MockFilesystemObjectStore(tmp_path))
    with pytest.raises(ValueError, match="catalog workflow id"):
        await driver.store(_context(workflow_id, workflow_type), [Payload(data=b"value")])
    assert not await asyncio.to_thread(lambda: list(tmp_path.rglob("*.payload")))


async def test_catalog_claim_cannot_select_a_tenant_or_arbitrary_object_and_verifies_integrity(
    tmp_path: Path,
) -> None:
    driver = CatalogClaimCheckStorageDriver(MockFilesystemObjectStore(tmp_path))
    [claim] = await driver.store(
        _context(catalog_refresh_workflow_id("public-source", "run")),
        [Payload(data=b"public")],
    )
    assert set(claim.claim_data) == {"scope", "key", "sha256"}
    for replacement in (
        {"tenant_id": str(uuid4())},
        {"scope": "tenant-v1"},
        {"key": "../tenant/secret.payload"},
        {"sha256": "a" * 63},
    ):
        forged = StorageDriverClaim(claim_data=dict(claim.claim_data) | replacement)
        with pytest.raises(ValueError, match="reference"):
            await driver.retrieve(StorageDriverRetrieveContext(), [forged])
    [object_path] = await asyncio.to_thread(lambda: list(tmp_path.rglob("*.payload")))
    await asyncio.to_thread(object_path.write_bytes, b"tampered")
    with pytest.raises(ValueError, match="integrity"):
        await driver.retrieve(StorageDriverRetrieveContext(), [claim])


async def test_catalog_converter_cannot_decode_a_tenant_claim_even_with_same_storage(
    tmp_path: Path,
) -> None:
    tenant = build_claim_check_data_converter(MockFilesystemObjectStore(tmp_path), 1)
    [claim] = await tenant._with_store_context(
        _context(request_workflow_id(uuid4(), uuid4()))
    ).encode(["tenant value"])
    catalog = build_catalog_claim_check_data_converter(MockFilesystemObjectStore(tmp_path), 1)
    with pytest.raises(ValueError):
        await catalog.decode([claim], [str])


async def test_explicit_catalog_temporal_client_has_no_tenant_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def forbidden_authority(*args: object, **kwargs: object) -> None:
        raise AssertionError("catalog client must not construct tenant authority")

    monkeypatch.setattr(temporal_client, "PostgresTenantEffectAuthority", forbidden_authority)
    connect = AsyncMock()
    monkeypatch.setattr(temporal_client.Client, "connect", connect)
    await temporal_client.connect_temporal(
        Settings(_env_file=None, claim_check_threshold_bytes=1),
        MockFilesystemObjectStore(tmp_path),
        catalog_only=True,
    )
    converter = connect.call_args.kwargs["data_converter"]
    [claim] = await converter._with_store_context(
        _context(
            catalog_refresh_workflow_id("public-source", "run"),
        )
    ).encode(["public value"])
    assert await converter.decode([claim], [str]) == ["public value"]
