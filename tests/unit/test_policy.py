"""Unit tests for the policy adapters (FR-5.9/FR-7/FR-10.4; ADR-004/ADR-005).

The PDP fails CLOSED (unknown/quarantined/kill-switched all deny) and admits a permitted
(source, modality); the in-memory pacer serializes bursts against a tiny bucket. No redis required.
"""

from __future__ import annotations

from typing import cast
from unittest.mock import patch
from uuid import uuid4

import pytest
import redis.asyncio as aioredis

from events_concierge.adapters.mock.policy import MockPolicySnapshotReader
from events_concierge.adapters.policy.engine import DataPolicyEngine, StoreBackedPolicyEngine
from events_concierge.adapters.policy.pacer import (
    InMemoryPacer,
    RedisPacer,
    default_source_budgets,
)
from events_concierge.composition import _build_pacer
from events_concierge.config import Settings
from events_concierge.domain.enums import Modality, Source
from events_concierge.domain.policy import SourcePolicy
from events_concierge.ports.policy import (
    PacerLeaseStatus,
    PacerOperation,
    PacerRequest,
    PolicyContext,
)


def _engine(kill_switch: bool = False) -> DataPolicyEngine:
    return DataPolicyEngine(
        source_policies={
            Source.PUBLIC_JSONLD: SourcePolicy(
                source=Source.PUBLIC_JSONLD,
                automation_allowed={Modality.BROWSER: True},
            ),
        },
        kill_switch=kill_switch,
    )


def _ctx(
    source: Source, modality: Modality = Modality.BROWSER, action: str = "register"
) -> PolicyContext:
    return PolicyContext(tenant_id=uuid4(), source=source, modality=modality, action=action)


def _pacer_request(
    source: Source = Source.MEETUP, quota_scope: str = "credential", *, cost: int = 1
) -> PacerRequest:
    return PacerRequest(
        source=source,
        quota_scope=quota_scope,
        operation=PacerOperation.REGISTRATION_MUTATION,
        cost=cost,
    )


@pytest.mark.parametrize(
    ("operation", "catalog_min_interval_ms", "message"),
    [
        (PacerOperation.CATALOG_HTTP_GET, None, "require catalog_min_interval_ms"),
        (PacerOperation.CATALOG_HTTP_GET, 0, "positive integer"),
        (PacerOperation.CATALOG_REFRESH, 250, "valid only"),
    ],
)
def test_catalog_http_get_pacer_request_requires_its_typed_interval(
    operation: PacerOperation, catalog_min_interval_ms: int | None, message: str
) -> None:
    """Catalog GET admission has a single typed interval input (ADR-005)."""
    with pytest.raises(ValueError, match=message):
        PacerRequest(
            source=Source.PUBLIC_JSONLD,
            quota_scope="catalog:public-jsonld",
            operation=operation,
            catalog_min_interval_ms=catalog_min_interval_ms,
        )


async def test_allows_permitted_source_modality() -> None:
    decision = await _engine().evaluate(_ctx(Source.PUBLIC_JSONLD))
    assert decision.allowed is True
    assert decision.reason


async def test_denies_unknown_source() -> None:
    decision = await _engine().evaluate(_ctx(Source.MEETUP))
    assert decision.allowed is False
    assert "unknown source" in decision.reason


async def test_denies_disallowed_modality() -> None:
    # BROWSER is allowed but API is not, on the same source.
    decision = await _engine().evaluate(_ctx(Source.PUBLIC_JSONLD, modality=Modality.API))
    assert decision.allowed is False


async def test_denies_quarantined_source() -> None:
    engine = _engine()
    engine.quarantine(Source.PUBLIC_JSONLD)
    decision = await engine.evaluate(_ctx(Source.PUBLIC_JSONLD))
    assert decision.allowed is False
    assert "quarantined" in decision.reason


async def test_denies_when_kill_switch_engaged() -> None:
    engine = _engine()
    engine.set_kill_switch(True)
    assert await engine.kill_switch_engaged() is True
    decision = await engine.evaluate(_ctx(Source.PUBLIC_JSONLD))
    assert decision.allowed is False
    assert "kill switch" in decision.reason.lower()


async def test_store_backed_policy_reads_a_fresh_tenant_freeze() -> None:
    """A per-tenant data-plane flip denies the next guard without a deployment (ADR-004)."""
    tenant_id = uuid4()
    reader = MockPolicySnapshotReader(
        {
            Source.PUBLIC_JSONLD: SourcePolicy(
                source=Source.PUBLIC_JSONLD,
                automation_allowed={Modality.BROWSER: True},
            )
        }
    )
    engine = StoreBackedPolicyEngine(reader)
    context = PolicyContext(
        tenant_id=tenant_id,
        source=Source.PUBLIC_JSONLD,
        modality=Modality.BROWSER,
        action="register",
    )

    assert (await engine.evaluate(context)).allowed is True
    reader.tenant_kill_switches[tenant_id] = True

    denied = await engine.evaluate(context)
    assert denied.allowed is False
    assert "kill switch" in denied.reason.lower()
    assert await engine.kill_switch_engaged(tenant_id) is True


