"""In-memory CredentialVault, RelayInbox, and InjectionBroker mocks (ADR-006/011).

The real vault is a KMS-envelope store and the broker a domain-pinning Rust process; the mock keeps
the interface faithful -- per-tenant/source credential store and a domain-pin gate on fill -- so the
workflows exercise the same contract with no AWS dependency."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from ...domain.credentials import Credential
from ...domain.enums import Source
from ...ports.email import (
    RelaySecretReference,
    RelaySecretReferenceRedemptionPort,
    RelaySecretReferenceStorePort,
    RelaySecretReservation,
)


def _utc_now() -> datetime:
    """Return aware UTC for the fixture broker's redeemed-once check."""
    return datetime.now(UTC)


class MockVault:
    """Process-local CredentialVault. Keyed by (tenant_id, source); holds Credential ciphertext only."""

    def __init__(self) -> None:
        self._store: dict[tuple[UUID, Source], Credential] = {}

    async def store(self, credential: Credential) -> None:
        """Persist under the per-tenant/source slot (FR-2.2). Mock keeps ciphertext as-is."""
        self._store[(credential.tenant_id, credential.source)] = credential

    async def get(self, tenant_id: UUID, source: Source) -> Credential | None:
        return self._store.get((tenant_id, source))

    async def revoke(self, tenant_id: UUID, source: Source) -> None:
        """Single-source disconnect: drop this source's slot, leave others intact (FR-2.11)."""
        self._store.pop((tenant_id, source), None)

    async def delete_tenant(self, tenant_id: UUID) -> None:
        """Purge every tenant-scoped ciphertext slot for the offline FR-10.5 erasure seam."""
        for key in tuple(self._store):
            if key[0] == tenant_id:
                del self._store[key]


@dataclass(slots=True)
class _RelaySecretRecord:
    """Internal metadata-only fixture record; no OTP, link, body, or ciphertext is modelled."""

    delivery_id: UUID
    reference: RelaySecretReference
    consumed: bool = False


class MockRelaySecretRegistry(RelaySecretReferenceStorePort, RelaySecretReferenceRedemptionPort):
    """Metadata-only RelayInbox claim-check registry for local fixtures (ADR-011).

    The real implementation will keep encrypted values behind KMS.  This offline double
    intentionally cannot hold or reveal an OTP/magic link: it can only mint an opaque UUID,
    deduplicate a delivery, expire it, and mark it consumed once.
    """

    def __init__(self) -> None:
        self._by_delivery: dict[UUID, _RelaySecretRecord] = {}
        self._by_reference: dict[UUID, _RelaySecretRecord] = {}

    def reserve_secret_reference(
        self,
        delivery_id: UUID,
        tenant_id: UUID,
        source: Source,
        sender_domain: str,
        expires_at: datetime,
    ) -> RelaySecretReservation | None:
        """Mint one opaque reference per delivery, returning it on an exact live replay."""
        if (
            not isinstance(delivery_id, UUID)
            or not isinstance(tenant_id, UUID)
            or not isinstance(source, Source)
            or not sender_domain.strip()
            or not _is_aware(expires_at)
        ):
            return None

        existing = self._by_delivery.get(delivery_id)
        if existing is not None:
            reference = existing.reference
            if (
                existing.consumed
                or reference.tenant_id != tenant_id
                or reference.source is not source
                or reference.sender_domain != sender_domain
                or reference.expires_at != expires_at
            ):
                return None
            return RelaySecretReservation(reference=reference, replayed=True)

        reference = RelaySecretReference(
            tenant_id=tenant_id,
            source=source,
            secret_ref=uuid4(),
            sender_domain=sender_domain,
            expires_at=expires_at,
        )
        record = _RelaySecretRecord(delivery_id=delivery_id, reference=reference)
        self._by_delivery[delivery_id] = record
        self._by_reference[reference.secret_ref] = record
        return RelaySecretReservation(reference=reference, replayed=False)

    def discard_secret_reference(self, secret_ref: UUID) -> None:
        """Forget only a never-consumed unpublished fixture capability."""
        record = self._by_reference.get(secret_ref)
        if record is None or record.consumed:
            return
        del self._by_reference[secret_ref]
        del self._by_delivery[record.delivery_id]

    async def consume_secret_reference(self, secret_ref: UUID, now: datetime) -> bool:
        """Consume one live opaque reference without returning a plaintext secret (ADR-011)."""
        if not isinstance(secret_ref, UUID) or not _is_aware(now):
            return False
        record = self._by_reference.get(secret_ref)
        if record is None or record.consumed or record.reference.expires_at <= now:
            return False
        record.consumed = True
        return True


class MockInjectionBroker:
    """Process-local InjectionBroker. Domain-pins BEFORE any (mock) decrypt (FR-2.5, AC-7)."""

    def __init__(
        self,
        *,
        secret_registry: RelaySecretReferenceRedemptionPort | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.filled_secret_refs: list[UUID] = []
        self._secret_registry = secret_registry
        self._now = now or _utc_now

    async def fill(
        self, session_ref: str, credential_id: UUID, field_ref: str, bound_origin: str
    ) -> bool:
        """Abort BEFORE decrypt on an empty/unpinnable bound_origin; otherwise accept the fill.

        The mock keeps the domain-pin gate faithful: an empty bound_origin cannot be matched against
        the live origin, so the fill is refused with zero secret released and returns False."""
        return bool(bound_origin)

    async def fill_secret_reference(
        self, session_ref: str, secret_ref: UUID, field_ref: str, bound_origin: str
    ) -> bool:
        """Record only a redeemed opaque reference after the domain-pin gate (ADR-011).

        The offline double intentionally has no secret store and never accepts plaintext.  When a
        metadata-only registry is supplied, it performs the consumed-once check only *after* the
        origin gate passes.  Recording the UUID gives tests an observable capability handoff without
        modelling decryption.
        """
        del session_ref, field_ref
        if not bound_origin.strip():
            return False
        if (
            self._secret_registry is not None
            and not await self._secret_registry.consume_secret_reference(secret_ref, self._now())
        ):
            return False
        self.filled_secret_refs.append(secret_ref)
        return True


def _is_aware(value: datetime) -> bool:
    """Refuse a malformed fixture time rather than weakening a TTL boundary."""
    return value.tzinfo is not None and value.utcoffset() is not None
