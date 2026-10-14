"""Real tenant isolation, erasure and serialization of mapping/queue/claim races."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.integration.test_muse import account, selected_event

from events_concierge.adapters.postgres.account_erasure import PostgresAccountErasureRepository
from events_concierge.adapters.postgres.muse import PostgresMuseRepository
from events_concierge.domain.account_erasure import AccountErasureStage
from events_concierge.domain.muse import (
    MuseConflictError,
    RegistrationAccountSettings,
    RegistrationAccountUpdate,
    SignupOutcome,
)
from events_concierge.infra.db import system_session_scope, tenant_session_scope
from events_concierge.ports.auth import AuthenticationFailedError

pytestmark = pytest.mark.integration


def preference(email="provider@example.test", revision=1, overrides=None):
    return RegistrationAccountUpdate(
        expected_email=email, provider_overrides=overrides or {}, revision=revision
    )


async def test_mapping_persists_normalizes_and_isolated_from_other_tenants_and_key_revocation(db):
    tenant, other = await account(), await account()
    repo = PostgresMuseRepository()
    assert await repo.registration_account(tenant, "muse") == RegistrationAccountSettings()
    saved = await repo.replace_registration_account(tenant, "muse", preference("Provider@EXAMPLE.TEST"))
    assert saved.expected_email == "Provider@example.test" and saved.updated_at is not None
    assert await repo.registration_account(tenant, "muse") == saved
    assert await repo.registration_account(other, "muse") == RegistrationAccountSettings()
    for scope in (tenant_session_scope(other), system_session_scope()):
        async with scope as session:
            assert (await session.execute(text("SELECT count(*) FROM registration_account_settings"))).scalar_one() == 0
    with pytest.raises(DBAPIError):
        async with tenant_session_scope(other) as session:
            await session.execute(text("""
                INSERT INTO registration_account_settings(tenant_id,executor,revision)
                VALUES (:tenant,'muse',1)
            """), {"tenant": tenant})
    await repo.connect(tenant, "f" * 64, datetime.now(UTC) + timedelta(days=1))
    await repo.revoke(tenant)
    assert not await repo.authenticate(tenant, "f" * 64)
    assert await repo.registration_account(tenant, "muse") == saved
    for revision in (1, 3):
        with pytest.raises(MuseConflictError):
            await repo.replace_registration_account(tenant, "muse", preference("other@example.test", revision))
    assert await repo.registration_account(tenant, "muse") == saved
    cleared = await repo.replace_registration_account(tenant, "muse", preference(None, 2))
    assert cleared.expected_email is None and cleared.provider_overrides == {} and cleared.revision == 2


async def test_mapping_changes_only_queued_tasks_and_preserves_claim_and_outcome_identity(db):
    tenant, repo = await account(), PostgresMuseRepository()
    first, second, third = selected_event(), selected_event(), selected_event()
    third = third.model_copy(update={"registration_url": "https://meetup.com/example/events/123/"})
    active = await repo.queue_registration(tenant, uuid4(), first.canonical_event_id, first)
    queued = await repo.queue_registration(tenant, uuid4(), second.canonical_event_id, second)
    claimed = await repo.claim(tenant, active.batch_id, first.canonical_event_id, uuid4())
    assert claimed.registration_account.expected_email is None
    await repo.replace_registration_account(tenant, "muse", preference(overrides={"meetup": "meetup@example.test"}))
    updated = (await repo.registrations_for_events(tenant, [second.canonical_event_id]))[0]
    assert updated.registration_account.expected_email == "provider@example.test"
    assert updated.version == queued.version + 1 and updated.unread
    unchanged = (await repo.batch(tenant, active.batch_id)).items[0]
    assert unchanged == claimed
    meetup = await repo.queue_registration(tenant, uuid4(), third.canonical_event_id, third)
    assert meetup.registration_account.provider == "meetup"
    assert meetup.registration_account.expected_email == "meetup@example.test"
    # Saving an identical mapping advances settings revision but creates no task notification.
    await repo.replace_registration_account(tenant, "muse", preference(revision=2, overrides={"meetup": "meetup@example.test"}))
    assert (await repo.registrations_for_events(tenant, [second.canonical_event_id]))[0] == updated
    expected = await repo.claim(tenant, queued.batch_id, second.canonical_event_id, uuid4())
    await repo.replace_registration_account(tenant, "muse", preference("new@example.test", 3))
    assert await repo.claim(tenant, queued.batch_id, second.canonical_event_id, expected.attempt_id) == expected
    outcome = await repo.report(tenant, queued.batch_id, second.canonical_event_id, expected.attempt_id,
                                SignupOutcome(status="registered", confirmation_reference="rsvp-123"))
    assert outcome.registration_account == expected.registration_account


async def test_queue_save_and_claim_race_keeps_the_claimed_snapshot(db):
    tenant, repo, event = await account(), PostgresMuseRepository(), selected_event()
    queued = await repo.queue_registration(tenant, uuid4(), event.canonical_event_id, event)
    attempt = uuid4()
    claimed, saved = await asyncio.gather(
        repo.claim(tenant, queued.batch_id, event.canonical_event_id, attempt),
        repo.replace_registration_account(tenant, "muse", preference()),
    )
    assert saved.expected_email == "provider@example.test"
    assert claimed.registration_account.expected_email in (None, "provider@example.test")
    current = (await repo.batch(tenant, queued.batch_id)).items[0]
    assert current == claimed
    await repo.replace_registration_account(tenant, "muse", preference("changed@example.test", 2))
    assert (await repo.batch(tenant, queued.batch_id)).items[0] == claimed
    second = selected_event()
    _, inserted = await asyncio.gather(
        repo.replace_registration_account(tenant, "muse", preference("latest@example.test", 3)),
        repo.queue_registration(tenant, uuid4(), second.canonical_event_id, second),
    )
    persisted = (await repo.registrations_for_events(tenant, [second.canonical_event_id]))[0]
    assert persisted.registration_account.expected_email == "latest@example.test"
    assert inserted.registration_account.expected_email in ("changed@example.test", "latest@example.test")
    contenders = await asyncio.gather(
        repo.replace_registration_account(tenant, "muse", preference("one@example.test", 4)),
        repo.replace_registration_account(tenant, "muse", preference("two@example.test", 4)),
        return_exceptions=True,
    )
    assert sum(isinstance(value, MuseConflictError) for value in contenders) == 1
    mapping = await repo.registration_account(tenant, "muse")
    assert mapping.revision == 4
    assert (await repo.registrations_for_events(tenant, [second.canonical_event_id]))[0].registration_account.expected_email == mapping.expected_email


async def test_legacy_claimed_identity_is_never_inferred_or_retargeted(db):
    tenant, repo, event = await account(), PostgresMuseRepository(), selected_event()
    queued = await repo.queue_registration(tenant, uuid4(), event.canonical_event_id, event)
    await repo.claim(tenant, queued.batch_id, event.canonical_event_id, uuid4())
    async with tenant_session_scope(tenant) as session:
        await session.execute(text("UPDATE muse_signup_items SET registration_account=NULL"))
    await repo.replace_registration_account(tenant, "muse", preference())
    saved = (await repo.batch(tenant, queued.batch_id)).items[0]
    assert saved.registration_account.expected_email is None
    async with tenant_session_scope(tenant) as session:
        assert (await session.execute(text("SELECT registration_account FROM muse_signup_items"))).scalar_one() is None


async def test_erasure_fences_preferences_and_deletes_the_tenant_satellite(db):
    tenant, survivor = await account(), await account()
    repo, erasure = PostgresMuseRepository(), PostgresAccountErasureRepository()
    await repo.replace_registration_account(tenant, "muse", preference())
    other = await repo.replace_registration_account(survivor, "muse", preference("survivor@example.test"))
    request = uuid4()
    await erasure.begin(tenant, request)
    with pytest.raises(AuthenticationFailedError):
        await repo.registration_account(tenant, "muse")
    with pytest.raises(AuthenticationFailedError):
        await repo.replace_registration_account(tenant, "muse", preference(revision=2))
    with pytest.raises(DBAPIError, match="fenced for erasure"):
        async with tenant_session_scope(tenant) as session:
            await session.execute(text("UPDATE registration_account_settings SET expected_email=NULL"))
    for stage in AccountErasureStage:
        count = 0 if stage in {AccountErasureStage.WORKFLOWS, AccountErasureStage.CALENDAR} else 1
        await erasure.complete_stage(tenant, request, stage, count)
    await erasure.finalize(tenant, request)
    async with tenant_session_scope(tenant) as session:
        assert (await session.execute(text("SELECT count(*) FROM registration_account_settings"))).scalar_one() == 0
    assert await repo.registration_account(survivor, "muse") == other
