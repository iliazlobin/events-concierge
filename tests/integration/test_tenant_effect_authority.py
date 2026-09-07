"""Exact PostgreSQL ordering races between live external effects and account erasure."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from temporalio.converter import StorageDriverStoreContext, StorageDriverWorkflowInfo

from events_concierge.adapters.mock.object_store import MockFilesystemObjectStore
from events_concierge.adapters.postgres.account_erasure import (
    PostgresAccountErasureRepository,
)
from events_concierge.adapters.postgres.tenant_effects import (
    PostgresTenantEffectAuthority,
    PostgresTenantExternalEffectDrain,
)
from events_concierge.adapters.postgres.tenant_repos import (
    PostgresRequestRepository,
    PostgresTenantRepository,
)
from events_concierge.application.request_start import RequestStartRelay
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.ids import request_workflow_id
from events_concierge.domain.request import EventRequest, RequestConstraints
from events_concierge.infra.db import tenant_session_scope
from events_concierge.ports.tenant_effects import (
    TenantEffectAuthorityConfig,
    TenantEffectCleanupNotAuthorizedError,
    TenantEffectFencedError,
    TenantEffectKind,
    TenantEffectLockTimeoutError,
    TenantEffectMode,
    TenantEffectRequest,
)
from events_concierge.workflows.claim_check import build_claim_check_data_converter

pytestmark = pytest.mark.integration


def _tenant() -> Tenant:
    tenant_id = uuid4()
    return Tenant(
        tenant_id=tenant_id,
        oidc_subject=f"oidc|effect-fence-{tenant_id}",
        notify_email=f"effect-fence-{tenant_id}@example.test",
        relay_inbox=f"effect-fence-{tenant_id}@u.example.test",
    )


def _request(tenant: Tenant, *, mode: TenantEffectMode = TenantEffectMode.LIVE) -> TenantEffectRequest:
    return TenantEffectRequest(
        tenant_id=tenant.tenant_id,
        kind=TenantEffectKind.REGISTRATION,
        timeout_seconds=5.0,
        mode=mode,
    )


async def test_started_effect_and_cancellation_settle_before_erasure_can_commit(db: None) -> None:
    """The erasure tombstone cannot pass a cancelled-but-still-running provider operation."""
    tenant = _tenant()
    await PostgresTenantRepository().add(tenant)
    authority = PostgresTenantEffectAuthority()
    repository = PostgresAccountErasureRepository()
    effect_started = asyncio.Event()
    allow_effect_to_finish = asyncio.Event()
    order: list[str] = []

    async def effect() -> None:
        order.append("effect_started")
        effect_started.set()
        await allow_effect_to_finish.wait()
        order.append("effect_finished")

    guarded = asyncio.create_task(authority.run(_request(tenant), effect))
    await effect_started.wait()
    erasure = asyncio.create_task(repository.begin(tenant.tenant_id, uuid4()))
    await asyncio.sleep(0.05)
    assert not erasure.done()

    guarded.cancel()
    await asyncio.sleep(0.05)
    assert not guarded.done()
    assert not erasure.done()

    allow_effect_to_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await guarded
    snapshot = await asyncio.wait_for(erasure, timeout=2.0)
    order.append("erasure_committed")
    assert snapshot.tenant_id == tenant.tenant_id
    assert order == ["effect_started", "effect_finished", "erasure_committed"]

    # This is the concrete worker boundary: it must observe the committed tombstone under the
    # exact same lock before the durable external_effects stage may be acknowledged.
    await PostgresTenantExternalEffectDrain(
        authority,
        timeout_seconds=1.0,
    ).drain_tenant_effects(tenant.tenant_id)

    called = False

    async def forbidden_effect() -> None:
        nonlocal called
        called = True

    with pytest.raises(TenantEffectFencedError, match="fenced"):
        await authority.run(_request(tenant), forbidden_effect)
    assert not called

    # Cleanup has the inverse authorization rule and can run only after this exact tombstone.
    await authority.run(
        _request(tenant, mode=TenantEffectMode.ERASURE_CLEANUP),
        forbidden_effect,
    )
    assert called


async def test_cleanup_mode_cannot_bypass_a_live_tenant_fence(db: None) -> None:
    tenant = _tenant()
    await PostgresTenantRepository().add(tenant)
    called = False

    async def cleanup() -> None:
        nonlocal called
        called = True

    with pytest.raises(TenantEffectCleanupNotAuthorizedError, match="tombstone"):
        await PostgresTenantEffectAuthority().run(
            _request(tenant, mode=TenantEffectMode.ERASURE_CLEANUP),
            cleanup,
        )
    assert not called


async def test_real_advisory_lock_timeout_is_mapped_to_the_typed_boundary(db: None) -> None:
    """PostgreSQL's actual lock-timeout SQLSTATE must never leak as a raw driver error."""
    tenant = _tenant()
    await PostgresTenantRepository().add(tenant)
    lock_held = asyncio.Event()
    release_lock = asyncio.Event()

    async def hold_same_lock() -> None:
        async with tenant_session_scope(tenant.tenant_id) as session:
            await session.execute(
                text(
                    """SELECT pg_advisory_xact_lock(
                           hashtextextended(
                               'account-erasure:' || CAST(:tenant_id AS text), 0
                           )
                       )"""
                ),
                {"tenant_id": tenant.tenant_id},
            )
            lock_held.set()
            await release_lock.wait()

    holder = asyncio.create_task(hold_same_lock())
    await lock_held.wait()
    called = False

    async def effect() -> None:
        nonlocal called
        called = True

    authority = PostgresTenantEffectAuthority(
        TenantEffectAuthorityConfig(lock_timeout_seconds=0.1)
    )
    try:
        with pytest.raises(TenantEffectLockTimeoutError, match="lock deadline"):
            await authority.run(_request(tenant), effect)
        assert not called
    finally:
        release_lock.set()
        await holder


