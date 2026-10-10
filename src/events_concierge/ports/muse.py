"""Narrow catalog and tenant persistence boundaries for the Muse connector."""

from datetime import datetime
from typing import Protocol
from uuid import UUID

from ..domain.catalog_browse import CatalogBrowseEvent
from ..domain.muse import (
    MuseConnection,
    SeenRegistration,
    SignupBatch,
    SignupEvent,
    SignupItem,
    SignupOutcome,
    SignupRegistration,
    SignupRegistrations,
)


class MuseCatalogPort(Protocol):
    async def get_browse_event(self, canonical_event_id: UUID) -> CatalogBrowseEvent | None: ...


class MuseRepository(Protocol):
    async def connection(self, tenant_id: UUID) -> MuseConnection: ...
    async def connect(
        self, tenant_id: UUID, digest: str, expires_at: datetime
    ) -> MuseConnection: ...
    async def revoke(self, tenant_id: UUID) -> None: ...
    async def authenticate(self, tenant_id: UUID, digest: str) -> bool: ...
    async def by_request(self, tenant_id: UUID, request_id: UUID) -> SignupBatch | None: ...
    async def create(
        self,
        tenant_id: UUID,
        request_id: UUID,
        events: list[SignupEvent],
    ) -> SignupBatch: ...
    async def batches(
        self, tenant_id: UUID, limit: int = 50, cursor: UUID | None = None
    ) -> list[SignupBatch]: ...
    async def batch(self, tenant_id: UUID, batch_id: UUID) -> SignupBatch | None: ...
    async def queue_registration(
        self,
        tenant_id: UUID,
        request_id: UUID,
        event_id: UUID,
        event: SignupEvent | None = None,
    ) -> SignupRegistration: ...
    async def registrations(
        self, tenant_id: UUID, limit: int = 50, cursor: UUID | None = None
    ) -> SignupRegistrations: ...
    async def see_registrations(self, tenant_id: UUID, items: list[SeenRegistration]) -> None: ...
    async def claim(
        self,
        tenant_id: UUID,
        batch_id: UUID,
        event_id: UUID,
        attempt_id: UUID,
    ) -> SignupItem: ...
    async def report(
        self,
        tenant_id: UUID,
        batch_id: UUID,
        event_id: UUID,
        attempt_id: UUID,
        outcome: SignupOutcome,
    ) -> SignupItem: ...