async def test_store_backed_policy_denies_when_its_durable_reader_is_unavailable() -> None:
    """A policy-store fault cannot extend autonomy past the pre-mutate guard (NFR-15)."""
    reader = MockPolicySnapshotReader()
    reader.unavailable = True
    engine = StoreBackedPolicyEngine(reader)

    denied = await engine.evaluate(_ctx(Source.PUBLIC_JSONLD))

    assert denied.allowed is False
    assert "unavailable" in denied.reason
    assert await engine.kill_switch_engaged() is True


async def test_in_memory_pacer_projects_a_wait_without_sleeping() -> None:
    """Pacer saturation becomes workflow-owned timing, never an activity-worker sleep (ADR-005)."""
    now = 100.0
    pacer = InMemoryPacer(rate_per_sec=10.0, burst=1, clock=lambda: now)

    request = _pacer_request()
    first = await pacer.acquire(request)
    second = await pacer.acquire(request)

    assert first.status is PacerLeaseStatus.GRANTED
    assert second.status is PacerLeaseStatus.WAIT
    assert second.retry_after_seconds == 0.1


async def test_in_memory_pacer_uses_a_catalog_http_get_single_token_interval() -> None:
    """A catalog GET ignores ordinary source shapes and refills only after its interval (ADR-005)."""
    now = [100.0]
    pacer = InMemoryPacer(rate_per_sec=100.0, burst=10, clock=lambda: now[0])
    request = PacerRequest(
        source=Source.PUBLIC_JSONLD,
        quota_scope="catalog:public-jsonld",
        operation=PacerOperation.CATALOG_HTTP_GET,
        catalog_min_interval_ms=250,
    )

    first = await pacer.acquire(request)
    second = await pacer.acquire(request)
    now[0] += 0.249
    almost_ready = await pacer.acquire(request)
    now[0] += 0.002
    ready = await pacer.acquire(request)

    assert first.status is PacerLeaseStatus.GRANTED
    assert second.status is PacerLeaseStatus.WAIT
    assert second.retry_after_seconds == pytest.approx(0.25)
    assert almost_ready.status is PacerLeaseStatus.WAIT
    assert almost_ready.retry_after_seconds == pytest.approx(0.001)
    assert ready.status is PacerLeaseStatus.GRANTED


async def test_in_memory_pacer_honors_a_shared_source_backoff() -> None:
    now = 100.0
    pacer = InMemoryPacer(rate_per_sec=10.0, burst=1, clock=lambda: now)

    request = _pacer_request()
    await pacer.observe_backoff(request, retry_after_seconds=30.0)
    deferred = await pacer.acquire(request)

    assert deferred.status is PacerLeaseStatus.WAIT
    assert deferred.retry_after_seconds == 30.0


class _UnavailableRedis:
    async def eval(self, *_: object) -> object:
        raise aioredis.ConnectionError("fixture Redis outage")


class _ScriptUnavailableRedis:
    async def eval(self, *_: object) -> object:
        raise aioredis.ResponseError("fixture scripts disabled")


async def test_redis_pacer_outage_never_fails_open() -> None:
    """A Redis transport outage returns a durable wait projection before any source call (ADR-005)."""
    pacer = RedisPacer(
        rate_per_sec=5.0,
        burst=10,
        unavailable_retry_seconds=2.0,
        redis_client=cast(aioredis.Redis, _UnavailableRedis()),
    )

    lease = await pacer.acquire(_pacer_request())

    assert lease.status is PacerLeaseStatus.WAIT
    assert lease.retry_after_seconds == 2.0
    assert "unavailable" in lease.detail


async def test_meetup_app_scope_refuses_an_unidentified_fair_queue_item() -> None:
    """The G2 contingency cannot bypass global fairness when a legacy caller omits lane identity."""
    pacer = RedisPacer(
        rate_per_sec=5.0,
        burst=10,
        unavailable_retry_seconds=2.0,
        redis_client=cast(aioredis.Redis, _UnavailableRedis()),
        meetup_app_quota_scope="fixture-app",
    )

    lease = await pacer.acquire(_pacer_request(Source.MEETUP, "legacy-token"))

    assert lease.status is PacerLeaseStatus.WAIT
    assert lease.retry_after_seconds == 2.0
    assert "stable queue_item_id" in lease.detail


