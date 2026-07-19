"""Authenticated-encryption coverage for local notification projections."""

from __future__ import annotations

import base64
from uuid import uuid4

import pytest

from events_concierge.adapters.mock.notification_secrets import (
    DevelopmentNotificationSecretProtector,
)
from events_concierge.ports.notification_secrets import NotificationSecretProtectionError


async def test_development_protector_interoperates_across_process_instances() -> None:
    """The explicit local key is stable while each envelope retains a fresh authenticated nonce."""
    tenant_id = uuid4()
    token = "a-local-bearer-value"
    completion_url = f"http://localhost:8000/v1/tasks/{token}/done"

    protected = await DevelopmentNotificationSecretProtector().protect_completion_url(
        tenant_id,
        completion_url,
    )
    revealed = await DevelopmentNotificationSecretProtector().reveal_completion_url(
        tenant_id,
        protected,
    )

    if revealed != completion_url:
        raise AssertionError("separate local protector instances did not interoperate")
    if completion_url in protected or token in protected:
        raise AssertionError("protected notification projection exposed plaintext")


async def test_development_protector_rejects_cross_tenant_and_tampered_envelopes() -> None:
    owner_tenant = uuid4()
    protected = await DevelopmentNotificationSecretProtector().protect_completion_url(
        owner_tenant,
        "http://localhost:8000/v1/tasks/local-capability/done",
    )

    with pytest.raises(NotificationSecretProtectionError, match="failed authentication"):
        await DevelopmentNotificationSecretProtector().reveal_completion_url(uuid4(), protected)

    version, encoded = protected.split(".", 1)
    envelope = bytearray(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    envelope[-1] ^= 1
    tampered = f"{version}." + base64.urlsafe_b64encode(envelope).rstrip(b"=").decode("ascii")
    with pytest.raises(NotificationSecretProtectionError, match="failed authentication"):
        await DevelopmentNotificationSecretProtector().reveal_completion_url(
            owner_tenant,
            tampered,
        )


async def test_development_protector_rejects_oversized_envelope_before_decode() -> None:
    with pytest.raises(NotificationSecretProtectionError, match="invalid envelope"):
        await DevelopmentNotificationSecretProtector().reveal_completion_url(
            uuid4(),
            "v1." + ("A" * 5501),
        )
