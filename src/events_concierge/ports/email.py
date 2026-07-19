"""RelayInbox references for short-lived login secrets (FR-5.7/5.8, ADR-011).

The ingestion boundary deliberately exposes only an opaque, consumed-once secret reference.  The
OTP or magic-link remains encrypted behind the InjectionBroker and never crosses an application,
workflow, prompt, tool, or logging boundary in plaintext (FR-2.6, ADR-011).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from ..domain.enums import Source


@dataclass(frozen=True, slots=True)
class RelaySecretReference:
    """One allowlisted RelayInbox secret capability, never its plaintext (ADR-011).

    ``secret_ref`` identifies a tenant-scoped, KMS-encrypted, short-TTL and consumed-once value
    held by the relay secret store.  The InjectionBroker is the only component permitted to redeem
    it.  Sender allowlisting and deterministic extraction happen before this port is called.
    """

    tenant_id: UUID
    source: Source
    secret_ref: UUID
    sender_domain: str
    expires_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.tenant_id, UUID):
            raise ValueError("relay secret reference tenant_id must be a UUID")
        if not isinstance(self.source, Source):
            raise ValueError("relay secret reference source must be a Source")
        if not isinstance(self.secret_ref, UUID):
            raise ValueError("relay secret reference secret_ref must be a UUID")
        if not self.sender_domain.strip():
            raise ValueError("relay secret reference sender_domain must not be empty")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("relay secret reference expires_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RelaySecretReservation:
    """Result of atomically reserving an opaque RelayInbox capability (ADR-011).

    ``replayed`` distinguishes a redelivered inbound message from a newly minted reference without
    carrying the extracted OTP, magic link, raw message, or any other secret material across the
    port.  A relay implementation must make a delivery idempotent before publishing this result.
    """

    reference: RelaySecretReference
    replayed: bool

    def __post_init__(self) -> None:
        if not isinstance(self.reference, RelaySecretReference):
            raise ValueError("relay secret reservation reference must be a RelaySecretReference")


class RelaySecretReferenceStorePort(Protocol):
    """Claim-check storage boundary for a fixture or real RelayInbox (ADR-011).

    Its arguments deliberately contain only delivery and capability metadata.  The real store owns
    encrypted plaintext internally; the fixture implementation models no plaintext at all.
    """

    def reserve_secret_reference(
        self,
        delivery_id: UUID,
        tenant_id: UUID,
        source: Source,
        sender_domain: str,
        expires_at: datetime,
    ) -> RelaySecretReservation | None:
        """Mint once per delivery, or return its existing opaque reservation on redelivery."""
        ...

    def discard_secret_reference(self, secret_ref: UUID) -> None:
        """Remove an unpublished, unconsumed capability after a failed handoff."""
        ...


class RelaySecretReferenceRedemptionPort(Protocol):
    """Consumed-once opaque-reference redemption gate used only by the broker (ADR-011)."""

    async def consume_secret_reference(self, secret_ref: UUID, now: datetime) -> bool:
        """Atomically consume a live capability without returning its plaintext."""
        ...


class RelaySecretReferencePublisherPort(Protocol):
    """Opaque handoff channel from deterministic ingress to a bounded fixture consumer."""

    async def publish_secret_reference(self, reference: RelaySecretReference) -> bool:
        """Publish a reference at least once; return False when the scoped slot is unavailable."""
        ...


class EmailIngestionPort(Protocol):
    async def await_secret_reference(
        self, tenant_id: UUID, source: Source, timeout_s: float
    ) -> RelaySecretReference | None:
        """Wait for an allowlisted opaque RelayInbox secret reference.

        Returns None on timeout, so the workflow can route to handoff (FR-8.4).  The returned
        capability contains no OTP or magic-link plaintext (FR-2.6, FR-5.8, ADR-011).
        """
        ...
