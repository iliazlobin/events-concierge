"""Non-blocking fair-share Pacer implementations (FR-5.9/FR-10.4, ADR-005).

The Pacer is deliberately advisory state outside Temporal.  A caller asks once immediately before
wire I/O; a saturated or unavailable Pacer returns a projected wait and the workflow owns the
durable timer.  It never sleeps in an activity worker.

Redis bucket state is disposable by design.  Missing state is initialized *empty*, not full, so a
Redis restart or eviction can only slow requests down before it refills.  The Redis Lua path uses
the server clock; its optimistic-transaction fallback has the identical empty-on-loss invariant.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import redis.asyncio as aioredis

from ...domain.enums import Source
from ...ports.policy import PacerBudget, PacerLease, PacerLeaseStatus, PacerOperation, PacerRequest


@dataclass(slots=True)
class _Bucket:
    tokens: float
    updated_at: float
    blocked_until: float = 0.0


def default_source_budgets() -> dict[Source, PacerBudget]:
    """Return ratified, credential-free profile shapes for future provider adapters (ADR-002/005).

    Meetup uses its accepted 500 points/60 seconds envelope; the caller supplies a measured cost
    once G2 exists. Luma's 100 POST/5-minute envelope is conservatively applied to all current
    Luma operations until its browser lane is activated. Ticketmaster's selected crawler working
    rate is 2 rps—below the external 5-rps maximum; P1c adds its separate 5,000/day ledger.
    """
    return {
        Source.MEETUP: PacerBudget(rate_per_sec=500.0 / 60.0, burst=500),
        Source.LUMA: PacerBudget(rate_per_sec=100.0 / 300.0, burst=100),
        Source.TICKETMASTER: PacerBudget(rate_per_sec=2.0, burst=2),
    }


class InMemoryPacer:
    """A deterministic process-local Pacer for offline tests and local mock composition.

    Its initial bucket has the configured burst so ordinary mock suites stay fast.  That is an
    intentional test-double difference: production Redis always cold-starts empty under ADR-005.
    """

    def __init__(
        self,
        rate_per_sec: float = 5.0,
        burst: int = 10,
        *,
        clock: Callable[[], float] | None = None,
        source_budgets: Mapping[Source, PacerBudget] | None = None,
    ) -> None:
        _validate_config(rate_per_sec, burst)
        self._default_budget = PacerBudget(rate_per_sec=rate_per_sec, burst=burst)
        self._source_budgets = dict(source_budgets or {})
        self._clock = clock or time.monotonic
        self._buckets: dict[str, _Bucket] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def acquire(self, request: PacerRequest) -> PacerLease:
        """Return an immediate token decision; never sleep while a worker is leased."""
        budget = self._budget_for(request)
        _validate_cost(request.cost, float(budget.burst))
        key = request.bucket_key
        async with self._lock_for(key):
            now = self._clock()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(budget.burst), updated_at=now)
                self._buckets[key] = bucket
            if now < bucket.blocked_until:
                return _wait(bucket.blocked_until - now, "source backoff is active")
            bucket.tokens = min(
                float(budget.burst), bucket.tokens + (now - bucket.updated_at) * budget.rate_per_sec
            )
            bucket.updated_at = now
            if bucket.tokens >= request.cost:
                bucket.tokens -= request.cost
                return PacerLease(PacerLeaseStatus.GRANTED)
            return _wait(
                (request.cost - bucket.tokens) / budget.rate_per_sec,
                "token bucket is empty",
            )

    async def observe_backoff(
        self,
        request: PacerRequest,
        *,
        retry_after_seconds: float | None = None,
        reset_at: datetime | None = None,
    ) -> None:
        """Share a local source throttle window without consuming a token (AC-73)."""
        delay = _backoff_seconds(retry_after_seconds, reset_at)
        if delay <= 0.0:
            return
        key = request.bucket_key
        budget = self._budget_for(request)
        async with self._lock_for(key):
            now = self._clock()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(budget.burst), updated_at=now)
                self._buckets[key] = bucket
            bucket.blocked_until = max(bucket.blocked_until, now + delay)
            # A source throttle is a full block, not retained burst capacity.
            bucket.tokens = 0.0
            bucket.updated_at = bucket.blocked_until

    def _budget_for(self, request: PacerRequest) -> PacerBudget:
        if request.operation is PacerOperation.CATALOG_HTTP_GET:
            return _catalog_http_budget(request)
        return self._source_budgets.get(request.source, self._default_budget)


# How long a shared bucket's tokens/timestamp survive idleness. It must comfortably exceed the
# gap between two ordinary uses of the same bucket, or steady-state operation keeps landing in the
# state-loss branch. Reviewed catalog sources refresh on cadences up to a full day, so a floor of
# one week leaves room for the slowest of them plus any operator pause. Retention costs one small
# hash per bucket and never hands back spent capacity: an idle bucket refills from its own
# recorded timestamp and caps at burst, exactly as a continuously used one does.
_DEFAULT_STATE_RETENTION_SECONDS = 604_800.0


class RedisPacer:
    """Shared Redis token buckets that fail throttle-first on every store failure.

    ``acquire`` is one non-blocking attempt.  The Lua hot path and WATCH/MULTI fallback both use
    Redis ``TIME`` and initialize a missing bucket with zero tokens.  A Redis error returns a
    finite ``WAIT`` projection rather than an unsafe grant (ADR-005).
    """

    # KEYS[1] = bucket hash
    # ARGV = rate/token-second, burst, cost, base-TTL-seconds
    # The string response keeps redis-py decoding stable across Redis releases: G|0 or W|seconds.
    _TAKE_LUA = """
    local time_parts = redis.call('TIME')
    local now = tonumber(time_parts[1]) + tonumber(time_parts[2]) / 1000000
    local rate = tonumber(ARGV[1])
    local burst = tonumber(ARGV[2])
    local cost = tonumber(ARGV[3])
    local base_ttl = tonumber(ARGV[4])
    local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
    local ts = tonumber(redis.call('HGET', KEYS[1], 'ts'))
    local blocked_until = tonumber(redis.call('HGET', KEYS[1], 'blocked_until')) or 0

    local function persist(next_tokens, next_ts, wait)
        local ttl = math.ceil(math.max(base_ttl, wait) * 1000) + 1000
        redis.call('HSET', KEYS[1], 'tokens', next_tokens, 'ts', next_ts,
                   'blocked_until', blocked_until)
        redis.call('PEXPIRE', KEYS[1], ttl)
    end

    if now < blocked_until then
        persist(tokens or 0, ts or blocked_until, blocked_until - now)
        return 'W|' .. tostring(blocked_until - now)
    end

    -- A missing Redis key is deliberately cold/empty after eviction or restart (ADR-005).
    if tokens == nil or ts == nil then
        local wait = cost / rate
        persist(0, now, wait)
        return 'W|' .. tostring(wait)
    end

    local filled = math.min(burst, tokens + math.max(0, now - ts) * rate)
    if filled >= cost then
        persist(filled - cost, now, 0)
        return 'G|0'
    end
    local wait = (cost - filled) / rate
    persist(filled, now, wait)
    return 'W|' .. tostring(wait)
    """

    # KEYS[1] = bucket hash
    # ARGV = retry-after seconds, reset Unix timestamp (or 0), base-TTL-seconds
    _BACKOFF_LUA = """
    local time_parts = redis.call('TIME')
    local now = tonumber(time_parts[1]) + tonumber(time_parts[2]) / 1000000
    local retry_after = tonumber(ARGV[1]) or 0
    local reset_at = tonumber(ARGV[2]) or 0
    local base_ttl = tonumber(ARGV[3])
    local existing = tonumber(redis.call('HGET', KEYS[1], 'blocked_until')) or 0
    local blocked_until = math.max(existing, now + retry_after, reset_at)
    if blocked_until <= now then
        return '0'
    end
    -- A provider throttle is a full block; do not retain accumulated burst capacity under it.
    redis.call('HSET', KEYS[1], 'tokens', 0, 'ts', blocked_until,
               'blocked_until', blocked_until)
    local ttl = math.ceil(math.max(base_ttl, blocked_until - now) * 1000) + 1000
    redis.call('PEXPIRE', KEYS[1], ttl)
    return tostring(blocked_until - now)
    """

    # KEYS = state hash, active-lane ring list, active-lane set, item-expiry ZSET.
    # ARGV = rate, burst, cost, base-TTL, item-TTL, DRR-quantum, degrade-threshold,
    #        lane digest, item digest, lane-key prefix, item-key prefix.
    #
    # The per-app Meetup contingency is deliberately a different key family from the ordinary
    # `(source, credential)` bucket.  Tenant and workflow queue identities arrive as SHA-256
    # digests, so neither a token nor a workflow-shaped identifier is persisted in Redis.  The
    # queue is advisory: a grant removes its item, so a crash after a grant re-enqueues and
    # conservatively charges again rather than accidentally reserving a stale lease (ADR-005).
    #
    # ``work_enqueued`` / ``work_completed`` yield a conservative queue projection for a specific
    # item.  Later arrivals cannot worsen an already-enqueued item's projection, and expiring work
    # advances ``work_completed`` before a new projection is made.  DRR still determines the
    # actual one-item-per-lane service order; the projection is intentionally a safe upper bound.
    _MEETUP_APP_TAKE_LUA = """
    local time_parts = redis.call('TIME')
    local now = tonumber(time_parts[1]) + tonumber(time_parts[2]) / 1000000
    local rate = tonumber(ARGV[1])
    local burst = tonumber(ARGV[2])
    local cost = tonumber(ARGV[3])
    local base_ttl = tonumber(ARGV[4])
    local item_ttl = tonumber(ARGV[5])
    local quantum = tonumber(ARGV[6])
    local degrade_after = tonumber(ARGV[7])
    local caller_lane = ARGV[8]
    local caller_item = ARGV[9]
    local lane_prefix = ARGV[10]
    local item_prefix = ARGV[11]

    local function ttl_ms(wait)
        return math.ceil(math.max(base_ttl, item_ttl, wait) * 1000) + 1000
    end

    local function lane_key(lane)
        return lane_prefix .. lane
    end

    local function item_key(item)
        return item_prefix .. item
    end

    local function number_field(key, field, default)
        local raw = redis.call('HGET', key, field)
        if raw == false or raw == nil then
            return default
        end
        local parsed = tonumber(raw)
        if parsed == nil then
            return default
        end
        return parsed
    end

    local function remove_lane_if_empty(lane)
        local key = lane_key(lane)
        if redis.call('LLEN', key) == 0 then
            redis.call('SREM', KEYS[3], lane)
            redis.call('LREM', KEYS[2], 0, lane)
            redis.call('DEL', key)
            redis.call('HDEL', KEYS[1], 'deficit:' .. lane)
        end
    end

    local function remove_item(item)
        local key = item_key(item)
        local lane = redis.call('HGET', key, 'lane')
        local item_cost = tonumber(redis.call('HGET', key, 'cost'))
        if lane ~= false and lane ~= nil and item_cost ~= nil then
            redis.call('LREM', lane_key(lane), 0, item)
            redis.call('HINCRBYFLOAT', KEYS[1], 'work_completed', item_cost)
            remove_lane_if_empty(lane)
        end
        redis.call('ZREM', KEYS[4], item)
        redis.call('DEL', key)
    end

    -- Drop expired queue entries before any projection. A queue identity is refreshed by a
    -- Temporal retry, but a permanently lost workflow may never return; its Redis entry must not
    -- hold a lane forever (ADR-005's deliberately disposable state).
    local expired = redis.call('ZRANGEBYSCORE', KEYS[4], '-inf', now)
    for _, item in ipairs(expired) do
        remove_item(item)
    end

    local caller_key = item_key(caller_item)
    local existing_lane = redis.call('HGET', caller_key, 'lane')
    local existing_cost = tonumber(redis.call('HGET', caller_key, 'cost'))
    local existing_expiry = tonumber(redis.call('HGET', caller_key, 'expires_at')) or 0
    if existing_lane ~= false and existing_lane ~= nil and existing_expiry > now then
        -- Replaying the same Temporal queue identity must refresh, never duplicate, FIFO work.
        if existing_lane ~= caller_lane or existing_cost == nil or existing_cost ~= cost then
            return 'W|1'
        end
        redis.call('HSET', caller_key, 'expires_at', now + item_ttl)
        redis.call('ZADD', KEYS[4], now + item_ttl, caller_item)
        redis.call('PEXPIRE', caller_key, ttl_ms(item_ttl))
    else
        if existing_lane ~= false and existing_lane ~= nil then
            remove_item(caller_item)
        end
        local work = redis.call('HINCRBYFLOAT', KEYS[1], 'work_enqueued', cost)
        redis.call(
            'HSET',
            caller_key,
            'lane', caller_lane,
            'cost', cost,
            'work', work,
            'expires_at', now + item_ttl
        )
        redis.call('PEXPIRE', caller_key, ttl_ms(item_ttl))
        redis.call('ZADD', KEYS[4], now + item_ttl, caller_item)
        redis.call('RPUSH', lane_key(caller_lane), caller_item)
        redis.call('PEXPIRE', lane_key(caller_lane), ttl_ms(item_ttl))
        if redis.call('SADD', KEYS[3], caller_lane) == 1 then
            redis.call('RPUSH', KEYS[2], caller_lane)
        end
    end

    -- A lane can contain an item whose hash expired just before its ZSET cleanup. Remove such
    -- heads while advancing around the ring; this is still one atomic scheduler decision.
    local function valid_head(lane)
        local key = lane_key(lane)
        while true do
            local head = redis.call('LINDEX', key, 0)
            if head == false or head == nil then
                remove_lane_if_empty(lane)
                return nil, nil
            end
            local head_key = item_key(head)
            local head_cost = tonumber(redis.call('HGET', head_key, 'cost'))
            local head_expiry = tonumber(redis.call('HGET', head_key, 'expires_at')) or 0
            if head_cost == nil or head_expiry <= now then
                if head_cost ~= nil then
                    redis.call('HINCRBYFLOAT', KEYS[1], 'work_completed', head_cost)
                end
                redis.call('LPOP', key)
                redis.call('ZREM', KEYS[4], head)
                redis.call('DEL', head_key)
                remove_lane_if_empty(lane)
            else
                return head, head_cost
            end
        end
    end

    local function current_head()
        local rounds = redis.call('LLEN', KEYS[2])
        for _ = 1, rounds do
            local lane = redis.call('LINDEX', KEYS[2], 0)
            if lane == false or lane == nil then
                return nil, nil, nil
            end
            if redis.call('SISMEMBER', KEYS[3], lane) == 0 then
                redis.call('LPOP', KEYS[2])
            else
                local head, head_cost = valid_head(lane)
                if head ~= nil then
                    return lane, head, head_cost
                end
            end
        end
        return nil, nil, nil
    end

    local tokens = tonumber(redis.call('HGET', KEYS[1], 'tokens'))
    local ts = tonumber(redis.call('HGET', KEYS[1], 'ts'))
    local blocked_until = number_field(KEYS[1], 'blocked_until', 0)
    if tokens == nil or ts == nil then
        -- State loss is throttle-first: queues can exist, but a recreated bucket is empty.
        tokens = 0
        ts = now
    elseif now >= blocked_until then
        tokens = math.min(burst, tokens + math.max(0, now - ts) * rate)
        ts = now
    end

    local caller_work = number_field(caller_key, 'work', number_field(KEYS[1], 'work_enqueued', cost))
    local completed = number_field(KEYS[1], 'work_completed', 0)
    local remaining_ahead = math.max(0, caller_work - completed)
    local blocked_wait = math.max(0, blocked_until - now)
    local refill_wait = math.max(0, (remaining_ahead - tokens) / rate)
    local projected_wait = blocked_wait + refill_wait
    if projected_wait > degrade_after then
        remove_item(caller_item)
        redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', ts, 'blocked_until', blocked_until)
        redis.call('PEXPIRE', KEYS[1], ttl_ms(projected_wait))
        redis.call('PEXPIRE', KEYS[2], ttl_ms(projected_wait))
        redis.call('PEXPIRE', KEYS[3], ttl_ms(projected_wait))
        redis.call('PEXPIRE', KEYS[4], ttl_ms(projected_wait))
        return 'D|' .. tostring(projected_wait)
    end

    if now < blocked_until then
        redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', ts, 'blocked_until', blocked_until)
        redis.call('PEXPIRE', KEYS[1], ttl_ms(projected_wait))
        redis.call('PEXPIRE', KEYS[2], ttl_ms(projected_wait))
        redis.call('PEXPIRE', KEYS[3], ttl_ms(projected_wait))
        redis.call('PEXPIRE', KEYS[4], ttl_ms(projected_wait))
        return 'W|' .. tostring(math.max(projected_wait, blocked_wait))
    end

    local lane, head, head_cost = current_head()
    if lane == nil then
        -- The caller was just enqueued, so this only protects an unexpected Redis inconsistency.
        redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', ts, 'blocked_until', blocked_until)
        redis.call('PEXPIRE', KEYS[1], ttl_ms(1))
        return 'W|1'
    end

    local deficit_field = 'deficit:' .. lane
    local deficit = number_field(KEYS[1], deficit_field, 0) + quantum
    redis.call('HSET', KEYS[1], deficit_field, deficit)
    if deficit < head_cost then
        redis.call('LPOP', KEYS[2])
        redis.call('RPUSH', KEYS[2], lane)
        redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', ts, 'blocked_until', blocked_until)
        redis.call('PEXPIRE', KEYS[1], ttl_ms(math.max(projected_wait, 1 / rate)))
        redis.call('PEXPIRE', KEYS[2], ttl_ms(math.max(projected_wait, 1 / rate)))
        redis.call('PEXPIRE', KEYS[3], ttl_ms(math.max(projected_wait, 1 / rate)))
        redis.call('PEXPIRE', KEYS[4], ttl_ms(math.max(projected_wait, 1 / rate)))
        return 'W|' .. tostring(math.max(projected_wait, 1 / rate))
    end

    if head == caller_item and tokens >= head_cost then
        local granted_cost = head_cost
        remove_item(caller_item)
        tokens = tokens - granted_cost
        deficit = deficit - granted_cost
        redis.call('HSET', KEYS[1], deficit_field, deficit)
        if redis.call('SISMEMBER', KEYS[3], lane) == 1 then
            redis.call('LPOP', KEYS[2])
            redis.call('RPUSH', KEYS[2], lane)
        end
        redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', now, 'blocked_until', blocked_until)
        redis.call('PEXPIRE', KEYS[1], ttl_ms(0))
        redis.call('PEXPIRE', KEYS[2], ttl_ms(0))
        redis.call('PEXPIRE', KEYS[3], ttl_ms(0))
        redis.call('PEXPIRE', KEYS[4], ttl_ms(0))
        return 'G|0'
    end

    local wait = math.max(projected_wait, 1 / rate)
    redis.call('HSET', KEYS[1], 'tokens', tokens, 'ts', ts, 'blocked_until', blocked_until)
    redis.call('PEXPIRE', KEYS[1], ttl_ms(wait))
    redis.call('PEXPIRE', KEYS[2], ttl_ms(wait))
    redis.call('PEXPIRE', KEYS[3], ttl_ms(wait))
    redis.call('PEXPIRE', KEYS[4], ttl_ms(wait))
    return 'W|' .. tostring(wait)
    """

    def __init__(
        self,
        redis_url: str | None = None,
        rate_per_sec: float = 5.0,
        burst: int = 10,
        *,
        unavailable_retry_seconds: float = 2.0,
        state_retention_seconds: float = _DEFAULT_STATE_RETENTION_SECONDS,
        key_prefix: str = "pacer",
        redis_client: aioredis.Redis | None = None,
        source_budgets: Mapping[Source, PacerBudget] | None = None,
        meetup_app_quota_scope: str | None = None,
        meetup_app_degrade_after_seconds: float = 300.0,
        meetup_app_lane_quantum: int = 1,
        meetup_app_queue_item_ttl_seconds: float = 600.0,
    ) -> None:
        _validate_config(rate_per_sec, burst)
        if not math.isfinite(unavailable_retry_seconds) or unavailable_retry_seconds <= 0.0:
            raise ValueError("unavailable_retry_seconds must be finite and positive")
        if not math.isfinite(state_retention_seconds) or state_retention_seconds <= 0.0:
            raise ValueError("state_retention_seconds must be finite and positive")
        if not key_prefix:
            raise ValueError("key_prefix must not be empty")
        if meetup_app_quota_scope == "":
            raise ValueError("meetup_app_quota_scope must not be empty when configured")
        if (
            not math.isfinite(meetup_app_degrade_after_seconds)
            or meetup_app_degrade_after_seconds <= 0
        ):
            raise ValueError("meetup_app_degrade_after_seconds must be finite and positive")
        if meetup_app_lane_quantum <= 0:
            raise ValueError("meetup_app_lane_quantum must be positive")
        if (
            not math.isfinite(meetup_app_queue_item_ttl_seconds)
            or meetup_app_queue_item_ttl_seconds < meetup_app_degrade_after_seconds
        ):
            raise ValueError(
                "meetup_app_queue_item_ttl_seconds must be finite and at least the degrade threshold"
            )
        if redis_client is None:
            if not redis_url:
                raise ValueError("redis_url is required when redis_client is not provided")
            redis_client = aioredis.from_url(redis_url, decode_responses=True)
        self._redis = redis_client
        self._default_budget = PacerBudget(rate_per_sec=rate_per_sec, burst=burst)
        self._source_budgets = dict(source_budgets or {})
        self._unavailable_retry_seconds = unavailable_retry_seconds
        self._state_retention_seconds = state_retention_seconds
        self._key_prefix = key_prefix.rstrip(":")
        self._meetup_app_quota_scope = meetup_app_quota_scope
        self._meetup_app_degrade_after_seconds = meetup_app_degrade_after_seconds
        self._meetup_app_lane_quantum = meetup_app_lane_quantum
        self._meetup_app_queue_item_ttl_seconds = meetup_app_queue_item_ttl_seconds

    @property
    def meetup_app_quota_scope(self) -> str | None:
        """Return the configured opaque app scope; ``None`` leaves normal per-token pacing active."""
        return self._meetup_app_quota_scope

    @property
    def meetup_app_degrade_after_seconds(self) -> float:
        """Return the owner-ratified five-minute projected-wait handoff threshold (ADR-005)."""
        return self._meetup_app_degrade_after_seconds

    def bucket_key(self, request: PacerRequest) -> str:
        """Expose the namespaced Redis key for focused fault-injection tests and operations."""
        return f"{self._key_prefix}:bucket:{request.bucket_key}"

    def meetup_app_state_key(self) -> str:
        """Return the opaque state key for the optional G2-gated Meetup app bucket (ADR-005)."""
        if self._meetup_app_quota_scope is None:
            raise ValueError("Meetup app-scope pacing is not configured")
        return f"{self._meetup_app_key_prefix()}:state"

    async def acquire(self, request: PacerRequest) -> PacerLease:
        """Attempt one atomic shared-bucket take, returning a throttle projection on any failure."""
        budget = self._budget_for(request)
        _validate_cost(request.cost, float(budget.burst))
        if self._uses_meetup_app_fairness(request):
            return await self._acquire_meetup_app_fair(request, budget)
        try:
            raw = await self._redis.eval(
                self._TAKE_LUA,
                1,
                self.bucket_key(request),
                str(budget.rate_per_sec),
                str(budget.burst),
                str(request.cost),
                str(self._base_ttl_seconds(budget)),
            )
            return _parse_lease(raw)
        except aioredis.ResponseError:
            # A server without scripting still needs the same state-loss safety invariant.
            return await self._optimistic_acquire(request, budget)
        except (aioredis.RedisError, OSError, ValueError) as exc:
            return _wait(self._unavailable_retry_seconds, f"Redis Pacer unavailable: {exc}")

    async def observe_backoff(
        self,
        request: PacerRequest,
        *,
        retry_after_seconds: float | None = None,
        reset_at: datetime | None = None,
    ) -> None:
        """Persist the longest source throttle window across all workers sharing a bucket."""
        delay = _backoff_seconds(retry_after_seconds, reset_at)
        reset_epoch = _reset_epoch(reset_at)
        if delay <= 0.0 and reset_epoch <= 0.0:
            return
        budget = self._budget_for(request)
        bucket_key = (
            self.meetup_app_state_key()
            if self._uses_meetup_app_fairness(request)
            else self.bucket_key(request)
        )
        base_ttl = self._base_ttl_seconds(budget)
        if self._uses_meetup_app_fairness(request):
            base_ttl = max(base_ttl, self._meetup_app_queue_item_ttl_seconds)
        try:
            await self._redis.eval(
                self._BACKOFF_LUA,
                1,
                bucket_key,
                str(delay),
                str(reset_epoch),
                str(base_ttl),
            )
        except aioredis.ResponseError:
            await self._optimistic_backoff(
                request,
                budget,
                delay,
                reset_epoch,
                bucket_key=bucket_key,
                base_ttl_seconds=base_ttl,
            )
        except (aioredis.RedisError, OSError):
            # The next acquisition remains throttle-first if Redis is still unavailable.  There is
            # no local fail-open cache that could defeat a provider's throttling instruction.
            return

    def _base_ttl_seconds(self, budget: PacerBudget) -> float:
        """Retain shared bucket state well past one refill window.

        A bucket is only ever missing for two reasons: real state loss, or ordinary idleness.
        Redis cannot tell them apart, and the take script deliberately treats a missing bucket as
        empty (ADR-005), so retention decides which case dominates. Retaining only one refill
        window made idleness the common case: a catalog source refreshed on an hourly interval
        found its bucket expired on every single attempt, was told to wait, and had its durable
        run paused before any provider call - roughly one refresh per dispatch pass instead of a
        full batch.

        Longer retention also makes the limiter *more* faithful, not less: a bucket that expires
        forgets the tokens it just spent, while a retained bucket keeps refilling from its own
        recorded timestamp. Genuine state loss still cold-starts empty and throttle-first.
        """
        return max(
            float(budget.burst) / budget.rate_per_sec,
            self._unavailable_retry_seconds,
            self._state_retention_seconds,
        )

    def _uses_meetup_app_fairness(self, request: PacerRequest) -> bool:
        """Restrict the G2 contingency to Meetup; all other source buckets remain unchanged."""
        return request.source is Source.MEETUP and self._meetup_app_quota_scope is not None

    def _meetup_app_key_prefix(self) -> str:
        if self._meetup_app_quota_scope is None:
            raise ValueError("Meetup app-scope pacing is not configured")
        return f"{self._key_prefix}:meetup-app:{_opaque_digest(self._meetup_app_quota_scope)}"

    async def _acquire_meetup_app_fair(
        self, request: PacerRequest, budget: PacerBudget
    ) -> PacerLease:
        """Run the G2-gated global DRR/FIFO lane scheduler without exposing tenant identities.

        The mode requires a Temporal-stable queue identity.  A missing identity cannot fall back to
        the credential bucket because doing so would silently defeat app-scope fairness; it waits
        conservatively until the caller has been upgraded (ADR-005, FR-10.4).
        """
        if request.tenant_id is None or not request.queue_item_id:
            return _wait(
                self._unavailable_retry_seconds,
                "Meetup app-scope pacing requires tenant_id and a stable queue_item_id",
            )
        prefix = self._meetup_app_key_prefix()
        lane_digest = _opaque_digest(str(request.tenant_id))
        item_digest = _opaque_digest(f"{request.tenant_id}\x00{request.queue_item_id}")
        base_ttl = max(self._base_ttl_seconds(budget), self._meetup_app_queue_item_ttl_seconds)
        try:
            raw = await self._redis.eval(
                self._MEETUP_APP_TAKE_LUA,
                4,
                f"{prefix}:state",
                f"{prefix}:ring",
                f"{prefix}:active",
                f"{prefix}:expiry",
                str(budget.rate_per_sec),
                str(budget.burst),
                str(request.cost),
                str(base_ttl),
                str(self._meetup_app_queue_item_ttl_seconds),
                str(self._meetup_app_lane_quantum),
                str(self._meetup_app_degrade_after_seconds),
                lane_digest,
                item_digest,
                f"{prefix}:lane:",
                f"{prefix}:item:",
            )
            return _parse_lease(raw, detail="Meetup app-scope DRR/FIFO queue")
        except aioredis.ResponseError as exc:
            # We must not bypass the shared fair queue if scripting is disabled.  Unlike the
            # generic bucket's WATCH/MULTI fallback, a partial fair-queue mutation would be worse
            # than a durable delay; the next retry remains cold/throttle-first.
            return _wait(
                self._unavailable_retry_seconds,
                f"Meetup app-scope Redis scheduler unavailable: {exc}",
            )
        except (aioredis.RedisError, OSError, ValueError) as exc:
            return _wait(
                self._unavailable_retry_seconds,
                f"Meetup app-scope Redis scheduler unavailable: {exc}",
            )

    async def _optimistic_acquire(self, request: PacerRequest, budget: PacerBudget) -> PacerLease:
        """Script-free Redis fallback using WATCH/MULTI with Redis TIME and empty initialization."""
        bucket_key = self.bucket_key(request)
        for _ in range(3):
            # redis-py's asyncio ``Pipeline.multi/reset`` lack inline annotations in redis 5.x;
            # isolate that untyped adapter boundary here while keeping every decision typed.
            pipeline: Any = self._redis.pipeline(transaction=True)
            try:
                await pipeline.watch(bucket_key)
                values = await pipeline.hgetall(bucket_key)
                now = await self._redis_time()
                lease, fields, ttl_seconds = self._take_from_fields(
                    values, now, request.cost, budget
                )
                pipeline.multi()
                pipeline.hset(bucket_key, mapping=fields)
                pipeline.pexpire(bucket_key, _ttl_milliseconds(ttl_seconds))
                await pipeline.execute()
                return lease
            except aioredis.WatchError:
                continue
            except (aioredis.RedisError, OSError, ValueError) as exc:
                return _wait(self._unavailable_retry_seconds, f"Redis Pacer unavailable: {exc}")
            finally:
                await pipeline.reset()
        return _wait(
            self._unavailable_retry_seconds, "Redis Pacer contention; retry conservatively"
        )

    async def _optimistic_backoff(
        self,
        request: PacerRequest,
        budget: PacerBudget,
        delay: float,
        reset_epoch: float,
        *,
        bucket_key: str | None = None,
        base_ttl_seconds: float | None = None,
    ) -> None:
        """Script-free full-block update; a collision is safe because later callers re-acquire."""
        bucket_key = bucket_key or self.bucket_key(request)
        base_ttl_seconds = base_ttl_seconds or self._base_ttl_seconds(budget)
        for _ in range(3):
            pipeline: Any = self._redis.pipeline(transaction=True)
            try:
                await pipeline.watch(bucket_key)
                values = await pipeline.hgetall(bucket_key)
                now = await self._redis_time()
                existing = _field_float(values, "blocked_until", 0.0) or 0.0
                blocked_until = max(existing, now + delay, reset_epoch)
                if blocked_until <= now:
                    return
                pipeline.multi()
                pipeline.hset(
                    bucket_key,
                    mapping={
                        "tokens": "0",
                        "ts": repr(blocked_until),
                        "blocked_until": repr(blocked_until),
                    },
                )
                pipeline.pexpire(
                    bucket_key,
                    _ttl_milliseconds(max(base_ttl_seconds, blocked_until - now)),
                )
                await pipeline.execute()
                return
            except aioredis.WatchError:
                continue
            except (aioredis.RedisError, OSError, ValueError):
                return
            finally:
                await pipeline.reset()

    async def _redis_time(self) -> float:
        seconds, microseconds = await self._redis.time()
        return float(seconds) + float(microseconds) / 1_000_000.0

    def _take_from_fields(
        self, values: dict[Any, Any], now: float, cost: int, budget: PacerBudget
    ) -> tuple[PacerLease, dict[str, str], float]:
        tokens_raw = _field_float(values, "tokens", None)
        timestamp_raw = _field_float(values, "ts", None)
        blocked_until = _field_float(values, "blocked_until", 0.0) or 0.0
        if now < blocked_until:
            wait = blocked_until - now
            return (
                _wait(wait, "source backoff is active"),
                {
                    "tokens": repr(tokens_raw or 0.0),
                    "ts": repr(timestamp_raw or blocked_until),
                    "blocked_until": repr(blocked_until),
                },
                max(self._base_ttl_seconds(budget), wait),
            )
        if tokens_raw is None or timestamp_raw is None:
            # Match Lua: missing state is empty and must refill before any grant.
            wait = float(cost) / budget.rate_per_sec
            return (
                _wait(wait, "Redis bucket cold-started empty"),
                {"tokens": "0", "ts": repr(now), "blocked_until": repr(blocked_until)},
                max(self._base_ttl_seconds(budget), wait),
            )
        filled = min(
            budget.burst,
            tokens_raw + max(0.0, now - timestamp_raw) * budget.rate_per_sec,
        )
        if filled >= cost:
            return (
                PacerLease(PacerLeaseStatus.GRANTED),
                {
                    "tokens": repr(filled - cost),
                    "ts": repr(now),
                    "blocked_until": repr(blocked_until),
                },
                self._base_ttl_seconds(budget),
            )
        wait = (float(cost) - filled) / budget.rate_per_sec
        return (
            _wait(wait, "token bucket is empty"),
            {
                "tokens": repr(filled),
                "ts": repr(now),
                "blocked_until": repr(blocked_until),
            },
            max(self._base_ttl_seconds(budget), wait),
        )

    async def aclose(self) -> None:
        """Release the owned redis-py client when a short-lived worker shuts down."""
        await self._redis.aclose()

    def _budget_for(self, request: PacerRequest) -> PacerBudget:
        if request.operation is PacerOperation.CATALOG_HTTP_GET:
            return _catalog_http_budget(request)
        return self._source_budgets.get(request.source, self._default_budget)


def _catalog_http_budget(request: PacerRequest) -> PacerBudget:
    """Shape one catalog GET as a single-token interval bucket (ADR-005)."""
    interval_ms = request.catalog_min_interval_ms
    if interval_ms is None:
        raise ValueError("catalog HTTP GET Pacer requests require catalog_min_interval_ms")
    return PacerBudget(rate_per_sec=1_000.0 / interval_ms, burst=1)


def _validate_config(rate_per_sec: float, burst: int) -> None:
    if not math.isfinite(rate_per_sec) or rate_per_sec <= 0.0:
        raise ValueError("rate_per_sec must be finite and positive")
    if burst <= 0:
        raise ValueError("burst must be positive")


def _validate_cost(cost: int, burst: float) -> None:
    if cost <= 0:
        raise ValueError("Pacer cost must be positive")
    if cost > burst:
        raise ValueError("Pacer cost cannot exceed bucket burst")


def _wait(seconds: float, detail: str) -> PacerLease:
    """Normalize all throttle projections to a finite positive durable-timer delay."""
    safe_seconds = seconds if math.isfinite(seconds) and seconds > 0.0 else 1.0
    return PacerLease(PacerLeaseStatus.WAIT, max(safe_seconds, 0.001), detail)


def _parse_lease(
    raw: object,
    *,
    detail: str = "shared token bucket is refilling or source backoff is active",
) -> PacerLease:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not isinstance(raw, str):
        raise ValueError("Redis Pacer returned a non-string Lua response")
    status, separator, value = raw.partition("|")
    if not separator:
        raise ValueError("Redis Pacer returned a malformed Lua response")
    try:
        wait = float(value)
    except ValueError as exc:
        raise ValueError("Redis Pacer returned a non-numeric wait") from exc
    if status == "G" and wait == 0.0:
        return PacerLease(PacerLeaseStatus.GRANTED)
    if status == "W" and math.isfinite(wait) and wait >= 0.0:
        return _wait(wait, detail)
    if status == "D" and math.isfinite(wait) and wait > 0.0:
        return PacerLease(PacerLeaseStatus.DEGRADE, wait, detail)
    raise ValueError("Redis Pacer returned an invalid Lua response")


def _opaque_digest(value: str) -> str:
    """Create a stable Redis-safe identifier without retaining a tenant/workflow value in keys."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _field_float(values: dict[Any, Any], field: str, default: float | None) -> float | None:
    value = values.get(field)
    if value is None:
        value = values.get(field.encode("utf-8"))
    if value is None:
        return default
    if isinstance(value, bytes):
        value = value.decode("utf-8")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _ttl_milliseconds(seconds: float) -> int:
    return max(1, math.ceil(max(seconds, 0.001) * 1_000.0) + 1_000)


def _backoff_seconds(retry_after_seconds: float | None, reset_at: datetime | None) -> float:
    """Project a source throttle relative to now while rejecting malformed negative values."""
    if retry_after_seconds is not None and (
        not math.isfinite(retry_after_seconds) or retry_after_seconds < 0.0
    ):
        raise ValueError("retry_after_seconds must be finite and non-negative")
    retry_after = retry_after_seconds or 0.0
    if reset_at is None:
        return retry_after
    if reset_at.tzinfo is None or reset_at.utcoffset() is None:
        raise ValueError("reset_at must be timezone-aware")
    reset_delay = max((reset_at.astimezone(UTC) - datetime.now(UTC)).total_seconds(), 0.0)
    return max(retry_after, reset_delay)


def _reset_epoch(reset_at: datetime | None) -> float:
    if reset_at is None:
        return 0.0
    if reset_at.tzinfo is None or reset_at.utcoffset() is None:
        raise ValueError("reset_at must be timezone-aware")
    return reset_at.astimezone(UTC).timestamp()
