"""Real PostgreSQL signup atomicity, RLS, immutable consent, and retained erasure fences."""

import asyncio
from time import time
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from events_concierge.adapters.postgres.account_erasure import PostgresAccountErasureRepository
from events_concierge.adapters.postgres.consumer_accounts import PostgresConsumerAccountRepository
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.domain.account_erasure import AccountErasureStage, AccountErasureStatus
from events_concierge.domain.consumer_identity import LegalPolicy, VerifiedConsumerIdentity
from events_concierge.infra.db import system_session_scope, tenant_session_scope
from events_concierge.ports.auth import AuthenticationFailedError, BrowserSessionUnavailableError

pytestmark = pytest.mark.integration
_POLICY = LegalPolicy(
    "2026-10-05",
    "https://events.example.test/terms/2026-10-05",
    "2026-10-05",
    "https://events.example.test/privacy/2026-10-05",
)


def _identity() -> VerifiedConsumerIdentity:
    return VerifiedConsumerIdentity(
        "events-identity-test",
        uuid4().hex,
        "google.com",
        "shared-contact@example.test",
        int(time()),
    )


async def test_atomic_signup_is_idempotent_and_contact_email_never_merges_accounts(
    db: None,
) -> None:
    accounts = PostgresConsumerAccountRepository()
    assert await accounts.is_ready()
    first, second = _identity(), _identity()
    results = await asyncio.gather(*(accounts.accept(first, _POLICY) for _ in range(4)))
    assert results == [first.tenant_id] * 4
    assert await accounts.accept(second, _POLICY) == second.tenant_id
    for identity in (first, second):
        tenant = await PostgresTenantRepository().get(identity.tenant_id)
        assert (
            tenant
            and tenant.oidc_subject == identity.subject
            and tenant.notify_email == identity.email
        )
        assert await accounts.has_accepted(identity.tenant_id, _POLICY)
    async with system_session_scope() as session:
        assert (
            await session.execute(text("SELECT count(*) FROM public.consumer_account_consents"))
        ).scalar_one() == 0
        assert not (
            await session.execute(
                text(
                    "SELECT has_table_privilege('ec_app','public.consumer_account_consents','INSERT')"
                )
            )
        ).scalar_one()
    async with tenant_session_scope(first.tenant_id) as session:
        assert (
            await session.execute(text("SELECT count(*) FROM public.consumer_account_consents"))
        ).scalar_one() == 1
        assert (
            await session.execute(
                text(
                    "SELECT count(*) FROM public.consumer_account_consents WHERE tenant_id=:other"
                ),
                {"other": second.tenant_id},
            )
        ).scalar_one() == 0


async def test_terms_versions_are_immutable_and_reacceptance_preserves_old_receipt(
    db: None,
) -> None:
    accounts, identity = PostgresConsumerAccountRepository(), _identity()
    await accounts.accept(identity, _POLICY)
    changed = LegalPolicy(
        _POLICY.terms_version,
        "https://events.example.test/changed-terms",
        _POLICY.privacy_version,
        _POLICY.privacy_url,
    )
    with pytest.raises(BrowserSessionUnavailableError):
        await accounts.accept(identity, changed)
    assert await accounts.has_accepted(identity.tenant_id, _POLICY)
    assert not await accounts.has_accepted(identity.tenant_id, changed)
    new = LegalPolicy(
        "2026-11-01",
        "https://events.example.test/terms/2026-11-01",
        _POLICY.privacy_version,
        _POLICY.privacy_url,
    )
    assert not await accounts.has_accepted(identity.tenant_id, new)
    await accounts.accept(identity, new)
    assert await accounts.has_accepted(identity.tenant_id, new)
    async with tenant_session_scope(identity.tenant_id) as session:
        assert (
            await session.execute(text("SELECT count(*) FROM public.consumer_account_consents"))
        ).scalar_one() == 2


async def test_erasure_removes_consent_and_recreation_of_retained_subject_is_fenced(
    db: None,
) -> None:
    accounts, identity, other = PostgresConsumerAccountRepository(), _identity(), _identity()
    await accounts.accept(identity, _POLICY)
    await accounts.accept(other, _POLICY)
    erasure, request_id = PostgresAccountErasureRepository(), uuid4()
    started = await erasure.begin(identity.tenant_id, request_id)
    with pytest.raises(AuthenticationFailedError):
        await accounts.accept(identity, _POLICY)
    for stage in AccountErasureStage:
        count = {
            AccountErasureStage.WORKFLOWS: started.workflow_target_count,
            AccountErasureStage.CALENDAR: started.calendar_target_count,
        }.get(stage, 1)
        await erasure.complete_stage(identity.tenant_id, request_id, stage, count)
    snapshot = await erasure.finalize(identity.tenant_id, request_id)
    assert snapshot.status is AccountErasureStatus.COMPLETED
    assert await PostgresTenantRepository().get(identity.tenant_id) is None
    assert not await accounts.has_accepted(identity.tenant_id, _POLICY)
    assert await accounts.has_accepted(other.tenant_id, _POLICY)
    with pytest.raises(AuthenticationFailedError):
        await accounts.accept(identity, _POLICY)
    # The narrow capability also rejects an attempt to reuse the subject under a random ID.
    async with system_session_scope() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text("""
                SELECT public.fn_accept_consumer_account(:id,:subject,:email,:terms,:terms_url,:privacy,:privacy_url)
            """),
                {
                    "id": uuid4(),
                    "subject": identity.subject,
                    "email": identity.email,
                    "terms": _POLICY.terms_version,
                    "terms_url": _POLICY.terms_url,
                    "privacy": _POLICY.privacy_version,
                    "privacy_url": _POLICY.privacy_url,
                },
            )
        await session.rollback()
