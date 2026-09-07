"""Production SES v2 notification delivery behind the typed notification port.

The adapter is SDK-agnostic: a deployment injects its regional, least-privilege SES v2 client.
Durable send authority and retries remain in the PostgreSQL outbox. SES does not offer an email
idempotency token, so the hashed dedup key is emitted as an SES message tag for delivery-event
correlation; the unresolved post-send acknowledgement policy remains an explicit launch decision.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping
from typing import Protocol

from ...ports.notifications import Notification
from ...ports.repositories import TenantRepository

_MAX_EMAIL_LENGTH = 320
_MAX_SUBJECT_LENGTH = 200
_MAX_BODY_BYTES = 100_000
_CONFIGURATION_SET = re.compile(r"[A-Za-z0-9_-]{1,64}")


class SesV2Client(Protocol):
    def send_email(self, **kwargs: object) -> Mapping[str, object]: ...


class NotificationDeliveryError(RuntimeError):
    """A notification cannot be handed to the configured delivery channel."""


class SesNotificationAdapter:
    """Resolve the RLS-scoped recipient and send one UTF-8 text email through SES v2."""

    def __init__(
        self,
        client: SesV2Client,
        tenants: TenantRepository,
        *,
        from_email: str,
        configuration_set: str,
        reply_to: str | None = None,
    ) -> None:
        self._client = client
        self._tenants = tenants
        self._from_email = _email(from_email, field="from_email")
        self._configuration_set = _bounded(configuration_set, field="configuration_set", limit=64)
        if _CONFIGURATION_SET.fullmatch(self._configuration_set) is None:
            raise NotificationDeliveryError("configuration_set has an invalid format")
        self._reply_to = _email(reply_to, field="reply_to") if reply_to is not None else None

    async def send(self, notification: Notification) -> None:
        tenant = await self._tenants.get(notification.tenant_id)
        if tenant is None:
            raise NotificationDeliveryError("notification tenant is not provisioned")
        recipient = _email(tenant.notify_email, field="tenant notify_email")
        subject = _bounded(
            notification.subject,
            field="notification subject",
            limit=_MAX_SUBJECT_LENGTH,
        )
        if "\r" in subject or "\n" in subject:
            raise NotificationDeliveryError("notification subject cannot contain newlines")
        body_bytes = notification.body.encode("utf-8")
        if not body_bytes or len(body_bytes) > _MAX_BODY_BYTES:
            raise NotificationDeliveryError("notification body has an invalid length")
        dedup_digest = hashlib.sha256(notification.dedup_key.encode("utf-8")).hexdigest()
        request: dict[str, object] = {
            "FromEmailAddress": self._from_email,
            "Destination": {"ToAddresses": [recipient]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": notification.body, "Charset": "UTF-8"}},
                }
            },
            "ConfigurationSetName": self._configuration_set,
            "EmailTags": [
                {"Name": "notification_kind", "Value": notification.kind.value},
                {"Name": "notification_dedup", "Value": dedup_digest},
            ],
        }
        if self._reply_to is not None:
            request["ReplyToAddresses"] = [self._reply_to]
        response = await asyncio.to_thread(self._client.send_email, **request)
        message_id = response.get("MessageId")
        if not isinstance(message_id, str) or not message_id.strip():
            raise NotificationDeliveryError("SES returned no message identifier")


def _bounded(value: str, *, field: str, limit: int) -> str:
    stripped = value.strip()
    if not stripped or len(stripped) > limit:
        raise NotificationDeliveryError(f"{field} has an invalid length")
    return stripped


def _email(value: str, *, field: str) -> str:
    candidate = _bounded(value, field=field, limit=_MAX_EMAIL_LENGTH)
    if (
        "\r" in candidate
        or "\n" in candidate
        or candidate.count("@") != 1
        or candidate.startswith("@")
        or candidate.endswith("@")
        or any(character.isspace() for character in candidate)
    ):
        raise NotificationDeliveryError(f"{field} is not a safe email address")
    return candidate
