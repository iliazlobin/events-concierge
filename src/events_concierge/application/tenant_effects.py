"""Cancellation-safe lifetime control for already-started tenant external effects."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from uuid import UUID

from ..ports.tenant_effects import (
    TenantEffectAuthority,
    TenantEffectMode,
    TenantEffectNestingError,
    TenantEffectRequest,
    TenantEffectTimedOutError,
)


@dataclass(slots=True)
class _TenantEffectLease:
    """Mutable capability shared by ContextVar copies in descendant asyncio tasks."""

    authority: TenantEffectAuthority
    tenant_id: UUID
    mode: TenantEffectMode
    active: bool = True
    accepting_nested: bool = True
    nested_active: int = 0
    nested_tasks: dict[asyncio.Task[object], int] = field(default_factory=dict)
    nested_settled: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.nested_settled.set()


_CURRENT_TENANT_EFFECT_LEASE: ContextVar[_TenantEffectLease | None] = ContextVar(
    "events_concierge_tenant_effect_lease",
    default=None,
)


async def settle_tenant_effect[T](effect: Awaitable[T], *, timeout_seconds: float) -> T:
    """Drain ``effect`` before returning, even when its caller is cancelled or its deadline passes.

    Cancellation of ``asyncio.to_thread`` only cancels the asyncio waiter; it does not stop the
    underlying SDK thread.  Consequently this helper never cancels the child task.  A finite timer
    records a deadline breach, while the authority (and therefore its PostgreSQL advisory lock)
    remains held until the child really returns.  Concrete HTTP/SDK adapters must also configure a
    finite transport timeout; returning early here would be a cross-system erasure race.
    """
    task = asyncio.ensure_future(effect)
    timer = asyncio.create_task(asyncio.sleep(timeout_seconds))
    caller_cancelled = False
    deadline_exceeded = False
    try:
        while not task.done():
            try:
                completed, _ = await asyncio.wait(
                    (task, timer),
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except asyncio.CancelledError:
                caller_cancelled = True
                continue
            if timer in completed and not task.done():
                deadline_exceeded = True
                caller_cancelled = await _drain(task) or caller_cancelled
                break

        if caller_cancelled:
            # Observe a provider exception so asyncio does not report an un-retrieved task, but
            # preserve cancellation as the caller-visible outcome after the lock-safe drain.
            _observe_failure(task)
            raise asyncio.CancelledError
        if deadline_exceeded:
            _observe_failure(task)
            raise TenantEffectTimedOutError(
                "tenant external effect exceeded its finite deadline and was drained"
            )
        return task.result()
    finally:
        timer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await timer


async def _drain[T](task: asyncio.Future[T]) -> bool:
    """Return whether more caller cancellation arrived while a child was settling."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    return cancelled


def _observe_failure[T](task: asyncio.Future[T]) -> None:
    if not task.cancelled():
        with contextlib.suppress(Exception):
            task.result()


class DirectTenantEffectAuthority:
    """Explicit offline/test authority with the same cancellation-safe task draining.

    Production composition never selects this implementation. It lets database-free application
    tests retain the effect-lifetime contract without pretending to provide a durable fence.
    """

    async def run[T](
        self,
        request: TenantEffectRequest,
        effect: Callable[[], Awaitable[T]],
    ) -> T:
        lease = _nested_lease(self, request)
        if lease is not None:
            try:
                return await settle_tenant_effect(
                    effect(), timeout_seconds=request.timeout_seconds
                )
            finally:
                _leave_nested_lease(lease)
        async with tenant_effect_lease_scope(self, request):
            return await settle_tenant_effect(effect(), timeout_seconds=request.timeout_seconds)

def _nested_lease(
    authority: TenantEffectAuthority,
    request: TenantEffectRequest,
) -> _TenantEffectLease | None:
    lease = _CURRENT_TENANT_EFFECT_LEASE.get()
    if lease is None:
        return None
    if not lease.active or not lease.accepting_nested:
        raise TenantEffectNestingError("tenant effect lock capability is no longer active")
    if lease.authority is not authority:
        raise TenantEffectNestingError("nested tenant effect used a different authority instance")
    if lease.tenant_id != request.tenant_id:
        raise TenantEffectNestingError("nested tenant effect crossed tenant authority")
    if lease.mode is not request.mode:
        raise TenantEffectNestingError("nested tenant effect changed authority mode")
    task = asyncio.current_task()
    if task is None:
        raise RuntimeError("nested tenant effect has no active asyncio task")
    lease.nested_active += 1
    lease.nested_tasks[task] = lease.nested_tasks.get(task, 0) + 1
    lease.nested_settled.clear()
    return lease


def _leave_nested_lease(lease: _TenantEffectLease) -> None:
    task = asyncio.current_task()
    if task is None or task not in lease.nested_tasks:
        raise RuntimeError("tenant effect nested task ownership was lost")
    depth = lease.nested_tasks[task]
    if depth == 1:
        del lease.nested_tasks[task]
    else:
        lease.nested_tasks[task] = depth - 1
    lease.nested_active -= 1
    if lease.nested_active < 0:
        raise RuntimeError("tenant effect nested lease count underflow")
    if lease.nested_active == 0:
        lease.nested_settled.set()


@asynccontextmanager
async def tenant_effect_lease_scope(
    authority: TenantEffectAuthority,
    request: TenantEffectRequest,
) -> AsyncIterator[None]:
    """Publish one exact mutable capability and invalidate every copied context before exit."""
    if _CURRENT_TENANT_EFFECT_LEASE.get() is not None:
        raise TenantEffectNestingError("an outer tenant effect lock capability is already installed")
    lease = _TenantEffectLease(authority, request.tenant_id, request.mode)
    token = _CURRENT_TENANT_EFFECT_LEASE.set(lease)
    try:
        yield
    finally:
        try:
            await _close_tenant_effect_lease(lease)
        finally:
            _reset_tenant_effect_lease(token)


async def _close_tenant_effect_lease(lease: _TenantEffectLease) -> None:
    """Stop new descendants, drain those already admitted, then invalidate all copied contexts."""
    lease.accepting_nested = False
    cancelled = False
    while lease.nested_active:
        try:
            await asyncio.shield(lease.nested_settled.wait())
        except asyncio.CancelledError:
            cancelled = True
    if lease.nested_tasks:
        raise RuntimeError("tenant effect nested task inventory did not settle")
    lease.active = False
    if cancelled:
        raise asyncio.CancelledError


def _reset_tenant_effect_lease(token: Token[_TenantEffectLease | None]) -> None:
    _CURRENT_TENANT_EFFECT_LEASE.reset(token)
