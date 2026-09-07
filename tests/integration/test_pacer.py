"""Redis Pacer fault-injection coverage (ADR-005, FR-10.4, AC-73).

These tests use only the local Redis compose service.  Every test receives a UUID namespace and
removes only its own keys; no shared database flush is ever needed to simulate a lost bucket.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import redis.asyncio as aioredis

from events_concierge.adapters.policy.pacer import RedisPacer
from events_concierge.domain.enums import Source
from events_concierge.ports.policy import PacerLeaseStatus, PacerOperation, PacerRequest

pytestmark = pytest.mark.integration


@asynccontextmanager
async def _redis_namespace() -> AsyncIterator[tuple[aioredis.Redis, str]]:
    redis_url = os.environ["EC_REDIS_URL"]
    raw = aioredis.from_url(redis_url, decode_responses=True)
    prefix = f"pacer-test:{uuid4().hex}"
    try:
        yield raw, prefix
    finally:
        keys = await raw.keys(f"{prefix}:*")
        if keys:
            await raw.delete(*keys)
        await raw.aclose()


def _request(source: Source, quota_scope: str, *, cost: int = 1) -> PacerRequest:
    return PacerRequest(
        source=source,
        quota_scope=quota_scope,
        operation=PacerOperation.REGISTRATION_MUTATION,
        cost=cost,
    )


def _catalog_http_get_request(quota_scope: str, *, min_interval_ms: int) -> PacerRequest:
    return PacerRequest(
        source=Source.PUBLIC_JSONLD,
        quota_scope=quota_scope,
        operation=PacerOperation.CATALOG_HTTP_GET,
        catalog_min_interval_ms=min_interval_ms,
    )


async def _seed_bucket(
    raw: aioredis.Redis, pacer: RedisPacer, request: PacerRequest, tokens: float
) -> None:
    seconds, microseconds = await raw.time()
    now = float(seconds) + float(microseconds) / 1_000_000.0
    await raw.hset(
        pacer.bucket_key(request),
        mapping={"tokens": repr(tokens), "ts": repr(now), "blocked_until": "0"},
    )
    await raw.pexpire(pacer.bucket_key(request), 60_000)


async def test_redis_pacer_is_atomic_across_independent_worker_clients() -> None:
    """Eight workers sharing a burst-two bucket can consume exactly two tokens (ADR-005)."""
    async with _redis_namespace() as (raw, prefix):
        pacers = [
            RedisPacer(os.environ["EC_REDIS_URL"], rate_per_sec=0.001, burst=2, key_prefix=prefix)
            for _ in range(8)
        ]
        request = _request(Source.MEETUP, "credential-a")
        try:
            await _seed_bucket(raw, pacers[0], request, tokens=2.0)
            leases = await asyncio.gather(*(pacer.acquire(request) for pacer in pacers))
        finally:
            await asyncio.gather(*(pacer.aclose() for pacer in pacers))

    assert sum(lease.status is PacerLeaseStatus.GRANTED for lease in leases) == 2
    assert sum(lease.status is PacerLeaseStatus.WAIT for lease in leases) == 6


async def test_redis_pacer_uses_catalog_http_get_single_token_interval_across_workers() -> None:
    """A per-GET catalog bucket has one shared token and its registry interval (ADR-005)."""
    async with _redis_namespace() as (raw, prefix):
        pacers = [
            RedisPacer(os.environ["EC_REDIS_URL"], rate_per_sec=100.0, burst=10, key_prefix=prefix)
            for _ in range(4)
        ]
        request = _catalog_http_get_request("catalog:public-jsonld", min_interval_ms=1_000)
        try:
            await _seed_bucket(raw, pacers[0], request, tokens=1.0)
            leases = await asyncio.gather(*(pacer.acquire(request) for pacer in pacers))
        finally:
            await asyncio.gather(*(pacer.aclose() for pacer in pacers))

    granted = [lease for lease in leases if lease.status is PacerLeaseStatus.GRANTED]
    waiting = [lease for lease in leases if lease.status is PacerLeaseStatus.WAIT]
    assert len(granted) == 1
    assert len(waiting) == 3
    assert all(lease.retry_after_seconds >= 0.9 for lease in waiting)


async def test_redis_pacer_shares_paged_legistar_origin_across_city_page_queue_ids() -> None:
    """All four city pages contend by API origin, never source/run identity (ADR-005)."""
    async with _redis_namespace() as (raw, prefix):
        first = RedisPacer(
            os.environ["EC_REDIS_URL"], rate_per_sec=100.0, burst=10, key_prefix=prefix
        )
        second = RedisPacer(
            os.environ["EC_REDIS_URL"], rate_per_sec=100.0, burst=10, key_prefix=prefix
        )
        third = RedisPacer(
            os.environ["EC_REDIS_URL"], rate_per_sec=100.0, burst=10, key_prefix=prefix
        )
        fourth = RedisPacer(
            os.environ["EC_REDIS_URL"], rate_per_sec=100.0, burst=10, key_prefix=prefix
        )
        first_page = PacerRequest(
            source=Source.PUBLIC_JSONLD,
            quota_scope="catalog-origin:webapi.legistar.com",
            operation=PacerOperation.CATALOG_HTTP_GET,
            queue_item_id="manual:san-jose-legistar:get:0",
            catalog_min_interval_ms=1_500,
        )
        second_page = PacerRequest(
            source=Source.PUBLIC_JSONLD,
            quota_scope="catalog-origin:webapi.legistar.com",
            operation=PacerOperation.CATALOG_HTTP_GET,
            queue_item_id="manual:sunnyvale-legistar:get:3",
            catalog_min_interval_ms=1_500,
        )
        third_page = PacerRequest(
            source=Source.PUBLIC_JSONLD,
            quota_scope="catalog-origin:webapi.legistar.com",
            operation=PacerOperation.CATALOG_HTTP_GET,
            queue_item_id="manual:alameda-legistar:get:4",
            catalog_min_interval_ms=1_500,
        )
        fourth_page = PacerRequest(
            source=Source.PUBLIC_JSONLD,
            quota_scope="catalog-origin:webapi.legistar.com",
            operation=PacerOperation.CATALOG_HTTP_GET,
            queue_item_id="manual:oakland-legistar:get:2",
            catalog_min_interval_ms=1_500,
        )
        try:
            await _seed_bucket(raw, first, first_page, tokens=1.0)
            leases = await asyncio.gather(
                first.acquire(first_page),
                second.acquire(second_page),
                third.acquire(third_page),
                fourth.acquire(fourth_page),
            )
        finally:
            await asyncio.gather(first.aclose(), second.aclose(), third.aclose(), fourth.aclose())

    assert sum(lease.status is PacerLeaseStatus.GRANTED for lease in leases) == 1
    waits = [lease for lease in leases if lease.status is PacerLeaseStatus.WAIT]
    assert len(waits) == 3
    assert all(lease.retry_after_seconds >= 1.4 for lease in waits)


async def test_redis_pacer_treats_costs_and_credential_scopes_as_shared_budget_boundaries() -> None:
    """Costs contend atomically, while a distinct credential receives its own bucket."""
    async with _redis_namespace() as (raw, prefix):
        first = RedisPacer(
            os.environ["EC_REDIS_URL"], rate_per_sec=0.001, burst=5, key_prefix=prefix
        )
        second = RedisPacer(
            os.environ["EC_REDIS_URL"], rate_per_sec=0.001, burst=5, key_prefix=prefix
        )
        first_scope = _request(Source.MEETUP, "credential-a", cost=3)
        second_scope = _request(Source.MEETUP, "credential-b")
        try:
            await _seed_bucket(raw, first, first_scope, tokens=5.0)
            cost_three = await asyncio.gather(
                first.acquire(first_scope),
                second.acquire(first_scope),
            )
            await _seed_bucket(raw, first, second_scope, tokens=1.0)
            isolated = await second.acquire(second_scope)
        finally:
            await asyncio.gather(first.aclose(), second.aclose())

    assert sum(lease.status is PacerLeaseStatus.GRANTED for lease in cost_three) == 1
    assert isolated.status is PacerLeaseStatus.GRANTED


async def test_redis_pacer_script_fallback_remains_atomic_across_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WATCH/MULTI fallback has the same shared burst boundary as the Lua hot path."""
    async with _redis_namespace() as (raw, prefix):
        pacers = [
            RedisPacer(os.environ["EC_REDIS_URL"], rate_per_sec=0.001, burst=2, key_prefix=prefix)
            for _ in range(4)
        ]

        async def script_unavailable(*_: object, **__: object) -> object:
            raise aioredis.ResponseError("fixture script disabled")

        request = _request(Source.MEETUP, "credential-a")
        try:
            await _seed_bucket(raw, pacers[0], request, tokens=2.0)
            for pacer in pacers:
                monkeypatch.setattr(pacer._redis, "eval", script_unavailable)
            leases = await asyncio.gather(*(pacer.acquire(request) for pacer in pacers))
        finally:
            await asyncio.gather(*(pacer.aclose() for pacer in pacers))

    assert sum(lease.status is PacerLeaseStatus.GRANTED for lease in leases) == 2
    assert sum(lease.status is PacerLeaseStatus.WAIT for lease in leases) == 2


