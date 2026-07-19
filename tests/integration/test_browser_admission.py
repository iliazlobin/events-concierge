"""Shared browser-admission Redis coverage (AC-45, NFR-4b, ADR-005).

These tests exercise only the local Redis service.  A short bounded lease makes the five-minute
production recovery fence observable without a slow test, and every case owns a UUID key namespace.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
import redis.asyncio as aioredis

from events_concierge.adapters.policy.browser_admission import RedisBrowserAdmission
from events_concierge.domain.enums import Source
from events_concierge.ports.browser_admission import BrowserAdmissionRequest

pytestmark = pytest.mark.integration

_TEST_LEASE_SECONDS = 0.05


@asynccontextmanager
async def _redis_namespace() -> AsyncIterator[tuple[aioredis.Redis, str]]:
    raw = aioredis.from_url(os.environ["EC_REDIS_URL"], decode_responses=True)
    prefix = f"browser-admission-test:{uuid4().hex}"
    try:
        yield raw, prefix
    finally:
        keys = await raw.keys(f"{prefix}:*")
        if keys:
            await raw.delete(*keys)
        await raw.aclose()


def _request(lease_id: str, fence_token: str) -> BrowserAdmissionRequest:
    return BrowserAdmissionRequest(
        tenant_id=uuid4(),
        source=Source.LUMA,
        lease_id=lease_id,
        fence_token=fence_token,
    )


async def _arm_after_recovery(raw: aioredis.Redis, admission: RedisBrowserAdmission) -> None:
    """Advance only this isolated fixture's recovery clock after proving its cold-start behavior."""
    cold = await admission.acquire(_request("cold-start", "cold-fence"))
    assert cold.granted is False
    assert "recovery" in cold.detail
    await raw.hset(admission.state_key, mapping={"recovery_until": "0"})


async def test_redis_browser_pool_caps_cross_worker_grants_and_recovers_after_release() -> None:
    """Two clients can hold exactly capacity slots; a released matching fence admits one more."""
    async with _redis_namespace() as (raw, prefix):
        first = RedisBrowserAdmission(
            os.environ["EC_REDIS_URL"],
            capacity=2,
            lease_seconds=1.0,
            key_prefix=prefix,
        )
        second = RedisBrowserAdmission(
            os.environ["EC_REDIS_URL"],
            capacity=2,
            lease_seconds=1.0,
            key_prefix=prefix,
        )
        try:
            await _arm_after_recovery(raw, first)
            one, two, three = await asyncio.gather(
                first.acquire(_request("one", "one-fence")),
                second.acquire(_request("two", "two-fence")),
                first.acquire(_request("three", "three-fence")),
            )
            grants = [lease for lease in (one, two, three) if lease.granted]
            saturated = [lease for lease in (one, two, three) if not lease.granted]
            assert len(grants) == 2
            assert len(saturated) == 1
            assert "saturated" in saturated[0].detail

            await second.release(grants[0])
            after_release = await first.acquire(_request("after-release", "fresh-fence"))
        finally:
            await first.aclose()
            await second.aclose()

    assert after_release.granted is True


async def test_redis_browser_pool_fences_stale_release_and_recovery_after_partial_loss() -> None:
    """A stale cleanup cannot erase a renewed slot, and any lost control structure re-fences."""
    async with _redis_namespace() as (raw, prefix):
        admission = RedisBrowserAdmission(
            os.environ["EC_REDIS_URL"],
            capacity=1,
            lease_seconds=_TEST_LEASE_SECONDS,
            key_prefix=prefix,
        )
        try:
            await _arm_after_recovery(raw, admission)
            original = await admission.acquire(_request("stable", "old-fence"))
            assert original.granted is True
            duplicate = await admission.acquire(_request("stable", "duplicate-fence"))
            assert duplicate.granted is False

            # A crashed activity's bounded lease expires. Its old release must not free the new
            # holder that reuses the workflow-minted identity with a new physical fence.
            await asyncio.sleep(_TEST_LEASE_SECONDS * 2)
            renewed = await admission.acquire(_request("stable", "new-fence"))
            assert renewed.granted is True
            await admission.release(original)
            still_full = await admission.acquire(_request("other", "other-fence"))
            assert still_full.granted is False

            await raw.delete(admission.owners_key)
            after_loss = await admission.acquire(_request("after-loss", "loss-fence"))
        finally:
            await admission.aclose()

    assert after_loss.granted is False
    assert "recovery" in after_loss.detail


@pytest.mark.parametrize("missing_key", ("state_key", "leases_key", "owners_key"))
async def test_redis_browser_pool_fences_each_missing_control_structure(missing_key: str) -> None:
    """No single surviving marker can be mistaken for proof that pre-loss sessions are gone."""
    async with _redis_namespace() as (raw, prefix):
        admission = RedisBrowserAdmission(
            os.environ["EC_REDIS_URL"],
            capacity=1,
            lease_seconds=1.0,
            key_prefix=prefix,
        )
        try:
            await _arm_after_recovery(raw, admission)
            held = await admission.acquire(_request("held", "held-fence"))
            assert held.granted is True
            await raw.delete(getattr(admission, missing_key))
            recovered = await admission.acquire(_request("after-partial-loss", "recovery-fence"))
        finally:
            await admission.aclose()

    assert recovered.granted is False
    assert "recovery" in recovered.detail
