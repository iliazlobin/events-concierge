"""Consumer identity and the exact legal documents accepted when opening an account."""

from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID


@dataclass(frozen=True, slots=True)
class LegalPolicy:
    terms_version: str
    terms_url: str
    privacy_version: str
    privacy_url: str


@dataclass(frozen=True, slots=True)
class VerifiedConsumerIdentity:
    project_id: str
    uid: str
    provider: str
    email: str
    authenticated_at: int

    @property
    def subject(self) -> str:
        return f"identity-platform:v1:{self.project_id}:{self.uid}"

    @property
    def tenant_id(self) -> UUID:
        # Stable across retries and erasure. An erased UID cannot escape its retained tombstone
        # by asking for another random tenant UUID. Email never participates in account binding.
        digest = bytearray(
            sha256(f"https://events-concierge/accounts/v1/{self.subject}".encode()).digest()[:16]
        )
        digest[6] = (digest[6] & 15) | 128  # UUIDv8 application-defined deterministic identifier.
        digest[8] = (digest[8] & 63) | 128
        return UUID(bytes=bytes(digest))
