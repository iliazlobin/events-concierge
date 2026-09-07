"""Tenant-issued API keys.

The product stores a SHA-256 digest and never the secret, so this is an index rather than a
credential store: disclosing the table yields nothing usable. The plaintext exists for exactly one
response and cannot be recovered afterwards -- which is why issuing returns it separately from the
record that describes it.

A plain digest is the right construction *here* specifically because the secret is a 256-bit value
this server generated. Password hashing exists to slow attacks on low-entropy human-chosen inputs;
against a random 32-byte token, brute force is already infeasible, and a slow KDF on the
authentication path would only add latency and a denial-of-service lever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    """One key as the owner may see it. Never carries the secret."""

    key_id: UUID
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def active(self) -> bool:
        """Whether this key would still authenticate."""
        return self.revoked_at is None


@dataclass(frozen=True, slots=True)
class IssuedApiKey:
    """A freshly minted key plus the one-time secret the caller must copy now."""

    record: ApiKeyRecord
    secret: str


class ApiKeyRepository(Protocol):
    """Issue, list, revoke and resolve tenant API keys."""

    async def list_keys(self, tenant_id: UUID) -> tuple[ApiKeyRecord, ...]:
        """Return the tenant's keys, newest first, without any secret material."""
        ...

    async def issue(
        self, tenant_id: UUID, key_id: UUID, name: str, key_prefix: str, key_hash: str
    ) -> ApiKeyRecord:
        """Persist one new key digest and return its non-secret record."""
        ...

    async def revoke(self, tenant_id: UUID, key_id: UUID) -> ApiKeyRecord | None:
        """Mark one key revoked. Returns ``None`` when the tenant has no such key.

        Revocation is a timestamp rather than a delete so an operator can still attribute past
        activity to a key that no longer works.
        """
        ...
