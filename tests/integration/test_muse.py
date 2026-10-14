"""Real RLS, credential expiry, attempt races and erasure on a disposable database."""

import asyncio
import os
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.support.integration_database import (
    isolated_database_name,
    replace_database,
    validate_isolated_test_environment,
)
from tests.support.run_isolated_integration import _create_database, _drop_database
from tests.unit.test_muse import published_event

from events_concierge.adapters.postgres.account_erasure import PostgresAccountErasureRepository
from events_concierge.adapters.postgres.muse import PostgresMuseRepository
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.application.muse import MuseSignupService
from events_concierge.domain.account_erasure import AccountErasureStage
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.muse import (
    MuseConflictError,
    MuseNotFoundError,
    RegistrationAccountUpdate,
    SeenRegistration,
    SignupEvent,
    SignupOutcome,
    SignupStatusCounts,
)
from events_concierge.infra.db import (
    dispose_engine,
    init_engine,
    system_session_scope,
    tenant_session_scope,
)
from events_concierge.ports.auth import AuthenticationFailedError

pytestmark = pytest.mark.integration


async def account():
    tenant_id = uuid4()
    await PostgresTenantRepository().add(
        Tenant(
            tenant_id=tenant_id,
            oidc_subject=f"muse|{tenant_id}",
            notify_email=f"muse-{tenant_id}@example.test",
            relay_inbox=f"{tenant_id}@relay.example.test",
        )
    )
    return tenant_id


def selected_event():
    event = published_event()
    return SignupEvent(
        canonical_event_id=event.canonical_event.canonical_event_id,
        title=event.canonical_event.title,
        start_at=event.canonical_event.start_at,
        end_at=event.canonical_event.end_at,
        venue_name=event.canonical_event.venue_name,
        city=event.canonical_event.city_norm,
        registration_url=event.sources[0].registration_url,
        observed_at=event.sources[0].last_seen_at,
    )


async def test_key_rotation_expiry_revocation_and_cross_account_isolation(db):
    tenant, other = await account(), await account()
    repository = PostgresMuseRepository()
    # Authentication and expiry are repository concerns; no catalog call is needed here.
    service = MuseSignupService(repository, None)  # type: ignore[arg-type]
    secret, _ = await service.connect(tenant)
    assert await service.authenticate("Bearer " + secret) == tenant
    replacement, _ = await service.connect(tenant)
    with pytest.raises(AuthenticationFailedError):
        await service.authenticate("Bearer " + secret)
    assert await service.authenticate("Bearer " + replacement) == tenant
    assert not await repository.authenticate(other, "f" * 64)
    async with tenant_session_scope(tenant) as session:
        stored = (
            await session.execute(text("SELECT token_hash FROM muse_connections"))
        ).scalar_one()
        assert secret not in stored and replacement not in stored and len(stored) == 64
        await session.execute(
            text("UPDATE muse_connections SET expires_at = clock_timestamp()-interval '1 second'")
        )
    with pytest.raises(AuthenticationFailedError):
        await service.authenticate("Bearer " + replacement)
    replacement, _ = await service.connect(tenant)
    await repository.revoke(tenant)
    assert not (await repository.connection(tenant)).connected
    with pytest.raises(AuthenticationFailedError):
        await service.authenticate("Bearer " + replacement)


async def test_batches_rls_and_atomic_duplicate_selection(db):
    tenant, other = await account(), await account()
    repository, event = PostgresMuseRepository(), selected_event()
    request_id = uuid4()
    batch = await repository.create(tenant, request_id, [event])
    assert await repository.create(tenant, request_id, [event]) == batch
    assert await repository.batch(other, batch.batch_id) is None
    assert await repository.batches(other) == []
    async with tenant_session_scope(other) as session:
        assert (
            await session.execute(text("SELECT count(*) FROM muse_signup_items"))
        ).scalar_one() == 0
    async with system_session_scope() as session:
        assert (
            await session.execute(text("SELECT count(*) FROM muse_signup_batches"))
        ).scalar_one() == 0
    with pytest.raises(DBAPIError):
        async with tenant_session_scope(other) as session:
            await session.execute(
                text("""
            INSERT INTO muse_signup_batches (batch_id,tenant_id,request_id)
            VALUES (:batch,:tenant,:request)
        """),
                {"batch": uuid4(), "tenant": tenant, "request": uuid4()},
            )
    different_event = selected_event()
    with pytest.raises(MuseConflictError):
        await repository.create(tenant, request_id, [different_event])
    new_event = selected_event()
    results = await asyncio.gather(
        repository.create(tenant, uuid4(), [new_event]),
        repository.create(tenant, uuid4(), [new_event]),
        return_exceptions=True,
    )
    assert sum(isinstance(result, MuseConflictError) for result in results) == 1
    assert len(await repository.batches(tenant)) == 2


