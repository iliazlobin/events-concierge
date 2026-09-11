"""Claim-check storage for public catalog workflows, isolated from tenant payload storage.

Only the explicit catalog runtime installs this driver. Its object store has a separate filesystem
root or GCS prefix and narrowly scoped IAM. The UUID below adapts the shared object-store interface
to one fixed system scope; no claim or workflow can select a tenant UUID.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Sequence
from uuid import UUID

from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    DataConverter,
    ExternalStorage,
    StorageDriver,
    StorageDriverClaim,
    StorageDriverRetrieveContext,
    StorageDriverStoreContext,
    StorageDriverWorkflowInfo,
)

from ..ports.object_store import ObjectStorePort

_DRIVER_NAME = "events-concierge.catalog-claim-check.v1"
_SCOPE = "catalog-v1"
_STORAGE_SCOPE_ID = UUID("ba0b3577-3024-5e8b-91d2-6858ee129179")
_WORKFLOW_ID = re.compile(r"^(catalog|catalog-paged):[a-z0-9][a-z0-9-]{1,79}:[0-9a-f]{64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_WORKFLOW_TYPES = {
    "catalog": "CatalogRefreshWorkflow",
    "catalog-paged": "CatalogPagedRefreshWorkflow",
}


class CatalogClaimCheckStorageDriver(StorageDriver):
    """Store catalog payloads without a tenant authority or tenant-selectable references."""

    def __init__(self, object_store: ObjectStorePort) -> None:
        self._object_store = object_store

    def name(self) -> str:
        return _DRIVER_NAME

    def type(self) -> str:
        return _DRIVER_NAME

    async def store(
        self,
        context: StorageDriverStoreContext,
        payloads: Sequence[Payload],
    ) -> list[StorageDriverClaim]:
        target = context.target
        if not isinstance(target, StorageDriverWorkflowInfo) or target.id is None:
            raise ValueError("catalog claim-check requires a deterministic catalog workflow id")
        matched = _WORKFLOW_ID.fullmatch(target.id)
        if matched is None or (
            target.type is not None and target.type != _WORKFLOW_TYPES[matched[1]]
        ):
            raise ValueError("catalog claim-check requires a deterministic catalog workflow id")
        claims: list[StorageDriverClaim] = []
        for payload in payloads:
            serialized = payload.SerializeToString()
            digest = hashlib.sha256(serialized).hexdigest()
            key = _claim_key(digest)
            await self._object_store.put(_STORAGE_SCOPE_ID, key, serialized)
            claims.append(
                StorageDriverClaim(
                    claim_data={
                        "scope": _SCOPE,
                        "key": key,
                        "sha256": digest,
                    }
                )
            )
        return claims

    async def retrieve(
        self,
        context: StorageDriverRetrieveContext,
        claims: Sequence[StorageDriverClaim],
    ) -> list[Payload]:
        # Temporal retrieval has no workflow target. Only this driver's fixed public catalog scope
        # is readable; metadata cannot switch roots or tenants, including for forged references.
        del context
        restored: list[Payload] = []
        for claim in claims:
            parts = claim.claim_data
            if set(parts) != {"scope", "key", "sha256"} or parts["scope"] != _SCOPE:
                raise ValueError("catalog claim-check reference has an invalid scope")
            digest = parts["sha256"]
            key = parts["key"]
            if not _DIGEST.fullmatch(digest) or key != _claim_key(digest):
                raise ValueError("catalog claim-check reference is malformed")
            serialized = await self._object_store.get(_STORAGE_SCOPE_ID, key)
            if not hmac.compare_digest(hashlib.sha256(serialized).hexdigest(), digest):
                raise ValueError("catalog claim-check payload failed integrity verification")
            payload = Payload()
            payload.ParseFromString(serialized)
            restored.append(payload)
        return restored


def build_catalog_claim_check_data_converter(
    object_store: ObjectStorePort,
    threshold_bytes: int,
) -> DataConverter:
    """Install exactly one catalog driver, with no fallback to tenant claim retrieval."""
    if threshold_bytes < 0:
        raise ValueError("claim-check threshold must be non-negative")
    return DataConverter(
        external_storage=ExternalStorage(
            drivers=[CatalogClaimCheckStorageDriver(object_store)],
            payload_size_threshold=threshold_bytes,
        )
    )


def _claim_key(digest: str) -> str:
    return f"temporal/{digest}.payload"