async def test_guarded_request_start_reenters_for_threshold_forced_claim_check(
    db: None,
    tmp_path: Path,
) -> None:
    """A Temporal encode under shared request-start authority must reuse its exact lock."""
    tenant = _tenant()
    await PostgresTenantRepository().add(tenant)
    request_id = uuid4()
    requests = PostgresRequestRepository()
    await requests.add_and_enqueue_start(
        EventRequest(request_id, tenant.tenant_id, "oversized request", RequestConstraints()),
        f"tenant-effect-nesting:{request_id}",
    )
    authority = PostgresTenantEffectAuthority()
    store = MockFilesystemObjectStore(tmp_path / "claim-check")

    class ClaimCheckingStarter:
        def __init__(self) -> None:
            self.claim_count = 0

        async def start(self, tenant_id: UUID, claimed_request_id: UUID) -> None:
            converter = build_claim_check_data_converter(
                store,
                threshold_bytes=1,
                tenant_effect_authority=authority,
                tenant_effect_timeout_seconds=1.0,
            )._with_store_context(
                StorageDriverStoreContext(
                    target=StorageDriverWorkflowInfo(
                        namespace="default",
                        id=request_workflow_id(tenant_id, claimed_request_id),
                        run_id=str(uuid4()),
                        type="EventRequestWorkflow",
                    )
                )
            )
            [claim] = await converter.encode(["x" * 4096])
            self.claim_count = len(claim.external_payloads)

        async def cancel(self, tenant_id: UUID, claimed_request_id: UUID) -> None:
            del tenant_id, claimed_request_id

    starter = ClaimCheckingStarter()
    relay = RequestStartRelay(
        requests,
        starter,
        tenant_effect_authority=authority,
        tenant_effect_timeout_seconds=1.0,
    )

    assert await asyncio.wait_for(
        relay.relay_request(tenant.tenant_id, request_id),
        timeout=2.0,
    )
    assert starter.claim_count >= 1
