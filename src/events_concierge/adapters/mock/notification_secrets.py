"""Development-only authenticated encryption for notification outbox secrets.

The fixed key is intentionally public and must never be used for production data.  Its only
purpose is to let independently started local API, workflow-worker, and notifier processes share
encrypted outbox projections without introducing a local KMS service.  Production composition
requires an explicitly provisioned ``NotificationSecretProtector`` instead.
"""

from __future__ import annotations

import base64
import binascii
import secrets
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ...ports.notification_secrets import (
    NotificationSecretProtectionError,
    NotificationSecretProtector,
)

_VERSION = "v1"
_NONCE_BYTES = 12
_MAX_COMPLETION_URL_BYTES = 4096
_MAX_ENCODED_ENVELOPE_CHARS = 5500
_AAD_PREFIX = b"events-concierge:notification-secret:v1:handoff-completion-url:"

# DEVELOPMENT ONLY. This deliberately stable, non-secret value permits separate local Compose
# processes to interoperate. The automatic composition fallback selects it only in mock mode.
_DEVELOPMENT_ONLY_KEY = bytes.fromhex(
    "296bfd937dba0c614ee1747b77425ceca0fc18b303265b620fb186c1a7980a0e"
)


class DevelopmentNotificationSecretProtector(NotificationSecretProtector):
    """AES-256-GCM local adapter with tenant/purpose-bound associated data."""

    def __init__(self) -> None:
        self._cipher = AESGCM(_DEVELOPMENT_ONLY_KEY)

    async def protect_completion_url(self, tenant_id: UUID, completion_url: str) -> str:
        plaintext = completion_url.encode("utf-8")
        if not plaintext or len(plaintext) > _MAX_COMPLETION_URL_BYTES:
            raise NotificationSecretProtectionError("completion URL has an invalid length")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        encrypted = self._cipher.encrypt(nonce, plaintext, _aad(tenant_id))
        return f"{_VERSION}.{_encode(nonce + encrypted)}"

    async def reveal_completion_url(self, tenant_id: UUID, protected_value: str) -> str:
        version, separator, encoded = protected_value.partition(".")
        if (
            version != _VERSION
            or separator != "."
            or not encoded
            or len(encoded) > _MAX_ENCODED_ENVELOPE_CHARS
        ):
            raise NotificationSecretProtectionError(
                "protected notification secret has an invalid envelope"
            )
        try:
            envelope = _decode(encoded)
        except (binascii.Error, ValueError) as error:
            raise NotificationSecretProtectionError(
                "protected notification secret has an invalid envelope"
            ) from error
        if len(envelope) <= _NONCE_BYTES + 16:
            raise NotificationSecretProtectionError(
                "protected notification secret has an invalid envelope"
            )
        nonce, ciphertext = envelope[:_NONCE_BYTES], envelope[_NONCE_BYTES:]
        try:
            plaintext = self._cipher.decrypt(nonce, ciphertext, _aad(tenant_id))
            completion_url = plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as error:
            raise NotificationSecretProtectionError(
                "protected notification secret failed authentication"
            ) from error
        if not completion_url or len(plaintext) > _MAX_COMPLETION_URL_BYTES:
            raise NotificationSecretProtectionError("completion URL has an invalid length")
        return completion_url


def _aad(tenant_id: UUID) -> bytes:
    return _AAD_PREFIX + tenant_id.bytes


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
