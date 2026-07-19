"""Temporal external-storage claim-check converter (FR-8.5, AC-58, ADR-003/010).

Temporal serializes values before this boundary.  Payloads at or above the configured threshold are
written under a tenant-scoped opaque key and workflow history retains only a short, integrity-checked
reference.  This module intentionally has no concrete-storage import: composition supplies a port.
"""

from __future__ import annotations

import hashlib
import hmac
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

_DRIVER_NAME = "events-concierge.claim-check.v1"
_CLAIM_PREFIX = "temporal"
_DIGEST_LENGTH = 64
_PARENT_WORKFLOW_PARTS = 3
_CHILD_WORKFLOW_PARTS = 2


class ClaimCheckStorageDriver(StorageDriver):
    """Store serialized Temporal payloads behind opaque, tenant-scoped references.

    Tenant identity is inferred only while storing, from one of the deterministic workflow-id shapes
    that ADR-003 owns.  Retrieval has no SDK target context, so it uses the already-validated tenant
    UUID carried by the immutable history claim and verifies the bytes' SHA-256 before deserializing.
    """

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
        """Persist each serialized payload once under the tenant's content-addressed prefix."""
        tenant_id = _tenant_id_from_store_context(context)
        claims: list[StorageDriverClaim] = []
        for payload in payloads:
            serialized = payload.SerializeToString()
            digest = hashlib.sha256(serialized).hexdigest()
            key = _claim_key(digest)
            await self._object_store.put(tenant_id, key, serialized)
            claims.append(
                StorageDriverClaim(
                    claim_data={
                        "tenant_id": str(tenant_id),
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
        """Read and integrity-check each opaque reference before restoring its Temporal payload."""
        del context
        restored: list[Payload] = []
        for claim in claims:
            tenant_id, key, expected_digest = _claim_parts(claim)
            serialized = await self._object_store.get(tenant_id, key)
            actual_digest = hashlib.sha256(serialized).hexdigest()
            if not hmac.compare_digest(actual_digest, expected_digest):
                raise ValueError("claim-check payload failed integrity verification")
            payload = Payload()
            payload.ParseFromString(serialized)
            restored.append(payload)
        return restored


def build_claim_check_data_converter(
    object_store: ObjectStorePort,
    threshold_bytes: int,
) -> DataConverter:
    """Build the shared converter; callers must pass it to every Temporal client (ADR-010)."""
    if threshold_bytes < 0:
        raise ValueError("claim-check threshold must be non-negative")
    return DataConverter(
        external_storage=ExternalStorage(
            drivers=[ClaimCheckStorageDriver(object_store)],
            payload_size_threshold=threshold_bytes,
        )
    )


def _tenant_id_from_store_context(context: StorageDriverStoreContext) -> UUID:
    target = context.target
    if not isinstance(target, StorageDriverWorkflowInfo) or target.id is None:
        raise ValueError("claim-check storage requires a deterministic workflow id")
    return _tenant_id_from_workflow_id(target.id)


def _tenant_id_from_workflow_id(workflow_id: str) -> UUID:
    parts = workflow_id.split(":")
    if len(parts) == _PARENT_WORKFLOW_PARTS and parts[0] == "req":
        tenant_text, trailing_id = parts[1], parts[2]
    elif len(parts) == _CHILD_WORKFLOW_PARTS:
        tenant_text, trailing_id = parts
    else:
        raise ValueError("claim-check storage requires a deterministic workflow id")
    tenant_id = _canonical_uuid(tenant_text)
    _canonical_uuid(trailing_id)
    return tenant_id


def _canonical_uuid(value: str) -> UUID:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError(
            "claim-check storage requires canonical UUID workflow identifiers"
        ) from error
    if str(parsed) != value:
        raise ValueError("claim-check storage requires canonical UUID workflow identifiers")
    return parsed


def _claim_key(digest: str) -> str:
    return f"{_CLAIM_PREFIX}/{digest}.payload"


def _claim_parts(claim: StorageDriverClaim) -> tuple[UUID, str, str]:
    try:
        tenant_text = claim.claim_data["tenant_id"]
        key = claim.claim_data["key"]
        digest = claim.claim_data["sha256"]
    except KeyError as error:
        raise ValueError("claim-check reference is incomplete") from error
    tenant_id = _canonical_uuid(tenant_text)
    if not _is_sha256_digest(digest) or key != _claim_key(digest):
        raise ValueError("claim-check reference is malformed")
    return tenant_id, key, digest


def _is_sha256_digest(value: str) -> bool:
    return len(value) == _DIGEST_LENGTH and all(
        character in "0123456789abcdef" for character in value
    )