async def test_racing_claims_have_one_owner_and_uncertainty_never_restarts(db):
    tenant = await account()
    repository, event = PostgresMuseRepository(), selected_event()
    batch = await repository.create(tenant, uuid4(), [event])
    attempts = [uuid4(), uuid4()]
    results = await asyncio.gather(
        *[
            repository.claim(tenant, batch.batch_id, event.canonical_event_id, attempt)
            for attempt in attempts
        ],
        return_exceptions=True,
    )
    assert sum(isinstance(result, MuseConflictError) for result in results) == 1
    current = await repository.batch(tenant, batch.batch_id)
    assert current is not None
    attempt = current.items[0].attempt_id
    assert attempt is not None
    uncertain = SignupOutcome(status="uncertain", note="Submission acknowledgment was lost")
    result = await repository.report(
        tenant, batch.batch_id, event.canonical_event_id, attempt, uncertain
    )
    assert result.status == "uncertain"
    assert (
        await repository.claim(tenant, batch.batch_id, event.canonical_event_id, attempt) == result
    )
    with pytest.raises(MuseConflictError):
        await repository.claim(tenant, batch.batch_id, event.canonical_event_id, uuid4())
    with pytest.raises(MuseConflictError):
        await repository.create(tenant, uuid4(), [event])
    with pytest.raises(MuseConflictError):
        await repository.report(
            tenant, batch.batch_id, event.canonical_event_id, uuid4(), uncertain
        )
    confirmed = SignupOutcome(status="registered", confirmation_reference="provider-rsvp-123")
    result = await repository.report(
        tenant, batch.batch_id, event.canonical_event_id, attempt, confirmed
    )
    assert (
        await repository.report(
            tenant, batch.batch_id, event.canonical_event_id, attempt, confirmed
        )
        == result
    )
    with pytest.raises(MuseConflictError):
        await repository.report(
            tenant, batch.batch_id, event.canonical_event_id, attempt, uncertain
        )


