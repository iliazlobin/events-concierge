"""Real RLS, credential expiry, attempt races and erasure on a disposable database."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.unit.test_muse import published_event

from events_concierge.adapters.postgres.account_erasure import PostgresAccountErasureRepository
from events_concierge.adapters.postgres.muse import PostgresMuseRepository
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.application.muse import MuseSignupService
from events_concierge.domain.account_erasure import AccountErasureStage
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.muse import MuseConflictError, SignupEvent, SignupOutcome
from events_concierge.infra.db import system_session_scope, tenant_session_scope
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
    await repository.create(tenant, uuid4(), [event])
    survivor_batch = await repository.create(survivor, uuid4(), [selected_event()])
    erasure = PostgresAccountErasureRepository()
    request_id = uuid4()
    await erasure.begin(tenant, request_id)
    with pytest.raises(AuthenticationFailedError):
        await repository.authenticate(tenant, "f" * 64)
    with pytest.raises(AuthenticationFailedError):
        await repository.create(tenant, uuid4(), [selected_event()])
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
        for table in ("muse_connections", "muse_signup_batches", "muse_signup_items"):
            assert (await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one() == 0
    assert await repository.batch(survivor, survivor_batch.batch_id) == survivor_batch
