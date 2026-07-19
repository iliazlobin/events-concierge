"""Tenant, stored third-party credential (ciphertext only in the domain), and consent record."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from .enums import CredentialKind, CredentialStatus, Source


@dataclass(slots=True)
class Tenant:
    tenant_id: UUID
    oidc_subject: str
    notify_email: str
    relay_inbox: str  # alice@u.<domain> -- inbound-only OTP/confirmation channel (FR-1.5)


@dataclass(slots=True)
class Credential:
    """A stored per-user third-party secret. The domain never holds plaintext -- only the ciphertext
    handle and its binding metadata; decryption lives behind the CredentialVault port (FR-2.2)."""

    credential_id: UUID
    tenant_id: UUID
    source: Source
    kind: CredentialKind
    ciphertext: bytes
    bound_origin: str
    status: CredentialStatus = CredentialStatus.ACTIVE
    identity_hash: str | None = None  # egress-IP slot + fingerprint at capture (FR-2.4)


@dataclass(frozen=True, slots=True)
class ConsentRecord:
    """First-class per-source, per-scope, timestamped delegation evidence (FR-2.9)."""

    consent_id: UUID
    tenant_id: UUID
    source: Source
    scope: str
    granted_at: datetime
    metadata: dict[str, str] = field(default_factory=dict)
