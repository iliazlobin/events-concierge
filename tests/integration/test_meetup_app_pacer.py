"""G2-gated Meetup app-scope fair-queue coverage (FR-10.4, ADR-005).

The tests use Redis only. They exercise the dormant per-app Pacer mode without a Meetup
credential, source adapter, browser session, or external request.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

import pytest
import redis.asyncio as aioredis

from events_concierge.adapters.policy.pacer import RedisPacer
from events_concierge.domain.enums import Source
from events_concierge.ports.policy import (
    PacerBudget,
    PacerLeaseStatus,
    PacerOperation,
    PacerRequest,
)

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _redis_namespace() -> AsyncIterator[tuple[aioredis.Redis, str]]:
    raw = aioredis.from_url(os.environ["EC_REDIS_URL"], decode_responses=True)
    prefix = f"meetup-app-pacer-test:{uuid4().hex}"
    try:
        yield raw, prefix
    finally:
        keys = await raw.keys(f"{prefix}:*")
        if keys:
            await raw.delete(*keys)
        await raw.aclose()


def _pacer(
    prefix: str, *, rate_per_sec: float = 1.0, burst: int = 10, degrade: float = 300.0
) -> RedisPacer:
    return RedisPacer(
        os.environ["EC_REDIS_URL"],
        rate_per_sec=rate_per_sec,
        burst=burst,
        key_prefix=prefix,
        source_budgets={Source.MEETUP: PacerBudget(rate_per_sec=rate_per_sec, burst=burst)},
        meetup_app_quota_scope="fixture-app-scope",
        meetup_app_degrade_after_seconds=degrade,
        meetup_app_queue_item_ttl_seconds=max(600.0, degrade),
    )


def _request(
    tenant_id: UUID, item_id: str, *, cost: int = 1, quota_scope: str = "token-a"
) -> PacerRequest:
    return PacerRequest(
        source=Source.MEETUP,
        quota_scope=quota_scope,
        operation=PacerOperation.REGISTRATION_MUTATION,
        cost=cost,
        tenant_id=tenant_id,
        queue_item_id=item_id,
    )


async def _seed_tokens(raw: aioredis.Redis, pacer: RedisPacer, tokens: float) -> None:
    seconds, microseconds = await raw.time()
    now = float(seconds) + float(microseconds) / 1_000_000.0
    state_key = pacer.meetup_app_state_key()
    await raw.hset(
        state_key,
        mapping={"tokens": repr(tokens), "ts": repr(now), "blocked_until": "0"},
    )
    await raw.pexpire(state_key, 600_000)


async def test_meetup_app_scope_uses_drr_between_tenants_and_fifo_within_a_tenant() -> None:
    """One app bucket rotates between tenant FIFO lanes despite different token scopes (ADR-005)."""
    async with _redis_namespace() as (raw, prefix):
        first = _pacer(prefix)
        second = _pacer(prefix)
        tenant_a, tenant_b = uuid4(), uuid4()
        a_first = _request(tenant_a, "a-first", quota_scope="token-a")
        a_second = _request(tenant_a, "a-second", quota_scope="token-a")
        b_first = _request(tenant_b, "b-first", quota_scope="token-b")
        try:
            # Cold state queues all three identities before an operator/test replenishes tokens.
            assert (await first.acquire(a_first)).status is PacerLeaseStatus.WAIT
            assert (await first.acquire(a_second)).status is PacerLeaseStatus.WAIT
            assert (await second.acquire(b_first)).status is PacerLeaseStatus.WAIT
            await _seed_tokens(raw, first, tokens=3.0)

            # A's first item wins its own FIFO lane. Its second item cannot pass B's head even
            # when its activity happens to re-acquire first; B then receives the next DRR turn.
            assert (await first.acquire(a_first)).status is PacerLeaseStatus.GRANTED
            assert (await first.acquire(a_second)).status is PacerLeaseStatus.WAIT
            assert (await second.acquire(b_first)).status is PacerLeaseStatus.GRANTED
            assert (await first.acquire(a_second)).status is PacerLeaseStatus.GRANTED
        finally:
            await first.aclose()
            await second.aclose()


@pytest.mark.parametrize(
    ("cost", "expected"),
    [(300, PacerLeaseStatus.WAIT), (301, PacerLeaseStatus.DEGRADE)],
)
async def test_meetup_app_degrades_only_above_the_strict_five_minute_projection(
    cost: int, expected: PacerLeaseStatus
) -> None:
    """Exactly five minutes remains a durable wait; only a longer lane projection hands off."""
    async with _redis_namespace() as (_, prefix):
        pacer = _pacer(prefix, rate_per_sec=1.0, burst=1_000, degrade=300.0)
        try:
            lease = await pacer.acquire(_request(uuid4(), f"threshold-{cost}", cost=cost))
        finally:
            await pacer.aclose()

    assert lease.status is expected
    assert lease.retry_after_seconds >= float(cost) - 0.01


async def test_meetup_app_retry_after_grant_requeues_and_recharges_instead_of_replaying_a_lease() -> (
    None
):
    """A crash after an advisory grant cannot leave a reusable source-call lease (ADR-003/005)."""
    async with _redis_namespace() as (raw, prefix):
        pacer = _pacer(prefix, rate_per_sec=0.001, burst=1, degrade=2_000.0)
        request = _request(uuid4(), "same-temporal-queue-item")
        try:
            # Seed the shared bucket after its first cold start so one physical attempt may run.
            assert (await pacer.acquire(request)).status is PacerLeaseStatus.WAIT
            await _seed_tokens(raw, pacer, tokens=1.0)
            assert (await pacer.acquire(request)).status is PacerLeaseStatus.GRANTED

            replay = await pacer.acquire(request)
        finally:
            await pacer.aclose()

    assert replay.status is PacerLeaseStatus.WAIT
    assert replay.retry_after_seconds >= 999.0


async def test_meetup_app_scope_shares_provider_backoff_and_cold_state_never_bursts() -> None:
    """A source throttle and Redis loss both remain global and fail throttle-first (AC-73)."""
    async with _redis_namespace() as (raw, prefix):
        first = _pacer(prefix, rate_per_sec=1.0, burst=2)
        second = _pacer(prefix, rate_per_sec=1.0, burst=2)
        tenant_a, tenant_b = uuid4(), uuid4()
        blocked_request = _request(tenant_a, "backoff", quota_scope="token-a")
        other_request = _request(tenant_b, "other", quota_scope="token-b")
        try:
            await first.observe_backoff(blocked_request, retry_after_seconds=30.0)
            blocked = await second.acquire(other_request)

            base = first.meetup_app_state_key().rsplit(":", 1)[0]
            keys = await raw.keys(f"{base}:*")
            assert keys
            await raw.delete(*keys)
            cold = await first.acquire(_request(tenant_a, "after-loss"))
        finally:
            await first.aclose()
            await second.aclose()

    assert blocked.status is PacerLeaseStatus.WAIT
    assert blocked.retry_after_seconds >= 29.0
    assert cold.status is PacerLeaseStatus.WAIT
    assert cold.retry_after_seconds >= 0.9
