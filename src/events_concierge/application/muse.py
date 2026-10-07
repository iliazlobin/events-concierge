"""Prepare explicit free-event selections; Muse owns browser execution."""

import hashlib
import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ..domain.enums import EventStatus, PriceStatus, RegistrationStatus
from ..domain.muse import (
    MAX_MUSE_EVENTS,
    MuseConflictError,
    MuseNotFoundError,
    SignupBatch,
    SignupEvent,
    SignupItem,
    provider_url,
)
from ..ports.auth import AuthenticationFailedError
from ..ports.muse import MuseCatalogPort, MuseRepository

_TOKEN = re.compile(r"^ec_muse_([0-9a-f]{32})\.([A-Za-z0-9_-]{43})$")
_CONNECTION_LIFETIME = timedelta(days=30)
_MAX_OBSERVATION_AGE = timedelta(hours=48)


class MuseSignupService:
    def __init__(self, repository: MuseRepository, catalog: MuseCatalogPort) -> None:
        self.repository = repository
        self.catalog = catalog

    async def connect(self, tenant_id: UUID) -> tuple[str, datetime]:
        secret = f"ec_muse_{tenant_id.hex}.{secrets.token_urlsafe(32)}"
        expires_at = datetime.now(UTC) + _CONNECTION_LIFETIME
        await self.repository.connect(tenant_id, _digest(secret), expires_at)
        return secret, expires_at

    async def authenticate(self, authorization: str | None) -> UUID:
        if authorization is None or not authorization.startswith("Bearer "):
            raise AuthenticationFailedError("Muse connection required")
        secret = authorization[7:]
        match = _TOKEN.fullmatch(secret)
        if match is None:
            raise AuthenticationFailedError("Muse connection required")
        tenant_id = UUID(hex=match[1])
        if not await self.repository.authenticate(tenant_id, _digest(secret)):
            raise AuthenticationFailedError("Muse connection required")
        return tenant_id

    async def prepare(
        self,
        tenant_id: UUID,
        request_id: UUID,
        event_ids: list[UUID],
    ) -> SignupBatch:
        if not 1 <= len(event_ids) <= MAX_MUSE_EVENTS or len(set(event_ids)) != len(event_ids):
            raise ValueError("select one to five distinct event dates")
        existing = await self.repository.by_request(tenant_id, request_id)
        if existing is not None:
            if {i.event.canonical_event_id for i in existing.items} != set(event_ids):
                raise MuseConflictError("this request already has a different selection")
            return existing
        now = datetime.now(UTC)
        events: list[SignupEvent] = []
        for event_id in event_ids:
            browse = await self.catalog.get_browse_event(event_id)
            if browse is None:
                raise ValueError("an event is no longer available in the published catalog")
            event = browse.canonical_event
            if (
                event.start_at <= now
                or event.price_status is not PriceStatus.FREE
                or event.event_status is not EventStatus.SCHEDULED
                or event.registration_status is RegistrationStatus.SOLD_OUT
            ):
                raise ValueError(
                    "Muse currently supports upcoming free events with available registration"
                )
            eligible: list[tuple[str, datetime]] = []
            for source in browse.sources:
                try:
                    url = provider_url(source.registration_url)
                except ValueError:
                    continue
                if now - _MAX_OBSERVATION_AGE <= source.last_seen_at <= now + timedelta(minutes=5):
                    eligible.append((url, source.last_seen_at))
            if not eligible:
                raise ValueError("Muse needs a recently collected Luma or Meetup event")
            url, observed_at = max(eligible, key=lambda candidate: candidate[1])
            events.append(
                SignupEvent(
                    canonical_event_id=event_id,
                    title=event.title,
                    start_at=event.start_at,
                    end_at=event.end_at,
                    venue_name=event.venue_name,
                    city=event.city_norm,
                    registration_url=url,
                    observed_at=observed_at,
                )
            )
        return await self.repository.create(tenant_id, request_id, events)

    async def claim(
        self,
        tenant_id: UUID,
        batch_id: UUID,
        event_id: UUID,
        attempt_id: UUID,
    ) -> SignupItem:
        batch = await self.repository.batch(tenant_id, batch_id)
        item = (
            next((i for i in batch.items if i.event.canonical_event_id == event_id), None)
            if batch
            else None
        )
        if item is None:
            raise MuseNotFoundError("signup item not found")
        if item.attempt_id == attempt_id:
            return item
        if item.status != "queued":
            raise MuseConflictError("another attempt owns this signup; check its provider status")
        browse = await self.catalog.get_browse_event(event_id)
        if browse is None:
            raise MuseConflictError("event no longer available; do not submit")
        event = browse.canonical_event
        now = datetime.now(UTC)
        if (
            event.price_status is not PriceStatus.FREE
            or event.event_status is not EventStatus.SCHEDULED
            or event.registration_status is RegistrationStatus.SOLD_OUT
            or event.start_at != item.event.start_at
            or event.end_at != item.event.end_at
            or event.venue_name != item.event.venue_name
            or event.city_norm != item.event.city
            or event.start_at <= now
            or not any(
                source.registration_url == item.event.registration_url
                and now - _MAX_OBSERVATION_AGE <= source.last_seen_at <= now + timedelta(minutes=5)
                for source in browse.sources
            )
        ):
            raise MuseConflictError("event facts changed or became stale; do not submit")
        return await self.repository.claim(tenant_id, batch_id, event_id, attempt_id)


def _digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("ascii")).hexdigest()
