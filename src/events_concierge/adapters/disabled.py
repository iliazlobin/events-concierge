"""Explicitly unavailable product effects for the discovery release.

These adapters never claim a read, delivery, write, revocation, or purge succeeded. In particular,
absence of a configured provider is not evidence that a tenant has no historical external data.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from ..domain.conflict import BusyBlock
from ..domain.credentials import Credential
from ..domain.enums import Source
from ..ports.calendar import CalendarEntry
from ..ports.notifications import Notification


class DisabledProductCapabilityError(RuntimeError):
    """The release cannot execute or attest completion of this external operation."""


class DisabledProductPort:
    """Marker rejected as a provisioned provider by the full-product preflight."""


class DisabledNotifier(DisabledProductPort):
    async def send(self, notification: Notification) -> None:
        raise DisabledProductCapabilityError("notification delivery is disabled in discovery")


class DisabledNotificationSecretProtector(DisabledProductPort):
    async def protect_completion_url(self, tenant_id: UUID, completion_url: str) -> str:
        raise DisabledProductCapabilityError("notification capabilities are disabled in discovery")

    async def reveal_completion_url(self, tenant_id: UUID, protected_value: str) -> str:
        raise DisabledProductCapabilityError("notification capabilities are disabled in discovery")


class DisabledCredentialVault(DisabledProductPort):
    async def store(self, credential: Credential) -> None:
        raise DisabledProductCapabilityError("provider credentials are disabled in discovery")

    async def get(self, tenant_id: UUID, source: Source) -> Credential | None:
        raise DisabledProductCapabilityError("provider credentials are disabled in discovery")

    async def revoke(self, tenant_id: UUID, source: Source) -> None:
        raise DisabledProductCapabilityError("provider credential revocation is unavailable")

    async def delete_tenant(self, tenant_id: UUID) -> None:
        raise DisabledProductCapabilityError(
            "provider credential purge cannot be verified in discovery"
        )


class DisabledCalendar(DisabledProductPort):
    async def free_busy(
        self, tenant_id: UUID, window_start: datetime, window_end: datetime
    ) -> list[BusyBlock]:
        raise DisabledProductCapabilityError("calendar access is disabled in discovery")

    async def upsert_event(self, tenant_id: UUID, entry: CalendarEntry) -> None:
        raise DisabledProductCapabilityError("calendar synchronization is disabled in discovery")

    async def delete_event(
        self,
        tenant_id: UUID,
        calendar_event_id: str,
        *,
        canonical_event_id: UUID | None = None,
    ) -> None:
        raise DisabledProductCapabilityError("calendar deletion cannot be verified in discovery")

    async def delete_tenant_events(self, tenant_id: UUID) -> None:
        raise DisabledProductCapabilityError("calendar purge cannot be verified in discovery")


_DISABLED_DISCOVERY_PORT_TYPES: dict[str, type[DisabledProductPort]] = {
    "notifier": DisabledNotifier,
    "notification_secret_protector": DisabledNotificationSecretProtector,
    "credential_vault": DisabledCredentialVault,
    "calendar": DisabledCalendar,
}


def is_disabled_discovery_port(name: str, value: object) -> bool:
    """Accept only these concrete fail-closed ports, not arbitrary provider claims/subclasses."""
    return type(value) is _DISABLED_DISCOVERY_PORT_TYPES.get(name)
