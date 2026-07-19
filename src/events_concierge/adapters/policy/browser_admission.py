"""Fenced browser-pool admission adapters (FR-6.4/NFR-4b, ADR-005).

Unlike a one-shot source-rate token, a browser slot remains held while a browser-facing source
operation is in flight.  The shared Redis implementation therefore records a stable opaque lease
identity plus a fresh fence owner.  Redis state loss cannot be treated as spare capacity: it first
installs a full browser-session recovery fence, then permits fresh work only after the prior
five-minute maximum session lifetime has elapsed.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import redis.asyncio as aioredis

from ...ports.browser_admission import (
    BrowserAdmissionLease,
    BrowserAdmissionPort,
    BrowserAdmissionRequest,
)

_MIN_RETRY_SECONDS: Final = 0.001
_CONTROL_MEMBER: Final = "__browser-admission-control__"
_CONTROL_SCORE: Final = "100000000000000000000"


@dataclass(slots=True)
class _MemoryRecord:
    """One locally held opaque lease, retained only until its bounded session expiry."""

    fence_digest: str
    expires_at: float


class InMemoryBrowserAdmission(BrowserAdmissionPort):
    """Deterministic process-local browser cap for offline tests and mock composition.

    This adapter preserves the duplicate and fenced-release behavior of the shared implementation,
    but it is intentionally not a deployment substitute: only :class:`RedisBrowserAdmission`
    enforces NFR-4b across workers (ADR-005).
    """

    def __init__(
        self,
        capacity: int = 135,
        lease_seconds: float = 300.0,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        _validate_config(capacity, lease_seconds)
        self._capacity = capacity
        self._lease_seconds = lease_seconds
        self._clock = clock or time.monotonic
        self._records: dict[str, _MemoryRecord] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, request: BrowserAdmissionRequest) -> BrowserAdmissionLease:
        """Grant one bounded slot or return an immediate saturation projection (AC-45)."""
        now = self._clock()
        lease_digest = _lease_digest(request.lease_id)
        async with self._lock:
            self._expire(now)
            existing = self._records.get(lease_digest)
            if existing is not None:
                return _saturated(
                    existing.expires_at - now,
                    "browser lease is already active; retry would duplicate a browser session",
                )
            if len(self._records) >= self._capacity:
                earliest_expiry = min(record.expires_at for record in self._records.values())
                return _saturated(earliest_expiry - now, "browser pool is saturated")
            self._records[lease_digest] = _MemoryRecord(
                fence_digest=_fence_digest(request.lease_id, request.fence_token),
                expires_at=now + self._lease_seconds,
            )
        return BrowserAdmissionLease(
            granted=True,
            lease_id=request.lease_id,
            fence_token=request.fence_token,
        )

    async def release(self, lease: BrowserAdmissionLease) -> None:
        """Remove only the current fence owner; stale cleanup cannot free a renewed slot."""
        if not lease.granted or not lease.lease_id or not lease.fence_token:
            return
        lease_digest = _lease_digest(lease.lease_id)
        fence_digest = _fence_digest(lease.lease_id, lease.fence_token)
        async with self._lock:
            now = self._clock()
            self._expire(now)
            existing = self._records.get(lease_digest)
            if existing is not None and existing.fence_digest == fence_digest:
                del self._records[lease_digest]

    def _expire(self, now: float) -> None:
        expired = [
            lease_digest
            for lease_digest, record in self._records.items()
            if record.expires_at <= now
        ]
        for lease_digest in expired:
            del self._records[lease_digest]


class RedisBrowserAdmission(BrowserAdmissionPort):
    """Globally shared, fail-closed browser cap with recovery fencing (NFR-4b, ADR-005).

    All three Redis structures carry independent control markers.  If Redis is restarted, evicts a
    key, or loses only one structure, the next atomic acquisition clears the uncertain records,
    starts a full ``lease_seconds`` recovery fence, and returns saturation.  That fence is long
    enough for every pre-loss browser session to age out before a replacement slot is granted.
    """

    # KEYS = state hash, leases ZSET, fence-owner hash
    # ARGV = control-member, control-score, lease-seconds, capacity, lease-digest, fence-digest
    #
    # Response: G|0 (granted), R|seconds (recovery fence), D|seconds (same stable lease still
    # active), S|seconds (capacity saturated).  The caller's raw lease/fence IDs never enter Redis;
    # only SHA-256 digests are members/fields (ADR-003/005).
    _ACQUIRE_LUA: Final = """
    local time_parts = redis.call('TIME')
    local now = tonumber(time_parts[1]) + tonumber(time_parts[2]) / 1000000
    local control = ARGV[1]
    local control_score = tonumber(ARGV[2])
    local duration = tonumber(ARGV[3])
    local capacity = tonumber(ARGV[4])
    local lease_id = ARGV[5]
    local fence_id = ARGV[6]

    local function recover()
        -- Any partial state loss makes the old slot ownership unknowable.  Remove remnants only
        -- after reserving a full maximum-session fence, so no old browser can overlap a new one.
        redis.call('DEL', KEYS[2], KEYS[3])
        redis.call('HSET', KEYS[1], 'control', control,
                   'recovery_until', tostring(now + duration))
        redis.call('ZADD', KEYS[2], control_score, control)
        redis.call('HSET', KEYS[3], control, control)
        return 'R|' .. tostring(duration)
    end

    local state_control = redis.call('HGET', KEYS[1], 'control')
    local recovery_until = tonumber(redis.call('HGET', KEYS[1], 'recovery_until'))
    local lease_control = redis.call('ZSCORE', KEYS[2], control)
    local owner_control = redis.call('HGET', KEYS[3], control)
    if state_control ~= control or recovery_until == nil or lease_control == false or
       lease_control == nil or owner_control ~= control then
        return recover()
    end

    if recovery_until > now then
        return 'R|' .. tostring(math.max(recovery_until - now, 0.001))
    end

    -- A paired ZSET/hash is required for a fence-aware release.  Count disagreement indicates
    -- partial state loss even when the sentinel keys survived, so recover instead of guessing.
    if redis.call('ZCARD', KEYS[2]) ~= redis.call('HLEN', KEYS[3]) then
        return recover()
    end

    -- Expiry is the crash-only release path.  Do not remove the control sentinel even though its
    -- deliberately distant score is normally outside this range.
    local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)
    for _, expired_lease_id in ipairs(expired) do
        if expired_lease_id ~= control then
            redis.call('ZREM', KEYS[2], expired_lease_id)
            redis.call('HDEL', KEYS[3], expired_lease_id)
        end
    end

    local existing_expiry = redis.call('ZSCORE', KEYS[2], lease_id)
    if existing_expiry ~= false and existing_expiry ~= nil then
        if redis.call('HGET', KEYS[3], lease_id) == false or
           redis.call('HGET', KEYS[3], lease_id) == nil then
            return recover()
        end
        return 'D|' .. tostring(math.max(tonumber(existing_expiry) - now, 0.001))
    end

    local active_count = redis.call('ZCARD', KEYS[2]) - 1
    if active_count >= capacity then
        local first = redis.call('ZRANGE', KEYS[2], 0, 0, 'WITHSCORES')
        if first[1] == nil or first[1] == control or first[2] == nil then
            return recover()
        end
        return 'S|' .. tostring(math.max(tonumber(first[2]) - now, 0.001))
    end

    redis.call('ZADD', KEYS[2], now + duration, lease_id)
    redis.call('HSET', KEYS[3], lease_id, fence_id)
    return 'G|0'
    """

    # KEYS = leases ZSET, fence-owner hash
    # ARGV = lease-digest, fence-digest
    # A delayed activity cleanup may see a lease ID reused after expiry.  Its old fence digest must
    # not delete the newer holder, which is why ZREM and HDEL happen only after the owner match.
    _RELEASE_LUA: Final = """
    local lease_id = ARGV[1]
    local fence_id = ARGV[2]
    local owner = redis.call('HGET', KEYS[2], lease_id)
    if owner ~= false and owner ~= nil and owner == fence_id then
        redis.call('ZREM', KEYS[1], lease_id)
        redis.call('HDEL', KEYS[2], lease_id)
    end
    return '0'
    """

    def __init__(
        self,
        redis_url: str | None = None,
        *,
        capacity: int = 135,
        lease_seconds: float = 300.0,
        unavailable_retry_seconds: float = 2.0,
        key_prefix: str = "browser-admission",
        redis_client: aioredis.Redis | None = None,
    ) -> None:
        _validate_config(capacity, lease_seconds)
        if not math.isfinite(unavailable_retry_seconds) or unavailable_retry_seconds <= 0.0:
            raise ValueError("unavailable_retry_seconds must be finite and positive")
        if not key_prefix:
            raise ValueError("key_prefix must not be empty")
        if redis_client is None:
            if not redis_url:
                raise ValueError("redis_url is required when redis_client is not provided")
            redis_client = aioredis.from_url(redis_url, decode_responses=True)
        self._redis = redis_client
        self._capacity = capacity
        self._lease_seconds = lease_seconds
        self._unavailable_retry_seconds = unavailable_retry_seconds
        self._key_prefix = key_prefix.rstrip(":")

    @property
    def state_key(self) -> str:
        """Return the global opaque state key for focused health checks and fault-injection tests."""
        return f"{self._key_prefix}:state"

    @property
    def leases_key(self) -> str:
        """Return the global opaque expiry index key; its members are SHA-256 lease IDs."""
        return f"{self._key_prefix}:leases"

    @property
    def owners_key(self) -> str:
        """Return the global opaque fence-owner key; it never stores an original fence token."""
        return f"{self._key_prefix}:owners"

    async def acquire(self, request: BrowserAdmissionRequest) -> BrowserAdmissionLease:
        """Atomically reserve a global slot or fail closed without queuing a browser activity."""
        try:
            raw = await self._redis.eval(
                self._ACQUIRE_LUA,
                3,
                self.state_key,
                self.leases_key,
                self.owners_key,
                _CONTROL_MEMBER,
                _CONTROL_SCORE,
                str(self._lease_seconds),
                str(self._capacity),
                _lease_digest(request.lease_id),
                _fence_digest(request.lease_id, request.fence_token),
            )
            return _parse_acquire(raw, request)
        except (aioredis.RedisError, OSError, ValueError) as exc:
            return _saturated(
                self._unavailable_retry_seconds,
                f"Redis browser admission unavailable: {exc}",
            )

    async def release(self, lease: BrowserAdmissionLease) -> None:
        """Best-effort fenced cleanup; an outage leaves the expiring slot conservatively held."""
        if not lease.granted or not lease.lease_id or not lease.fence_token:
            return
        try:
            await self._redis.eval(
                self._RELEASE_LUA,
                2,
                self.leases_key,
                self.owners_key,
                _lease_digest(lease.lease_id),
                _fence_digest(lease.lease_id, lease.fence_token),
            )
        except (aioredis.RedisError, OSError, ValueError):
            # Do not turn a completed source effect into an activity retry.  A matching slot expires
            # naturally, and any Redis state loss is fenced by the next acquisition (ADR-003/005).
            return

    async def aclose(self) -> None:
        """Close the owned redis-py client when a short-lived worker exits."""
        await self._redis.aclose()


def _validate_config(capacity: int, lease_seconds: float) -> None:
    if capacity <= 0:
        raise ValueError("browser admission capacity must be positive")
    if not math.isfinite(lease_seconds) or lease_seconds <= 0.0:
        raise ValueError("browser admission lease_seconds must be finite and positive")


def _lease_digest(lease_id: str) -> str:
    """Hash a Temporal-stable global lease ID before it reaches Redis (ADR-003/005)."""
    return hashlib.sha256(lease_id.encode("utf-8")).hexdigest()


def _fence_digest(lease_id: str, fence_token: str) -> str:
    """Bind a fresh opaque owner fence to its stable lease identity without retaining either raw."""
    payload = f"{lease_id}\x00{fence_token}".encode()
    return hashlib.sha256(payload).hexdigest()


def _saturated(retry_after_seconds: float, detail: str) -> BrowserAdmissionLease:
    """Normalize every non-grant to a finite durable handoff projection (AC-45/NFR-15)."""
    retry = (
        retry_after_seconds
        if math.isfinite(retry_after_seconds) and retry_after_seconds > 0.0
        else _MIN_RETRY_SECONDS
    )
    return BrowserAdmissionLease(
        granted=False,
        retry_after_seconds=max(retry, _MIN_RETRY_SECONDS),
        detail=detail,
    )


def _parse_acquire(raw: object, request: BrowserAdmissionRequest) -> BrowserAdmissionLease:
    """Parse the intentionally tiny Lua response and reject malformed state as saturation."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        raise ValueError("Redis browser admission returned a non-string Lua response")
    status, separator, value = raw.partition("|")
    if not separator:
        raise ValueError("Redis browser admission returned a malformed Lua response")
    try:
        retry_after = float(value)
    except ValueError as exc:
        raise ValueError("Redis browser admission returned a non-numeric retry") from exc
    if status == "G" and retry_after == 0.0:
        return BrowserAdmissionLease(
            granted=True,
            lease_id=request.lease_id,
            fence_token=request.fence_token,
        )
    if status == "R" and math.isfinite(retry_after) and retry_after >= 0.0:
        return _saturated(retry_after, "browser pool recovery fence is active")
    if status == "D" and math.isfinite(retry_after) and retry_after >= 0.0:
        return _saturated(
            retry_after,
            "browser lease is already active; retry would duplicate a browser session",
        )
    if status == "S" and math.isfinite(retry_after) and retry_after >= 0.0:
        return _saturated(retry_after, "browser pool is saturated")
    raise ValueError("Redis browser admission returned an invalid Lua response")
