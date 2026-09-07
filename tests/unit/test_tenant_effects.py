"""Cancellation and finite-deadline invariants for tenant external-effect authority."""

from __future__ import annotations

import asyncio
import gc
import math
import threading
import weakref
from uuid import uuid4

import pytest

from events_concierge.application.tenant_effects import DirectTenantEffectAuthority
from events_concierge.ports.tenant_effects import (
    TenantEffectAuthorityConfig,
    TenantEffectKind,
    TenantEffectMode,
    TenantEffectNestingError,
    TenantEffectRequest,
    TenantEffectTimedOutError,
)


def _request(*, timeout_seconds: float = 1.0) -> TenantEffectRequest:
    return TenantEffectRequest(
        tenant_id=uuid4(),
        kind=TenantEffectKind.CLAIM_CHECK_WRITE,
        timeout_seconds=timeout_seconds,
    )


@pytest.mark.parametrize(
    "timeout_seconds",
    (0.0, 0.09, 60.01, math.inf, -math.inf, math.nan),
)
def test_effect_deadline_must_be_hard_bounded_and_finite(timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="finite and between"):
        _request(timeout_seconds=timeout_seconds)


@pytest.mark.parametrize("lock_timeout_seconds", (0.0, 30.01, math.inf, math.nan))
def test_lock_deadline_must_be_hard_bounded_and_finite(lock_timeout_seconds: float) -> None:
    with pytest.raises(ValueError, match="finite and between"):
        TenantEffectAuthorityConfig(lock_timeout_seconds=lock_timeout_seconds)


async def test_cancellation_waits_for_a_started_executor_thread_to_settle() -> None:
    """Cancelling the asyncio waiter must not let its real SDK-like thread escape the guard."""
    authority = DirectTenantEffectAuthority()
    thread_started = threading.Event()
    allow_thread_to_finish = threading.Event()
    thread_finished = threading.Event()

    def blocking_effect() -> str:
        thread_started.set()
        allow_thread_to_finish.wait(timeout=2.0)
        thread_finished.set()
        return "settled"

    call = asyncio.create_task(
        authority.run(
            _request(),
            lambda: asyncio.to_thread(blocking_effect),
        )
    )
    assert await asyncio.to_thread(thread_started.wait, 1.0)

    call.cancel()
    await asyncio.sleep(0)
    assert not call.done()
    assert not thread_finished.is_set()

    allow_thread_to_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    assert thread_finished.is_set()


async def test_deadline_breach_is_reported_only_after_the_effect_really_settles() -> None:
    authority = DirectTenantEffectAuthority()
    effect_started = asyncio.Event()
    allow_effect_to_finish = asyncio.Event()
    effect_finished = asyncio.Event()

    async def slow_effect() -> None:
        effect_started.set()
        await allow_effect_to_finish.wait()
        effect_finished.set()

    call = asyncio.create_task(authority.run(_request(timeout_seconds=0.1), slow_effect))
    await effect_started.wait()
    await asyncio.sleep(0.15)
    assert not call.done()
    assert not effect_finished.is_set()

    allow_effect_to_finish.set()
    with pytest.raises(TenantEffectTimedOutError, match="drained"):
        await call
    assert effect_finished.is_set()


async def test_provider_failure_propagates_after_settlement() -> None:
    async def fail() -> None:
        raise RuntimeError("fixture provider failure")

    with pytest.raises(RuntimeError, match="fixture provider failure"):
        await DirectTenantEffectAuthority().run(_request(), fail)


async def test_exact_same_authority_tenant_and_mode_can_nest() -> None:
    authority = DirectTenantEffectAuthority()
    calls: list[str] = []
    request = _request()

    async def inner() -> str:
        calls.append("inner")
        return "nested"

    async def outer() -> str:
        calls.append("outer")
        return await authority.run(request, inner)

    assert await authority.run(request, outer) == "nested"
    assert calls == ["outer", "inner"]


async def test_nested_authority_rejects_instance_tenant_and_mode_changes() -> None:
    authority = DirectTenantEffectAuthority()
    other = DirectTenantEffectAuthority()
    request = _request()
    attempts = (
        (other, request),
        (
            authority,
            TenantEffectRequest(
                tenant_id=uuid4(),
                kind=request.kind,
                timeout_seconds=1.0,
            ),
        ),
        (
            authority,
            TenantEffectRequest(
                tenant_id=request.tenant_id,
                kind=request.kind,
                timeout_seconds=1.0,
                mode=TenantEffectMode.ERASURE_CLEANUP,
            ),
        ),
    )

    async def no_effect() -> None:
        raise AssertionError("mismatched nested effect must not start")

    async def outer() -> None:
        for nested_authority, nested_request in attempts:
            with pytest.raises(TenantEffectNestingError):
                await nested_authority.run(nested_request, no_effect)

    await authority.run(request, outer)


async def test_spawned_nested_effect_is_drained_before_outer_scope_exits() -> None:
    authority = DirectTenantEffectAuthority()
    request = _request()
    nested_started = asyncio.Event()
    release_nested = asyncio.Event()
    nested_task: asyncio.Task[None] | None = None

    async def nested() -> None:
        nested_started.set()
        await release_nested.wait()

    async def outer() -> None:
        nonlocal nested_task
        nested_task = asyncio.create_task(authority.run(request, nested))
        await nested_started.wait()

    guarded = asyncio.create_task(authority.run(request, outer))
    await nested_started.wait()
    await asyncio.sleep(0)
    assert not guarded.done()
    release_nested.set()
    await guarded
    assert nested_task is not None
    await nested_task


async def test_unreferenced_admitted_nested_task_is_retained_until_settlement() -> None:
    authority = DirectTenantEffectAuthority()
    request = _request()
    nested_started = asyncio.Event()
    release_nested = asyncio.Event()
    nested_ref: weakref.ReferenceType[asyncio.Task[None]] | None = None

    async def nested() -> None:
        nested_started.set()
        await release_nested.wait()

    async def outer() -> None:
        nonlocal nested_ref
        nested_ref = weakref.ref(asyncio.create_task(authority.run(request, nested)))
        await nested_started.wait()

    guarded = asyncio.create_task(authority.run(request, outer))
    await nested_started.wait()
    gc.collect()
    assert nested_ref is not None
    assert nested_ref() is not None
    assert guarded.done() is False

    release_nested.set()
    await guarded


async def test_context_copied_to_an_outliving_child_is_invalidated_on_exit() -> None:
    authority = DirectTenantEffectAuthority()
    request = _request()
    let_child_attempt = asyncio.Event()
    child: asyncio.Task[None] | None = None
    effect_called = False

    async def nested_effect() -> None:
        nonlocal effect_called
        effect_called = True

    async def escaped_child() -> None:
        await let_child_attempt.wait()
        await authority.run(request, nested_effect)

    async def outer() -> None:
        nonlocal child
        child = asyncio.create_task(escaped_child())

    await authority.run(request, outer)
    let_child_attempt.set()
    assert child is not None
    with pytest.raises(TenantEffectNestingError, match="no longer active"):
        await child
    assert not effect_called