async def test_redis_loss_cold_starts_empty_without_a_source_burst() -> None:
    """Deleting one bucket simulates eviction/restart; all immediate callers must be deferred."""
    async with _redis_namespace() as (raw, prefix):
        pacers = [
            RedisPacer(os.environ["EC_REDIS_URL"], rate_per_sec=0.001, burst=3, key_prefix=prefix)
            for _ in range(6)
        ]
        request = _request(Source.LUMA, "calendar-a")
        try:
            # Prove the old live state could have burst, then delete only this namespace's bucket.
            await _seed_bucket(raw, pacers[0], request, tokens=3.0)
            assert (await pacers[0].acquire(request)).status is PacerLeaseStatus.GRANTED
            await raw.delete(pacers[0].bucket_key(request))

            leases = await asyncio.gather(*(pacer.acquire(request) for pacer in pacers))
        finally:
            await asyncio.gather(*(pacer.aclose() for pacer in pacers))

    assert all(lease.status is PacerLeaseStatus.WAIT for lease in leases)
    assert all(lease.retry_after_seconds >= 999.0 for lease in leases)


async def test_redis_pacer_honors_shared_retry_after_and_script_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider backoff blocks another worker; a scripting outage still cold-starts safely."""
    async with _redis_namespace() as (raw, prefix):
        first = RedisPacer(os.environ["EC_REDIS_URL"], rate_per_sec=1.0, burst=2, key_prefix=prefix)
        second = RedisPacer(
            os.environ["EC_REDIS_URL"], rate_per_sec=1.0, burst=2, key_prefix=prefix
        )
        meetup = _request(Source.MEETUP, "credential-a")
        luma = _request(Source.LUMA, "calendar-a")
        ticketmaster = _request(Source.TICKETMASTER, "app-key")
        try:
            await first.observe_backoff(meetup, retry_after_seconds=30.0)
            blocked = await second.acquire(meetup)
            await first.observe_backoff(luma, reset_at=datetime.now(UTC) + timedelta(seconds=30))
            reset_blocked = await second.acquire(luma)

            async def script_unavailable(*_: object, **__: object) -> object:
                raise aioredis.ResponseError("fixture script disabled")

            # The fallback is exercised against the real Redis pipeline, not a local imitation.
            monkeypatch.setattr(second._redis, "eval", script_unavailable)
            cold = await second.acquire(ticketmaster)
            await _seed_bucket(raw, second, ticketmaster, tokens=1.0)
            fallback_grant = await second.acquire(ticketmaster)
        finally:
            await asyncio.gather(first.aclose(), second.aclose())

    assert blocked.status is PacerLeaseStatus.WAIT
    assert blocked.retry_after_seconds >= 29.0
    assert reset_blocked.status is PacerLeaseStatus.WAIT
    assert reset_blocked.retry_after_seconds >= 29.0
    assert cold.status is PacerLeaseStatus.WAIT
    assert fallback_grant.status is PacerLeaseStatus.GRANTED


def _catalog_refresh_request(quota_scope: str) -> PacerRequest:
    return PacerRequest(
        source=Source.PUBLIC_JSONLD,
        quota_scope=quota_scope,
        operation=PacerOperation.CATALOG_REFRESH,
    )


async def test_an_idle_catalog_bucket_outlives_its_refill_window_and_still_grants() -> None:
    """An hourly source must not be deferred purely because its bucket expired between runs.

    A missing bucket is treated as lost state and fails throttle-first, which is correct for a
    real eviction. When retention covered only one refill window, ordinary idleness produced the
    same signal: every source on an hourly cadence found its bucket gone on every attempt, was
    told to wait, and had its durable refresh paused before any provider call.
    """
    async with _redis_namespace() as (raw, prefix):
        pacer = RedisPacer(
            os.environ["EC_REDIS_URL"],
            rate_per_sec=5.0,
            burst=10,
            key_prefix=prefix,
        )
        request = _catalog_refresh_request("catalog:hourly-source")
        try:
            first = await pacer.acquire(request)
            ttl_ms = await raw.pttl(pacer.bucket_key(request))
            # Idling past a full refill window must not discard the bucket.
            await asyncio.sleep(2.5)
            second = await pacer.acquire(request)
            third = await pacer.acquire(request)
        finally:
            await pacer.aclose()

    # The very first touch of a never-seen bucket still starts empty.
    assert first.status is PacerLeaseStatus.WAIT
    # ...but the bucket must survive far longer than the burst/rate refill window.
    assert ttl_ms > 60_000, "an idle bucket must outlive ordinary cadence gaps"
    assert second.status is PacerLeaseStatus.GRANTED
    assert third.status is PacerLeaseStatus.GRANTED


async def test_retained_bucket_still_enforces_the_shared_rate() -> None:
    """Retaining state longer must not hand back capacity that was already spent."""
    async with _redis_namespace() as (raw, prefix):
        pacer = RedisPacer(
            os.environ["EC_REDIS_URL"],
            rate_per_sec=0.001,
            burst=2,
            key_prefix=prefix,
        )
        request = _catalog_refresh_request("catalog:spent-source")
        try:
            await _seed_bucket(raw, pacer, request, tokens=2.0)
            leases = [await pacer.acquire(request) for _ in range(4)]
        finally:
            await pacer.aclose()

    assert sum(lease.status is PacerLeaseStatus.GRANTED for lease in leases) == 2
    assert sum(lease.status is PacerLeaseStatus.WAIT for lease in leases) == 2
