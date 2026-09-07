"""KMS-envelope authenticated encryption for notification capability projections."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import secrets
from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ...ports.notification_secrets import NotificationSecretProtectionError

_VERSION = "v2"
_NONCE_BYTES = 12
_AES_256_KEY_BYTES = 32
_CONTEXT_ID_BYTES = 32
_ASCII_SPACE = 32
_MAX_COMPLETION_URL_BYTES = 4096
_MAX_ENVELOPE_CHARS = 32_768
_MAX_KMS_KEY_ID_CHARS = 2048
_GCM_TAG_BYTES = 16
_AAD_PREFIX = b"events-concierge:notification-secret:v2:handoff-completion-url:"
_KMS_APPLICATION = "events-concierge"
_KMS_PURPOSE = "notification-completion-url"


class KmsClient(Protocol):
    def generate_data_key(self, **kwargs: object) -> Mapping[str, object]: ...

    def decrypt(self, **kwargs: object) -> Mapping[str, object]: ...


class KmsNotificationSecretProtector:
    """Protect each value with a fresh DEK wrapped by one deployment-owned KMS key."""

    def __init__(self, client: KmsClient, *, key_id: str) -> None:
        if not key_id.strip() or len(key_id) > _MAX_KMS_KEY_ID_CHARS:
            raise ValueError("KMS key_id must be a bounded non-empty identifier")
        self._client = client
        self._key_id = key_id.strip()

    async def protect_completion_url(self, tenant_id: UUID, completion_url: str) -> str:
        plaintext = completion_url.encode("utf-8")
        if not plaintext or len(plaintext) > _MAX_COMPLETION_URL_BYTES:
            raise NotificationSecretProtectionError("completion URL has an invalid length")
        context_id = secrets.token_bytes(_CONTEXT_ID_BYTES)
        context = _context(context_id)
        response = await asyncio.to_thread(
            self._client.generate_data_key,
            KeyId=self._key_id,
            KeySpec="AES_256",
            EncryptionContext=context,
        )
        encrypted_key = _bytes(response.get("CiphertextBlob"), field="KMS encrypted data key")
        immutable_key_id = _key_id(response.get("KeyId"), field="KMS immutable key id")
        plaintext_key = _secret_bytes(response.get("Plaintext"), field="KMS plaintext data key")
        if len(plaintext_key) != _AES_256_KEY_BYTES:
            _zero(plaintext_key)
            raise NotificationSecretProtectionError("KMS returned an invalid plaintext data key")
        try:
            nonce = secrets.token_bytes(_NONCE_BYTES)
            ciphertext = AESGCM(bytes(plaintext_key)).encrypt(
                nonce,
                plaintext,
                _aad(tenant_id, immutable_key_id, context_id),
            )
        finally:
            _zero(plaintext_key)
        document = {
            "ciphertext": _encode(ciphertext),
            "context_id": _encode(context_id),
            "encrypted_key": _encode(encrypted_key),
            "key_id": immutable_key_id,
            "nonce": _encode(nonce),
        }
        encoded = _encode(json.dumps(document, separators=(",", ":"), sort_keys=True).encode())
        envelope = f"{_VERSION}.{encoded}"
        if len(envelope) > _MAX_ENVELOPE_CHARS:
            raise NotificationSecretProtectionError("protected notification secret is too large")
        return envelope

    async def reveal_completion_url(self, tenant_id: UUID, protected_value: str) -> str:
        document = _decode_envelope(protected_value)
        encrypted_key = _field_bytes(document, "encrypted_key")
        context_id = _field_bytes(document, "context_id")
        immutable_key_id = _key_id(document.get("key_id"), field="protected KMS key id")
        nonce = _field_bytes(document, "nonce")
        ciphertext = _field_bytes(document, "ciphertext")
        if (
            len(context_id) != _CONTEXT_ID_BYTES
            or len(nonce) != _NONCE_BYTES
            or len(ciphertext) <= _GCM_TAG_BYTES
        ):
            raise NotificationSecretProtectionError(
                "protected notification secret has an invalid envelope"
            )
        response = await asyncio.to_thread(
            self._client.decrypt,
            CiphertextBlob=encrypted_key,
            EncryptionContext=_context(context_id),
            KeyId=immutable_key_id,
        )
        plaintext_key = _secret_bytes(response.get("Plaintext"), field="KMS plaintext data key")
        try:
            response_key_id = _key_id(response.get("KeyId"), field="KMS decrypted key id")
            if not secrets.compare_digest(response_key_id, immutable_key_id):
                raise NotificationSecretProtectionError("KMS decrypted with an unexpected key id")
            if len(plaintext_key) != _AES_256_KEY_BYTES:
                raise NotificationSecretProtectionError(
                    "KMS returned an invalid plaintext data key"
                )
            plaintext = AESGCM(bytes(plaintext_key)).decrypt(
                nonce,
                ciphertext,
                _aad(tenant_id, immutable_key_id, context_id),
            )
        except InvalidTag as error:
            raise NotificationSecretProtectionError(
                "protected notification secret failed authentication"
            ) from error
        finally:
            _zero(plaintext_key)
        try:
            completion_url = plaintext.decode("utf-8")
        except UnicodeDecodeError as error:
            raise NotificationSecretProtectionError(
                "protected notification secret is not UTF-8"
            ) from error
        if not completion_url or len(plaintext) > _MAX_COMPLETION_URL_BYTES:
            raise NotificationSecretProtectionError("completion URL has an invalid length")
        return completion_url


def _context(context_id: bytes) -> dict[str, str]:
    return {
        "application": _KMS_APPLICATION,
        "purpose": _KMS_PURPOSE,
        # Random per-envelope value: CloudTrail/KMS audit metadata cannot be joined back to a
        # deleted tenant. Tenant binding remains cryptographic in the local AES-GCM AAD.
        "envelope_id": _encode(context_id),
    }


def _aad(tenant_id: UUID, key_id: str, context_id: bytes) -> bytes:
    return _AAD_PREFIX + tenant_id.bytes + b":" + key_id.encode("utf-8") + b":" + context_id


def _decode_envelope(value: str) -> Mapping[str, object]:
    version, separator, encoded = value.partition(".")
    if (
        version != _VERSION
        or separator != "."
        or not encoded
        or len(value) > _MAX_ENVELOPE_CHARS
    ):
        raise NotificationSecretProtectionError(
            "protected notification secret has an invalid envelope"
        )
    try:
        decoded = _decode(encoded)
        document: object = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise NotificationSecretProtectionError(
            "protected notification secret has an invalid envelope"
        ) from error
    if not isinstance(document, dict) or set(document) != {
        "ciphertext",
        "context_id",
        "encrypted_key",
        "key_id",
        "nonce",
    }:
        raise NotificationSecretProtectionError(
            "protected notification secret has an invalid envelope"
        )
    return document


def _field_bytes(document: Mapping[str, object], field: str) -> bytes:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise NotificationSecretProtectionError(
            "protected notification secret has an invalid envelope"
        )
    try:
        return _decode(value)
    except (binascii.Error, ValueError) as error:
        raise NotificationSecretProtectionError(
            "protected notification secret has an invalid envelope"
        ) from error


def _bytes(value: object, *, field: str) -> bytes:
    if not isinstance(value, bytes) or not value:
        raise NotificationSecretProtectionError(f"{field} is missing or invalid")
    return value


def _key_id(value: object, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > _MAX_KMS_KEY_ID_CHARS
        or any(ord(character) < _ASCII_SPACE for character in value)
    ):
        raise NotificationSecretProtectionError(f"{field} is missing or invalid")
    return value


def _secret_bytes(value: object, *, field: str) -> bytearray:
    return bytearray(_bytes(value, field=field))


def _zero(value: bytearray) -> None:
    value[:] = b"\0" * len(value)


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
