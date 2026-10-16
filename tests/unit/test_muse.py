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
    SignupRegistrations,
    SignupStatusCounts,
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
        "http://partiful.com/e/event",
        "https://partiful.com.evil.test/e/event",
        "https://%70artiful.com/e/review_test",
        "https://partiful%2ecom/e/review_test",
        "https://www.%70artiful.com/e/review_test",
        "https://user:secret@partiful.com/e/event",
        "https://partiful.com:444/e/event",
        "https://partiful.com/",
        "https://partiful.com/login?redirect=/e/event",
        "https://partiful.com/e/",
        "https://partiful.com/e/event/guests",
        "https://partiful.com/e/../login",
        "https://partiful.com/e/event%2Fother",
        "https://partiful.com/e/event#secret",
        "https://partiful.com/e/event\n",
        "https://partiful.com/e/\\evil",
        "https://partiful.com/e/évent",
    ],
)
def test_provider_links_are_bounded_credential_free_and_not_redirect_targets(url):
    with pytest.raises(ValueError):
        provider_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://lu.ma/event", "https://luma.com/event", "https://www.meetup.com/group/events/123/",
        "https://partiful.com/e/Party_123-ab", "https://www.partiful.com/e/event/?c=invite",
        "https://partiful.com:443/e/event",
    ],
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


@pytest.mark.parametrize("url", ["https://lu.ma/muse-test", "https://partiful.com/e/muse-test"])
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
async def test_ineligible_event_is_rejected_before_saving(changes, url):
    browse = published_event(url)
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
        (timedelta(hours=49), "https://partiful.com/e/event"),
        (timedelta(minutes=-10), "https://partiful.com/e/event"),
        (timedelta(hours=1), "https://partiful.com/login"),
    ],
)
async def test_stale_or_unsupported_source_cannot_be_sent_to_muse(age, url):
    browse = published_event(url, age)
    service, repository, _ = service_for(browse)
    with pytest.raises(ValueError, match="recently collected"):
        await service.prepare(uuid4(), uuid4(), [browse.canonical_event.canonical_event_id])
    repository.create.assert_not_awaited()


@pytest.mark.parametrize("url", ["https://lu.ma/muse-test", "https://partiful.com/e/muse-test"])
async def test_selection_is_bounded_and_idempotent_even_if_catalog_later_changes(url):
    browse = published_event(url)
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


@pytest.mark.parametrize("url", ["https://lu.ma/muse-test", "https://partiful.com/e/muse-test"])
@pytest.mark.parametrize(
    "change", ["price", "title", "start", "end", "venue", "city", "stale", "future", "url", "unpublished"]
)
async def test_claim_rechecks_facts_before_a_browser_attempt(change, url):
    browse = published_event(url)
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
    elif change in {"future", "url"}:
        updates = (
            {"last_seen_at": datetime.now(UTC) + timedelta(minutes=10)}
            if change == "future" else {"registration_url": url + "-different"}
        )
        catalog.get_browse_event.return_value = replace(
            browse, sources=(replace(browse.sources[0], **updates),),
        )
    else:
        catalog.get_browse_event.return_value = None
    with pytest.raises(MuseConflictError):
        await service.claim(tenant, batch.batch_id, event_id, attempt)
    repository.claim.assert_not_awaited()


@pytest.mark.parametrize("url", [
    "https://lu.ma/muse-test", "https://www.meetup.com/group/events/123/",
    "https://partiful.com/e/muse-test",
])
async def test_unchanged_eligible_event_can_be_claimed_with_its_provider_snapshot(url):
    browse = published_event(url)
    service, repository, _ = service_for(browse)
    tenant, attempt, event_id = uuid4(), uuid4(), browse.canonical_event.canonical_event_id
    batch = await service.prepare(tenant, uuid4(), [event_id])
    repository.batch.return_value = batch
    claimed = batch.items[0].model_copy(update={"status": "in_progress", "attempt_id": attempt})
    repository.claim.return_value = claimed
    assert await service.claim(tenant, batch.batch_id, event_id, attempt) == claimed
    assert claimed.event.registration_url == url
    repository.claim.assert_awaited_once_with(tenant, batch.batch_id, event_id, attempt)


@pytest.mark.parametrize("url", ["https://lu.ma/muse-test", "https://partiful.com/e/muse-test"])
async def test_claim_replay_returns_existing_progress_and_foreign_attempt_is_held(url):
    browse = published_event(url)
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


def test_partiful_confirmation_evidence_requires_the_reviewed_event_path():
    evidence = "https://partiful.com/e/confirmed_event?c=invite"
    assert SignupOutcome(status="registered", evidence_url=evidence).evidence_url == evidence
    with pytest.raises(ValidationError):
        SignupOutcome(status="registered", evidence_url="https://partiful.com/login")
    with pytest.raises(ValidationError):
        SignupOutcome(status="registered", evidence_url="https://partiful.com.evil.test/e/event")


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


@pytest.mark.parametrize("url", ["https://lu.ma/muse-test", "https://partiful.com/e/muse-test"])
async def test_queue_validates_new_event_and_saves_only_published_facts(url):
    browse = published_event(url)
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


def test_registration_counts_include_zero_categories_and_cover_the_entire_queue():
    counts = SignupStatusCounts(queued=50, registered=3, needs_input=2)
    result = SignupRegistrations(items=[], total=55, unread_count=5, status_counts=counts)
    assert result.status_counts.model_dump() == {
        "queued": 50,
        "in_progress": 0,
        "needs_input": 2,
        "awaiting_approval": 0,
        "waitlisted": 0,
        "registered": 3,
        "failed": 0,
        "uncertain": 0,
    }


@pytest.mark.parametrize("status", list(SignupStatusCounts.model_fields))
def test_registration_status_counts_reject_negative_categories(status):
    with pytest.raises(ValidationError):
        SignupStatusCounts.model_validate({status: -1})


@pytest.mark.parametrize("value", [True, 1.5, "2"])
def test_registration_status_counts_require_integer_counts(value):
    with pytest.raises(ValidationError):
        SignupStatusCounts(queued=value)


@pytest.mark.parametrize("total, unread", [(51, 0), (50, 51)])
def test_registration_totals_cannot_disagree_with_status_or_unread_counts(total, unread):
    with pytest.raises(ValidationError):
        SignupRegistrations(
            items=[], total=total, unread_count=unread, status_counts=SignupStatusCounts(queued=50)
        )
