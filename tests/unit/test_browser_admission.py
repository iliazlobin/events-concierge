"""Offline browser-pool admission coverage (AC-45, NFR-4b, ADR-005)."""

from __future__ import annotations

import asyncio
import hashlib
from typing import cast
from uuid import uuid4

import pytest
import redis.asyncio as aioredis
from pydantic import ValidationError

from events_concierge.adapters.policy.browser_admission import (
    InMemoryBrowserAdmission,
    RedisBrowserAdmission,
)
from events_concierge.config import Settings
from events_concierge.domain.enums import Source
from events_concierge.ports.browser_admission import BrowserAdmissionRequest


class _Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _request(lease_id: str, fence_token: str) -> BrowserAdmissionRequest:
    return BrowserAdmissionRequest(
        tenant_id=uuid4(),
        source=Source.LUMA,
        lease_id=lease_id,
        fence_token=fence_token,
    )


async def test_in_memory_browser_admission_enforces_capacity_and_matching_release() -> None:
    """One global local slot admits one browser call until its exact holder releases (AC-45)."""
    admission = InMemoryBrowserAdmission(capacity=1, lease_seconds=300.0)
    first = await admission.acquire(_request("first", "fence-one"))
    saturated = await admission.acquire(_request("second", "fence-two"))

    assert first.granted is True
    assert saturated.granted is False
    assert saturated.retry_after_seconds > 0.0

    await admission.release(first)
    second = await admission.acquire(_request("second", "fence-two"))

    assert second.granted is True


async def test_in_memory_browser_admission_defaults_to_the_ratified_135_slot_cap() -> None:
    """The local double preserves NFR-4b's 135-slot default instead of silently widening it."""
    admission = InMemoryBrowserAdmission()

    leases = await asyncio.gather(
        *(admission.acquire(_request(f"lease-{index}", f"fence-{index}")) for index in range(136))
    )

    assert sum(lease.granted for lease in leases) == 135
    assert sum(not lease.granted for lease in leases) == 1


def test_settings_rejects_a_recovery_fence_shorter_than_the_session_wall_clock() -> None:
    """A deployment cannot re-admit after Redis loss before a five-minute session can age out."""
    with pytest.raises(ValidationError, match="greater than or equal to 300"):
        Settings(browser_admission_lease_seconds=299.0)


async def test_in_memory_browser_admission_duplicate_and_stale_release_cannot_free_reused_slot() -> (
    None
):
    """A retry cannot double-consume, and an old cleanup cannot delete a newer fence (ADR-003)."""
    clock = _Clock()
    admission = InMemoryBrowserAdmission(1, 300.0, clock=clock)
    original = await admission.acquire(_request("same-stable-lease", "old-fence"))
    duplicate = await admission.acquire(_request("same-stable-lease", "other-fence"))

    assert original.granted is True
    assert duplicate.granted is False

    clock.now += 300.0
    renewed = await admission.acquire(_request("same-stable-lease", "new-fence"))
    assert renewed.granted is True

    await admission.release(original)
    still_saturated = await admission.acquire(_request("other-lease", "other-fence"))
    assert still_saturated.granted is False

    await admission.release(renewed)
    after_matching_release = await admission.acquire(_request("other-lease", "other-fence"))
    assert after_matching_release.granted is True


async def test_in_memory_browser_admission_expiry_recovers_a_crashed_holder() -> None:
    """The bounded five-minute lease is the conservative cleanup path after an activity crash."""
    clock = _Clock()
    admission = InMemoryBrowserAdmission(1, 300.0, clock=clock)
    assert (await admission.acquire(_request("crashed", "fence-one"))).granted is True

    clock.now += 300.0
    recovered = await admission.acquire(_request("after-expiry", "fence-two"))

    assert recovered.granted is True


class _UnavailableRedis:
    async def eval(self, *_: object) -> object:
        raise aioredis.ConnectionError("fixture Redis outage")

    async def aclose(self) -> None:
        return None


class _RecordingRedis:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    async def eval(self, *args: object) -> object:
        self.calls.append(args)
        return "G|0"

    async def aclose(self) -> None:
        return None


async def test_redis_browser_admission_outage_never_fails_open() -> None:
    """Redis loss is saturation, not evidence that a browser slot is spare (NFR-15)."""
    admission = RedisBrowserAdmission(
        capacity=1,
        unavailable_retry_seconds=2.0,
        redis_client=cast(aioredis.Redis, _UnavailableRedis()),
    )

    lease = await admission.acquire(_request("lease", "fence"))

    assert lease.granted is False
    assert lease.retry_after_seconds == 2.0
    assert "unavailable" in lease.detail


async def test_redis_browser_admission_only_sends_digests_to_redis() -> None:
    """The shared pool retains neither tenant/workflow-shaped IDs nor raw fence tokens (ADR-005)."""
    redis = _RecordingRedis()
    admission = RedisBrowserAdmission(redis_client=cast(aioredis.Redis, redis))
    request = _request("tenant:event:register", "fresh-secret-fence")

    lease = await admission.acquire(request)

    assert lease.granted is True
    flat_arguments = " ".join(str(argument) for argument in redis.calls[0])
    assert request.lease_id not in flat_arguments
    assert request.fence_token not in flat_arguments
    assert hashlib.sha256(request.lease_id.encode()).hexdigest() in flat_arguments