async def test_account_erasure_fences_auth_and_cascades_muse_state(db):
    tenant, survivor = await account(), await account()
    repository, event = PostgresMuseRepository(), selected_event()
    await repository.connect(tenant, "f" * 64, datetime.now(UTC) + timedelta(days=1))
    await repository.queue_registration(tenant, uuid4(), event.canonical_event_id, event)
    survivor_batch = await repository.create(survivor, uuid4(), [selected_event()])
    erasure = PostgresAccountErasureRepository()
    request_id = uuid4()
    await erasure.begin(tenant, request_id)
    with pytest.raises(AuthenticationFailedError):
        await repository.authenticate(tenant, "f" * 64)
    with pytest.raises(AuthenticationFailedError):
        await repository.create(tenant, uuid4(), [selected_event()])
    with pytest.raises(AuthenticationFailedError):
        await repository.registrations(tenant)
    with pytest.raises(AuthenticationFailedError):
        await repository.registrations_for_events(tenant, [event.canonical_event_id])
    with pytest.raises(AuthenticationFailedError):
        await repository.queue_registration(tenant, uuid4(), event.canonical_event_id)
    # Raw app-role DML is fenced too, even if a future adapter omits its preflight.
    with pytest.raises(DBAPIError, match="fenced for erasure"):
        async with tenant_session_scope(tenant) as session:
            await session.execute(text("UPDATE muse_connections SET revoked_at=clock_timestamp()"))
    for stage, count in (
        (AccountErasureStage.EXTERNAL_EFFECTS, 1),
        (AccountErasureStage.WORKFLOWS, 0),
        (AccountErasureStage.CALENDAR, 0),
        (AccountErasureStage.BROWSER_SESSIONS, 1),
        (AccountErasureStage.CREDENTIAL_VAULT, 1),
        (AccountErasureStage.OBJECT_STORE, 1),
    ):
        await erasure.complete_stage(tenant, request_id, stage, count)
    await erasure.finalize(tenant, request_id)
    async with tenant_session_scope(tenant) as session:
        for table in (
            "muse_connections",
            "muse_signup_batches",
            "muse_signup_items",
            "muse_signup_requests",
        ):
            assert (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0
    assert await repository.batch(survivor, survivor_batch.batch_id) == survivor_batch


async def test_queue_duplicate_clicks_requests_and_concurrent_accounts_are_idempotent(db):
    tenant, other = await account(), await account()
    repository, event = PostgresMuseRepository(), selected_event()
    request = uuid4()
    results = await asyncio.gather(
        *[
            repository.queue_registration(tenant, candidate, event.canonical_event_id, event)
            for candidate in (request, request, uuid4(), uuid4())
        ]
    )
    assert all(result == results[0] for result in results)
    assert results[0].version == 1 and not results[0].unread
    assert len(await repository.batches(tenant)) == 1
    assert (await repository.registrations(tenant)).total == 1
    alias = uuid4()
    assert (
        await repository.queue_registration(tenant, alias, event.canonical_event_id) == results[0]
    )
    for reused in (request, alias):
        different = selected_event()
        with pytest.raises(MuseConflictError, match="different selection"):
            await repository.queue_registration(
                tenant, reused, different.canonical_event_id, different
            )
        with pytest.raises(MuseConflictError):
            await repository.create(tenant, reused, [different])
    other_registration = await repository.queue_registration(
        other, request, event.canonical_event_id, event
    )
    assert other_registration.batch_id != results[0].batch_id
    async with tenant_session_scope(other) as session:
        assert (
            await session.execute(text("SELECT count(*) FROM muse_signup_requests"))
        ).scalar_one() == 1
    with pytest.raises(DBAPIError):
        async with tenant_session_scope(other) as session:
            await session.execute(
                text("""
                INSERT INTO muse_signup_requests (tenant_id, request_id, canonical_event_id)
                VALUES (:tenant, :request, :event)
            """),
                {"tenant": tenant, "request": uuid4(), "event": event.canonical_event_id},
            )


async def test_progress_versions_preserve_new_updates_during_acknowledgement_races(db):
    tenant = await account()
    repository, event, attempt = PostgresMuseRepository(), selected_event(), uuid4()
    queued = await repository.queue_registration(tenant, uuid4(), event.canonical_event_id, event)
    await repository.claim(tenant, queued.batch_id, event.canonical_event_id, attempt)
    claimed = (await repository.registrations(tenant)).items[0]
    assert claimed.version == 2 and claimed.unread
    await repository.claim(tenant, queued.batch_id, event.canonical_event_id, attempt)
    assert (await repository.registrations(tenant)).items[0] == claimed
    outcome = SignupOutcome(status="needs_input", note="Sign in to the provider")
    await asyncio.gather(
        repository.report(tenant, queued.batch_id, event.canonical_event_id, attempt, outcome),
        repository.see_registrations(
            tenant, [SeenRegistration(event_id=event.canonical_event_id, version=2)]
        ),
    )
    updated = (await repository.registrations(tenant)).items[0]
    assert updated.status == "needs_input" and updated.version == 3 and updated.unread
    await repository.report(tenant, queued.batch_id, event.canonical_event_id, attempt, outcome)
    assert (await repository.registrations(tenant)).items[0] == updated
    await repository.see_registrations(
        tenant, [SeenRegistration(event_id=event.canonical_event_id, version=3)]
    )
    await repository.see_registrations(
        tenant, [SeenRegistration(event_id=event.canonical_event_id, version=2)]
    )
    seen = await repository.registrations(tenant)
    assert not seen.items[0].unread and seen.unread_count == 0 and seen.items[0].version == 3


async def test_seen_validation_is_atomic_and_never_acknowledges_foreign_or_future_state(db):
    tenant, other = await account(), await account()
    repository, first, second = PostgresMuseRepository(), selected_event(), selected_event()
    queued = await repository.queue_registration(tenant, uuid4(), first.canonical_event_id, first)
    await repository.claim(tenant, queued.batch_id, first.canonical_event_id, uuid4())
    await repository.queue_registration(other, uuid4(), second.canonical_event_id, second)
    valid = SeenRegistration(event_id=first.canonical_event_id, version=2)
    for invalid, error in (
        (SeenRegistration(event_id=second.canonical_event_id, version=1), MuseNotFoundError),
        (SeenRegistration(event_id=first.canonical_event_id, version=3), ValueError),
    ):
        with pytest.raises(error):
            await repository.see_registrations(
                tenant, [valid, invalid] if error is MuseNotFoundError else [invalid]
            )
        assert (await repository.registrations(tenant)).unread_count == 1
    with pytest.raises(ValueError, match="distinct"):
        await repository.see_registrations(tenant, [valid, valid])
    assert (await repository.registrations(other)).unread_count == 0


async def test_queue_and_connector_pages_cover_more_than_fifty_tasks_and_reject_foreign_cursors(db):
    tenant, other = await account(), await account()
    repository = PostgresMuseRepository()
    empty = await repository.registrations(other)
    assert empty.total == 0 and empty.status_counts == SignupStatusCounts()
    events = [selected_event() for _ in range(55)]
    # Saved history remains readable after the event date has passed.
    events[0] = events[0].model_copy(
        update={
            "start_at": datetime(2020, 1, 1, tzinfo=UTC),
            "end_at": datetime(2020, 1, 1, 2, tzinfo=UTC),
        }
    )
    queued = []
    for event in events:
        queued.append(
            await repository.queue_registration(tenant, uuid4(), event.canonical_event_id, event)
        )
    statuses = [
        "registered",
        "needs_input",
        "awaiting_approval",
        "waitlisted",
        "in_progress",
        "failed",
        "uncertain",
    ]
    for registration, status in zip(queued, statuses, strict=False):
        attempt = uuid4()
        event_id = registration.event.canonical_event_id
        await repository.claim(tenant, registration.batch_id, event_id, attempt)
        if status != "in_progress":
            await repository.report(
                tenant,
                registration.batch_id,
                event_id,
                attempt,
                SignupOutcome(
                    status=status,
                    confirmation_reference="provider-rsvp-stats"
                    if status == "registered"
                    else None,
                ),
            )
    expected = SignupStatusCounts(queued=48, **dict.fromkeys(statuses, 1))
    page = await repository.registrations(tenant)
    assert len(page.items) == 50 and page.total == 55 and page.next_cursor is not None
    assert page.status_counts == expected and page.unread_count == 7
    assert not any(item.status == "registered" for item in page.items)
    remainder = await repository.registrations(tenant, cursor=page.next_cursor)
    assert len(remainder.items) == 5 and remainder.total == 55 and remainder.next_cursor is None
    assert remainder.status_counts == expected and remainder.unread_count == 7
    assert any(item.status == "registered" for item in remainder.items)
    older_id = events[0].canonical_event_id
    visible = await repository.registrations_for_events(tenant, [older_id, uuid4()])
    assert len(visible) == 1 and visible[0].event.canonical_event_id == older_id
    assert visible[0].event.start_at < datetime.now(UTC) and visible[0].status == "registered"
    assert visible[0].unread and visible[0].version == 3
    assert (await repository.registrations(tenant)).unread_count == page.unread_count
    assert await repository.registrations_for_events(other, [older_id]) == []
    for invalid in ([], [older_id, older_id], [uuid4() for _ in range(101)]):
        with pytest.raises(ValueError, match="distinct"):
            await repository.registrations_for_events(tenant, invalid)
    assert (
        len(
            await repository.registrations_for_events(
                tenant,
                [event.canonical_event_id for event in events] + [uuid4() for _ in range(45)],
            )
        )
        == 55
    )
    ids = [item.event.canonical_event_id for item in page.items + remainder.items]
    assert len(set(ids)) == 55 and set(ids) == {event.canonical_event_id for event in events}
    batches = await repository.batches(tenant)
    more_batches = await repository.batches(tenant, cursor=batches[-1].batch_id)
    assert len(batches) == 50 and len(more_batches) == 5
    assert len({batch.batch_id for batch in batches + more_batches}) == 55
    other_event = selected_event()
    foreign = await repository.queue_registration(
        other, uuid4(), other_event.canonical_event_id, other_event
    )
    with pytest.raises(MuseNotFoundError):
        await repository.registrations(tenant, cursor=foreign.event.canonical_event_id)
    with pytest.raises(MuseNotFoundError):
        await repository.batches(tenant, cursor=foreign.batch_id)
    with pytest.raises(MuseNotFoundError):
        await repository.registrations(tenant, cursor=uuid4())
    isolated = await repository.registrations(other)
    assert isolated.total == 1 and isolated.status_counts == SignupStatusCounts(queued=1)


async def test_legacy_batches_appear_in_queue_and_keep_connector_attempt_ownership(db):
    tenant = await account()
    repository, first, second = PostgresMuseRepository(), selected_event(), selected_event()
    original_request = uuid4()
    batch = await repository.create(tenant, original_request, [first, second])
    page = await repository.registrations(tenant)
    assert page.total == 2 and all(item.batch_id == batch.batch_id for item in page.items)
    assert all(item.version == 1 and not item.unread for item in page.items)
    alias = uuid4()
    existing = await repository.queue_registration(tenant, alias, first.canonical_event_id)
    assert existing.batch_id == batch.batch_id
    assert len(await repository.batches(tenant)) == 1
    for events in ([first], [first, second], [second]):
        with pytest.raises(MuseConflictError, match="individual registration"):
            await repository.create(tenant, alias, events)
    with pytest.raises(MuseConflictError, match="individual registration"):
        await repository.by_request(tenant, alias)
    with pytest.raises(MuseConflictError):
        await repository.queue_registration(tenant, original_request, first.canonical_event_id)
    attempt = uuid4()
    claimed = await repository.claim(tenant, batch.batch_id, first.canonical_event_id, attempt)
    assert claimed.status == "in_progress"
    with pytest.raises(MuseConflictError):
        await repository.claim(tenant, batch.batch_id, first.canonical_event_id, uuid4())
    assert (await repository.registrations(tenant)).unread_count == 1


async def _migrate_muse_test_database(environ, direction, revision):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "alembic", direction, revision, env=environ,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    assert process.returncode == 0, (stdout.decode(), stderr.decode())


@pytest.fixture
async def muse_migration_db(db):
    """A whole-schema downgrade must not cross another feature's retained fixtures."""
    run_id = uuid4().hex
    database = isolated_database_name(run_id)
    owner, app = os.environ["EC_MIGRATION_URL"], os.environ["EC_DATABASE_URL"]
    child = {**os.environ, "EC_INTEGRATION_TEST_RUN_ID": run_id,
             "EC_MIGRATION_URL": replace_database(owner, database),
             "EC_DATABASE_URL": replace_database(app, database)}
    validate_isolated_test_environment(child)
    _create_database(owner, database)
    try:
        await _migrate_muse_test_database(child, "upgrade", "head")
        await dispose_engine()
        init_engine(child["EC_DATABASE_URL"])
        yield child
    finally:
        await dispose_engine()
        _drop_database(owner, database)
        init_engine(app)


async def test_queue_migration_preserves_existing_batches_and_starts_notifications_seen(muse_migration_db):
    tenant = await account()
    repository, event = PostgresMuseRepository(), selected_event()
    batch = await repository.create(tenant, uuid4(), [event])
    attempt = uuid4()
    await repository.claim(tenant, batch.batch_id, event.canonical_event_id, attempt)
    await repository.report(
        tenant,
        batch.batch_id,
        event.canonical_event_id,
        attempt,
        SignupOutcome(status="needs_input", note="Provider login required"),
    )
    existing = await repository.batch(tenant, batch.batch_id)
    # Model a retained pre-0219 attempt: it never had an account snapshot. The 0219
    # downgrade must refuse to destroy snapshots created by the new application.
    async with tenant_session_scope(tenant) as session:
        await session.execute(text("UPDATE muse_signup_items SET registration_account = NULL"))
    # The isolated schema holds only this test's Muse history, never another feature's records.
    for direction, revision in (("downgrade", "0213"), ("upgrade", "head")):
        await _migrate_muse_test_database(muse_migration_db, direction, revision)
    assert await repository.batch(tenant, batch.batch_id) == existing
    upgraded = (await repository.registrations(tenant)).items[0]
    assert upgraded.status == "needs_input" and upgraded.attempt_id == attempt
    assert upgraded.version == 1 and not upgraded.unread
    assert (
        await repository.queue_registration(tenant, uuid4(), event.canonical_event_id) == upgraded
    )


@pytest.mark.parametrize("retained", ["preference", "snapshot"])
async def test_registration_account_downgrade_refuses_retained_data(muse_migration_db, retained):
    tenant, repository = await account(), PostgresMuseRepository()
    if retained == "preference":
        await repository.replace_registration_account(
            tenant, "muse", RegistrationAccountUpdate(
                expected_email="provider@example.test", provider_overrides={}, revision=1
            ),
        )
    else:
        event = selected_event()
        await repository.queue_registration(tenant, uuid4(), event.canonical_event_id, event)
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "alembic", "downgrade", "0218", env=muse_migration_db,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await process.communicate()
    assert process.returncode != 0
    assert b"registration account data is retained" in stderr
    if retained == "preference":
        assert (await repository.registration_account(tenant, "muse")).expected_email == "provider@example.test"
    else:
        assert (await repository.registrations(tenant)).total == 1
