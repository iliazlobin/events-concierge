"""Selection authority and safe, resumable Muse handoffs; no provider I/O."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError

from events_concierge.application.muse import MuseSignupService
from events_concierge.domain.catalog_browse import CatalogBrowseEvent, CatalogBrowseSource
from events_concierge.domain.enums import EventStatus, PriceStatus, RegistrationStatus, Source
from events_concierge.domain.events import CanonicalEvent
from events_concierge.domain.muse import (
    MuseConflictError,
    MuseNotFoundError,
    SignupBatch,
    SignupItem,
    SignupOutcome,
    SignupRegistration,
    provider_url,
)
from events_concierge.ports.auth import AuthenticationFailedError
from events_concierge.ports.muse import MuseRepository


def published_event(url="https://lu.ma/muse-test", age=timedelta(hours=1)):
    now = datetime.now(UTC)
    event = CanonicalEvent(
        canonical_event_id=uuid4(),
        title="Published free event",
        start_at=now + timedelta(days=7),
        end_at=now + timedelta(days=7, hours=2),
        venue_name="The venue",
        city_norm="San Francisco",
        price_status=PriceStatus.FREE,
        registration_status=RegistrationStatus.OPEN,
    )
    source = CatalogBrowseSource(
        source_key="luma-sf",
        label="Luma SF",
        publisher="Luma",
        provider="public_jsonld",
        seed_url="https://lu.ma/sf",
        source=Source.PUBLIC_JSONLD,
        source_event_id="test",
        registration_url=url,
        last_seen_at=now - age,
        refresh_run_key="observed",
    )
    return CatalogBrowseEvent(event, (source,))


def service_for(browse=None):
    repository = AsyncMock(spec=MuseRepository)
    repository.by_request.return_value = None
    catalog = AsyncMock()
    catalog.get_browse_event.return_value = browse or published_event()

    async def create(_tenant, request_id, events):
        now = datetime.now(UTC)
        return SignupBatch(
            batch_id=uuid4(),
            request_id=request_id,
            created_at=now,
            items=[SignupItem(event=event, updated_at=now) for event in events],
        )

    repository.create.side_effect = create
    return MuseSignupService(repository, catalog), repository, catalog


async def test_connection_returns_secret_once_and_persists_only_digest():
    service, repository, _ = service_for()
    tenant = uuid4()
    secret, expires = await service.connect(tenant)
    persisted_tenant, digest, persisted_expiry = repository.connect.await_args.args
    assert persisted_tenant == tenant and persisted_expiry == expires
    assert len(digest) == 64 and digest != secret and secret not in digest
    assert timedelta(days=29) < expires - datetime.now(UTC) <= timedelta(days=30)
    repository.authenticate.return_value = True
    assert await service.authenticate("Bearer " + secret) == tenant
    repository.authenticate.assert_awaited_once_with(tenant, digest)
    repository.authenticate.return_value = False
    with pytest.raises(AuthenticationFailedError):
        await service.authenticate("Bearer " + secret)


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "Basic guessed",
        "Bearer guessed",
        "Bearer ec_muse_" + "f" * 32 + "." + "x" * 44,
    ],
)
async def test_malformed_credentials_do_not_open_a_tenant_context(authorization):
    service, repository, _ = service_for()
    with pytest.raises(AuthenticationFailedError):
        await service.authenticate(authorization)
    repository.authenticate.assert_not_awaited()


@pytest.mark.parametrize(
    "url",
    [
        "http://lu.ma/event",
        "https://lu.ma.evil.test/event",
        "https://evil.test/event",
        "https://user:secret@lu.ma/event",
        "https://lu.ma:444/event",
        "https://lu.ma/",
        "https://lu.ma/event#secret",
        "https://lu.ma/event\n",
        "https://lu.ma/event\x7f",
        "https://lu.ma/\\evil",
        "https://lu.ma/évent",
    ],
)
def test_provider_links_are_bounded_credential_free_and_not_redirect_targets(url):
    with pytest.raises(ValueError):
        provider_url(url)


@pytest.mark.parametrize(
    "url",
    ["https://lu.ma/event", "https://luma.com/event", "https://www.meetup.com/group/events/123/"],
)
async def test_batch_uses_published_occurrence_and_only_bounded_public_facts(url):
    browse = published_event(url)
    service, _, _ = service_for(browse)
    batch = await service.prepare(uuid4(), uuid4(), [browse.canonical_event.canonical_event_id])
    item = batch.items[0]
    assert item.status == "queued" and item.attempt_id is None
    assert item.event.registration_url == url and item.event.price_status == "free"
    assert "description" not in item.event.model_dump()
    assert "tenant_id" not in batch.model_dump()


@pytest.mark.parametrize(
    "changes",
    [
        {"price_status": PriceStatus.PAID},
        {"price_status": PriceStatus.UNKNOWN},
        {"event_status": EventStatus.CANCELLED},
        {"registration_status": RegistrationStatus.SOLD_OUT},
        {"start_at": datetime(2020, 1, 1, tzinfo=UTC)},
    ],
)
async def test_ineligible_event_is_rejected_before_saving(changes):
    browse = published_event()
    browse = replace(browse, canonical_event=replace(browse.canonical_event, **changes))
    service, repository, _ = service_for(browse)
    with pytest.raises(ValueError):
        await service.prepare(uuid4(), uuid4(), [browse.canonical_event.canonical_event_id])
    repository.create.assert_not_awaited()


@pytest.mark.parametrize(
    "age,url",
    [
        (timedelta(hours=49), "https://lu.ma/event"),
        (timedelta(minutes=-10), "https://lu.ma/event"),
        (timedelta(hours=1), "https://unreviewed.test/event"),
    ],
)
async def test_stale_or_unsupported_source_cannot_be_sent_to_muse(age, url):
    browse = published_event(url, age)
    service, repository, _ = service_for(browse)
    with pytest.raises(ValueError, match="recently collected"):
        await service.prepare(uuid4(), uuid4(), [browse.canonical_event.canonical_event_id])
    repository.create.assert_not_awaited()


async def test_selection_is_bounded_and_idempotent_even_if_catalog_later_changes():
    browse = published_event()
    event_id = browse.canonical_event.canonical_event_id
    service, repository, catalog = service_for(browse)
    tenant, request = uuid4(), uuid4()
    for ids in ([], [event_id, event_id], [uuid4() for _ in range(6)]):
        with pytest.raises(ValueError):
            await service.prepare(tenant, request, ids)
    repository.create.assert_not_awaited()
    batch = await service.prepare(tenant, request, [event_id])
    repository.by_request.return_value = batch
    catalog.get_browse_event.reset_mock()
    assert await service.prepare(tenant, request, [event_id]) == batch
    catalog.get_browse_event.assert_not_awaited()
    with pytest.raises(MuseConflictError):
        await service.prepare(tenant, request, [uuid4()])


@pytest.mark.parametrize(
    "change", ["price", "title", "start", "end", "venue", "city", "stale", "unpublished"]
)
async def test_claim_rechecks_facts_before_a_browser_attempt(change):
    browse = published_event()
    service, repository, catalog = service_for(browse)
    tenant, request, attempt = uuid4(), uuid4(), uuid4()
    event_id = browse.canonical_event.canonical_event_id
    batch = await service.prepare(tenant, request, [event_id])
    repository.batch.return_value = batch
    updates = {
        "price": {"price_status": PriceStatus.PAID},
        "title": {"title": "Political Fundraiser"},
        "start": {"start_at": browse.canonical_event.start_at + timedelta(days=1)},
        "end": {"end_at": None},
        "venue": {"venue_name": "Changed"},
        "city": {"city_norm": "Los Angeles"},
    }
    if change in updates:
        catalog.get_browse_event.return_value = replace(
            browse, canonical_event=replace(browse.canonical_event, **updates[change])
        )
    elif change == "stale":
        catalog.get_browse_event.return_value = replace(
            browse,
            sources=(
                replace(browse.sources[0], last_seen_at=datetime.now(UTC) - timedelta(days=3)),
            ),
        )
    else:
        catalog.get_browse_event.return_value = None
    with pytest.raises(MuseConflictError):
        await service.claim(tenant, batch.batch_id, event_id, attempt)
    repository.claim.assert_not_awaited()


async def test_claim_replay_returns_existing_progress_and_foreign_attempt_is_held():
    browse = published_event()
    service, repository, catalog = service_for(browse)
    tenant, attempt = uuid4(), uuid4()
    event_id = browse.canonical_event.canonical_event_id
    batch = await service.prepare(tenant, uuid4(), [event_id])
    active = batch.items[0].model_copy(update={"status": "uncertain", "attempt_id": attempt})
    repository.batch.return_value = batch.model_copy(update={"items": [active]})
    catalog.get_browse_event.reset_mock()
    assert await service.claim(tenant, batch.batch_id, event_id, attempt) == active
    with pytest.raises(MuseConflictError):
        await service.claim(tenant, batch.batch_id, event_id, uuid4())
    catalog.get_browse_event.assert_not_awaited()
    repository.claim.assert_not_awaited()
    repository.batch.return_value = None
    with pytest.raises(MuseNotFoundError):
        await service.claim(uuid4(), batch.batch_id, event_id, attempt)


def test_registration_confirmation_and_waitlist_are_not_interchangeable():
    with pytest.raises(ValidationError):
        SignupOutcome(status="registered")
    with pytest.raises(ValidationError):
        SignupOutcome(status="registered", confirmation_reference="   ")
    assert SignupOutcome(status="waitlisted").confirmation_reference is None
    assert SignupOutcome(status="uncertain").status == "uncertain"
    assert (
        SignupOutcome(status="registered", confirmation_reference="provider-rsvp-123").status
        == "registered"
    )


async def test_queue_replay_returns_saved_registration_without_catalog_revalidation():
    service, repository, catalog = service_for()
    batch = await service.prepare(uuid4(), uuid4(), [uuid4()])
    registration = SignupRegistration(
        **batch.items[0].model_dump(),
        batch_id=batch.batch_id,
        created_at=batch.created_at,
        version=1,
        unread=False,
    )
    repository.queue_registration.return_value = registration
    catalog.get_browse_event.reset_mock()
    tenant, request = uuid4(), uuid4()
    event_id = registration.event.canonical_event_id
    assert await service.queue(tenant, request, event_id) == registration
    repository.queue_registration.assert_awaited_once_with(tenant, request, event_id)
    catalog.get_browse_event.assert_not_awaited()


async def test_queue_validates_new_event_and_saves_only_published_facts():
    browse = published_event()
    service, repository, _ = service_for(browse)
    repository.queue_registration.side_effect = [MuseNotFoundError(), "queued-result"]
    tenant, request, event_id = uuid4(), uuid4(), browse.canonical_event.canonical_event_id
    assert await service.queue(tenant, request, event_id) == "queued-result"
    args = repository.queue_registration.await_args.args
    assert args[:3] == (tenant, request, event_id)
    assert args[3].registration_url == browse.sources[0].registration_url
    assert args[3].title == browse.canonical_event.title
    assert "description" not in args[3].model_dump()


async def test_queue_conflicting_request_is_rejected_without_new_selection():
    service, repository, catalog = service_for()
    repository.queue_registration.side_effect = MuseConflictError("different selection")
    with pytest.raises(MuseConflictError):
        await service.queue(uuid4(), uuid4(), uuid4())
    catalog.get_browse_event.assert_not_awaited()