async def test_meetup_app_scope_never_falls_back_to_a_credential_bucket_when_scripts_are_unavailable() -> (
    None
):
    """A fair-queue scripting outage remains a durable delay rather than a per-token bypass."""
    pacer = RedisPacer(
        rate_per_sec=5.0,
        burst=10,
        unavailable_retry_seconds=2.0,
        redis_client=cast(aioredis.Redis, _ScriptUnavailableRedis()),
        meetup_app_quota_scope="fixture-app",
    )
    request = PacerRequest(
        source=Source.MEETUP,
        quota_scope="legacy-token",
        operation=PacerOperation.REGISTRATION_MUTATION,
        tenant_id=uuid4(),
        queue_item_id="workflow-queue-item",
    )

    lease = await pacer.acquire(request)

    assert lease.status is PacerLeaseStatus.WAIT
    assert lease.retry_after_seconds == 2.0
    assert "scheduler unavailable" in lease.detail


async def test_pacer_auto_mode_keeps_non_mock_composition_on_shared_redis() -> None:
    """The explicit local test override cannot silently weaken a non-mock deployment (ADR-005)."""
    local = _build_pacer(Settings(mock_cloud=True, pacer_backend="auto"))
    production = _build_pacer(Settings(mock_cloud=False, pacer_backend="auto"))
    forced_redis = _build_pacer(Settings(mock_cloud=True, pacer_backend="redis"))

    assert isinstance(local, InMemoryPacer)
    assert isinstance(production, RedisPacer)
    assert isinstance(forced_redis, RedisPacer)
    await production.aclose()
    await forced_redis.aclose()


def test_meetup_per_app_mode_requires_owner_validated_g2_evidence() -> None:
    """G2 remains a hard gate before ADR-005's global Meetup fairness contingency can start."""
    with pytest.raises(ValueError, match="G2"):
        _build_pacer(
            Settings(
                mock_cloud=True,
                pacer_backend="memory",
                meetup_quota_scope_mode="per_app",
            )
        )


def test_validated_meetup_per_app_mode_forces_shared_redis() -> None:
    """The G2-gated app bucket cannot silently run in a process-local mock Pacer (ADR-005)."""
    settings = Settings(
        mock_cloud=True,
        pacer_backend="memory",
        meetup_quota_scope_mode="per_app",
        meetup_app_g2_validated=True,
        meetup_app_quota_scope="meetup-app-fixture",
        meetup_app_degrade_after_seconds=301.0,
    )

    with patch("events_concierge.composition.RedisPacer") as redis_pacer:
        _build_pacer(settings)

    redis_pacer.assert_called_once_with(
        settings.redis_url,
        rate_per_sec=settings.pacer_rate_per_second,
        burst=settings.pacer_burst,
        unavailable_retry_seconds=settings.pacer_unavailable_retry_seconds,
        source_budgets=default_source_budgets(),
        meetup_app_quota_scope="meetup-app-fixture",
        meetup_app_degrade_after_seconds=301.0,
    )


def test_non_mock_composition_rejects_process_local_pacing() -> None:
    with pytest.raises(ValueError, match="shared Redis"):
        _build_pacer(Settings(mock_cloud=False, pacer_backend="memory"))


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_redis_pacer_rejects_non_finite_retry_or_rate_configuration(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        RedisPacer(rate_per_sec=value, burst=1)
    with pytest.raises(ValueError, match="finite"):
        RedisPacer(rate_per_sec=1.0, burst=1, unavailable_retry_seconds=value)


async def test_ratified_source_profiles_apply_to_typed_requests() -> None:
    """P1b carries source, opaque scope, operation, and cost before any live adapter is enabled."""
    now = 100.0
    pacer = InMemoryPacer(clock=lambda: now, source_budgets=default_source_budgets())
    meetup = _pacer_request(Source.MEETUP, "credential-a", cost=500)
    luma = _pacer_request(Source.LUMA, "calendar-a", cost=100)
    ticketmaster = _pacer_request(Source.TICKETMASTER, "app-key", cost=2)

    assert (await pacer.acquire(meetup)).status is PacerLeaseStatus.GRANTED
    assert (await pacer.acquire(meetup)).status is PacerLeaseStatus.WAIT
    assert (await pacer.acquire(luma)).status is PacerLeaseStatus.GRANTED
    assert (await pacer.acquire(luma)).status is PacerLeaseStatus.WAIT
    assert (await pacer.acquire(ticketmaster)).status is PacerLeaseStatus.GRANTED
    assert (await pacer.acquire(ticketmaster)).status is PacerLeaseStatus.WAIT
