from __future__ import annotations

import os
from collections.abc import Mapping
from uuid import uuid4

import pytest

from events_concierge.adapters.kms.notification_secrets import KmsNotificationSecretProtector
from events_concierge.ports.notification_secrets import NotificationSecretProtectionError


class _Kms:
    def __init__(self) -> None:
        self.key = os.urandom(32)
        self.encrypted_key = b"kms-wrapped-data-key"
        self.immutable_key_id = "arn:aws:kms:us-west-2:111122223333:key/immutable-version"
        self.generate_calls: list[dict[str, object]] = []
        self.decrypt_calls: list[dict[str, object]] = []

    def generate_data_key(self, **kwargs: object) -> Mapping[str, object]:
        self.generate_calls.append(kwargs)
        return {
            "Plaintext": self.key,
            "CiphertextBlob": self.encrypted_key,
            "KeyId": self.immutable_key_id,
        }

    def decrypt(self, **kwargs: object) -> Mapping[str, object]:
        self.decrypt_calls.append(kwargs)
        if kwargs.get("CiphertextBlob") != self.encrypted_key:
            return {"Plaintext": os.urandom(32), "KeyId": self.immutable_key_id}
        return {"Plaintext": self.key, "KeyId": self.immutable_key_id}


async def test_kms_protector_round_trips_with_tenant_bound_context() -> None:
    tenant_id = uuid4()
    client = _Kms()
    protector = KmsNotificationSecretProtector(client, key_id="alias/events-notification")

    protected = await protector.protect_completion_url(
        tenant_id, "https://events.example/handoff/complete?token=secret"
    )
    revealed = await protector.reveal_completion_url(tenant_id, protected)

    assert revealed == "https://events.example/handoff/complete?token=secret"
    assert len(client.generate_calls) == 1
    expected_context = client.generate_calls[0]["EncryptionContext"]
    assert isinstance(expected_context, dict)
    assert expected_context["application"] == "events-concierge"
    assert expected_context["purpose"] == "notification-completion-url"
    assert len(expected_context["envelope_id"]) == 43
    assert str(tenant_id) not in str(expected_context)
    assert client.generate_calls[0]["KeyId"] == "alias/events-notification"
    assert client.generate_calls[0]["KeySpec"] == "AES_256"
    assert client.decrypt_calls[0]["EncryptionContext"] == expected_context
    assert client.decrypt_calls[0]["KeyId"] == client.immutable_key_id
    assert "token=secret" not in protected


async def test_kms_protector_rejects_cross_tenant_replay() -> None:
    client = _Kms()
    protector = KmsNotificationSecretProtector(client, key_id="alias/events-notification")
    protected = await protector.protect_completion_url(uuid4(), "https://events.example/complete")

    with pytest.raises(NotificationSecretProtectionError, match="failed authentication"):
        await protector.reveal_completion_url(uuid4(), protected)


async def test_kms_protector_rejects_tampered_envelope() -> None:
    client = _Kms()
    protector = KmsNotificationSecretProtector(client, key_id="alias/events-notification")
    protected = await protector.protect_completion_url(uuid4(), "https://events.example/complete")
    replacement = "A" if protected[-1] != "A" else "B"

    with pytest.raises(NotificationSecretProtectionError):
        await protector.reveal_completion_url(uuid4(), protected[:-1] + replacement)


async def test_kms_protector_rejects_invalid_kms_key_material() -> None:
    class InvalidKms(_Kms):
        def generate_data_key(self, **kwargs: object) -> Mapping[str, object]:
            return {
                "Plaintext": b"short",
                "CiphertextBlob": self.encrypted_key,
                "KeyId": self.immutable_key_id,
            }

    protector = KmsNotificationSecretProtector(InvalidKms(), key_id="alias/events-notification")

    with pytest.raises(NotificationSecretProtectionError, match="invalid plaintext data key"):
        await protector.protect_completion_url(uuid4(), "https://events.example/complete")


async def test_kms_protector_decrypts_with_enveloped_immutable_key_after_alias_rotation() -> None:
    client = _Kms()
    old = KmsNotificationSecretProtector(client, key_id="alias/events-notification")
    tenant_id = uuid4()
    protected = await old.protect_completion_url(tenant_id, "https://events.example/complete")

    rotated = KmsNotificationSecretProtector(client, key_id="alias/events-notification-next")
    assert (
        await rotated.reveal_completion_url(tenant_id, protected)
        == "https://events.example/complete"
    )
    assert client.decrypt_calls[-1]["KeyId"] == client.immutable_key_id


async def test_kms_protector_rejects_decrypt_key_identity_mismatch() -> None:
    class WrongKeyKms(_Kms):
        def decrypt(self, **kwargs: object) -> Mapping[str, object]:
            response = dict(super().decrypt(**kwargs))
            response["KeyId"] = "arn:aws:kms:us-west-2:111122223333:key/wrong"
            return response

    client = WrongKeyKms()
    protector = KmsNotificationSecretProtector(client, key_id="alias/events-notification")
    tenant_id = uuid4()
    protected = await protector.protect_completion_url(
        tenant_id, "https://events.example/complete"
    )

    with pytest.raises(NotificationSecretProtectionError, match="unexpected key id"):
        await protector.reveal_completion_url(tenant_id, protected)
