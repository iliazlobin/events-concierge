from __future__ import annotations

import hashlib
from collections.abc import Mapping
from uuid import UUID, uuid4

import pytest

from events_concierge.adapters.ses.notifier import (
    NotificationDeliveryError,
    SesNotificationAdapter,
)
from events_concierge.domain.credentials import Tenant
from events_concierge.ports.notifications import Notification, NotificationKind


class _Tenants:
    def __init__(self, tenant: Tenant | None) -> None:
        self._tenant = tenant

    async def get(self, tenant_id: UUID) -> Tenant | None:
        return self._tenant if self._tenant is not None and self._tenant.tenant_id == tenant_id else None

    async def add(self, tenant: Tenant) -> None:
        self._tenant = tenant


class _Ses:
    def __init__(self, response: Mapping[str, object] | None = None) -> None:
        self.response = response or {"MessageId": "ses-message-1"}
        self.calls: list[dict[str, object]] = []

    def send_email(self, **kwargs: object) -> Mapping[str, object]:
        self.calls.append(kwargs)
        return self.response


def _notification(tenant_id: UUID) -> Notification:
    return Notification(
        tenant_id=tenant_id,
        kind=NotificationKind.COMPLETION,
        subject="Your registration is confirmed",
        body="Open your plan to see the details.",
        dedup_key="private:notification:dedup:key",
    )


async def test_ses_adapter_resolves_recipient_and_emits_hashed_delivery_tag() -> None:
    tenant_id = uuid4()
    ses = _Ses()
    adapter = SesNotificationAdapter(
        ses,
        _Tenants(Tenant(tenant_id, "subject", "person@example.com", "relay@u.example.com")),
        from_email="notify@example.com",
        configuration_set="events-production",
        reply_to="support@example.com",
    )

    await adapter.send(_notification(tenant_id))

    assert len(ses.calls) == 1
    request = ses.calls[0]
    assert request["Destination"] == {"ToAddresses": ["person@example.com"]}
    assert request["ReplyToAddresses"] == ["support@example.com"]
    assert request["ConfigurationSetName"] == "events-production"
    assert request["EmailTags"] == [
        {"Name": "notification_kind", "Value": "completion"},
        {
            "Name": "notification_dedup",
            "Value": hashlib.sha256(b"private:notification:dedup:key").hexdigest(),
        },
    ]
    assert "private:notification:dedup:key" not in repr(request)


async def test_ses_adapter_fails_before_delivery_for_unknown_tenant() -> None:
    tenant_id = uuid4()
    ses = _Ses()
    adapter = SesNotificationAdapter(
        ses,
        _Tenants(None),
        from_email="notify@example.com",
        configuration_set="events-production",
    )

    with pytest.raises(NotificationDeliveryError, match="not provisioned"):
        await adapter.send(_notification(tenant_id))

    assert ses.calls == []


async def test_ses_adapter_requires_provider_acknowledgement() -> None:
    tenant_id = uuid4()
    adapter = SesNotificationAdapter(
        _Ses({"MessageId": ""}),
        _Tenants(Tenant(tenant_id, "subject", "person@example.com", "relay@u.example.com")),
        from_email="notify@example.com",
        configuration_set="events-production",
    )

    with pytest.raises(NotificationDeliveryError, match="no message identifier"):
        await adapter.send(_notification(tenant_id))


async def test_ses_adapter_exposes_non_idempotent_redelivery_boundary() -> None:
    """The stable tag is evidence, not a provider dedup primitive; ADR-009 remains open."""
    tenant_id = uuid4()
    ses = _Ses()
    adapter = SesNotificationAdapter(
        ses,
        _Tenants(Tenant(tenant_id, "subject", "person@example.com", "relay@u.example.com")),
        from_email="notify@example.com",
        configuration_set="events-production",
    )
    notification = _notification(tenant_id)

    await adapter.send(notification)
    await adapter.send(notification)

    assert len(ses.calls) == 2
    assert ses.calls[0]["EmailTags"] == ses.calls[1]["EmailTags"]


@pytest.mark.parametrize("address", ["", "missing-at", "a@b@c", "bad\n@example.com"])
def test_ses_adapter_rejects_unsafe_sender_configuration(address: str) -> None:
    with pytest.raises(NotificationDeliveryError):
        SesNotificationAdapter(
            _Ses(),
            _Tenants(None),
            from_email=address,
            configuration_set="events-production",
        )


def test_ses_adapter_rejects_unsafe_configuration_set() -> None:
    with pytest.raises(NotificationDeliveryError, match="invalid format"):
        SesNotificationAdapter(
            _Ses(),
            _Tenants(None),
            from_email="notify@example.com",
            configuration_set="events production",
        )
