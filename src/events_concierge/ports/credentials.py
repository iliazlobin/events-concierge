"""Credential vault + the non-LLM injection broker (ADR-006). The broker is the ONLY holder of the
decrypt capability; it domain-pins BEFORE decrypting and zeroizes after typing. Mocked in the
foundation; the real Rust broker + KMS-envelope vault land when AWS is provisioned."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from ..domain.credentials import Credential
from ..domain.enums import Source


class CredentialVault(Protocol):
    async def store(self, credential: Credential) -> None:
        """Encrypt + persist under a per-tenant DEK with {tenant, credential_type} context (FR-2.2)."""
        ...

    async def get(self, tenant_id: UUID, source: Source) -> Credential | None: ...

    async def revoke(self, tenant_id: UUID, source: Source) -> None:
        """Single-source disconnect: revoke token/cookie, leave other sources intact (FR-2.11)."""
        ...

    async def delete_tenant(self, tenant_id: UUID) -> None:
        """Purge every tenant-scoped credential and ephemeral RelayInbox secret (FR-10.5, ADR-006/011)."""
        ...


class InjectionBroker(Protocol):
    async def fill(
        self, session_ref: str, credential_id: UUID, field_ref: str, bound_origin: str
    ) -> bool:
        """Pin the live origin against bound_origin, decrypt into a locked buffer, type it, zeroize.

        Returns True on success; a domain-pin mismatch aborts BEFORE any decrypt (zero secret
        released) and returns False (FR-2.5, AC-7)."""
        ...

    async def fill_secret_reference(
        self, session_ref: str, secret_ref: UUID, field_ref: str, bound_origin: str
    ) -> bool:
        """Redeem one opaque RelayInbox reference and fill it after domain-pinning (ADR-011).

        ``secret_ref`` is the sole secret-related argument.  The broker resolves the tenant-scoped,
        encrypted, consumed-once value internally, types it into the pinned browser field, then
        zeroizes it.  No OTP or magic-link plaintext may enter this port (FR-2.6, FR-5.8).
        """
        ...
