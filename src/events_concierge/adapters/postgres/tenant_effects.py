"""PostgreSQL advisory-lock authority for tenant-scoped external mutations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ...application.tenant_effects import (
    _leave_nested_lease,
    _nested_lease,
    settle_tenant_effect,
    tenant_effect_lease_scope,
)
from ...infra.db import tenant_session_scope
from ...ports.tenant_effects import (
    TenantEffectAuthority,
    TenantEffectAuthorityConfig,
    TenantEffectCleanupNotAuthorizedError,
    TenantEffectFencedError,
    TenantEffectKind,
    TenantEffectLockTimeoutError,
    TenantEffectMode,
    TenantEffectRequest,
)

T = TypeVar("T")


class PostgresTenantEffectAuthority:
    """Serialize external effects with ``fn_begin_account_erasure`` for one tenant.

    The advisory-key expression is intentionally byte-for-byte equivalent to migration 0108.  A
    transaction stays open across the complete external operation.  The task is shielded and
    drained before scope exit, so cancellation cannot return the connection (and release the lock)
    while a blocking SDK thread or provider coroutine is still running.
    """

    def __init__(self, config: TenantEffectAuthorityConfig | None = None) -> None:
        self._config = config or TenantEffectAuthorityConfig()

    async def run(
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
        async with tenant_session_scope(request.tenant_id) as session:
            lock_timeout_ms = int(self._config.lock_timeout_seconds * 1000)
            await session.execute(
                text("SELECT set_config('lock_timeout', :timeout, true)"),
                {"timeout": f"{lock_timeout_ms}ms"},
            )
            try:
                await session.execute(
                    text(
                        """SELECT pg_advisory_xact_lock(
                               hashtextextended(
                                   'account-erasure:' || CAST(:tenant_id AS text), 0
                               )
                           )"""
                    ),
                    {"tenant_id": request.tenant_id},
                )
            except DBAPIError as error:
                if _is_lock_timeout(error):
                    raise TenantEffectLockTimeoutError(
                        "tenant erasure authority lock deadline expired"
                    ) from None
                raise

            tombstone_exists = bool(
                (
                    await session.execute(
                        text(
                            """SELECT EXISTS (
                                   SELECT 1
                                   FROM public.account_erasure_requests AS erasure
                                   WHERE erasure.tenant_id = :tenant_id
                               )"""
                        ),
                        {"tenant_id": request.tenant_id},
                    )
                ).scalar_one()
            )
            if request.mode is TenantEffectMode.LIVE and tombstone_exists:
                raise TenantEffectFencedError("tenant account is fenced for erasure")
            if request.mode is TenantEffectMode.ERASURE_CLEANUP and not tombstone_exists:
                raise TenantEffectCleanupNotAuthorizedError(
                    "tenant erasure cleanup requires a durable erasure tombstone"
                )
            async with tenant_effect_lease_scope(self, request):
                return await settle_tenant_effect(
                    effect(),
                    timeout_seconds=request.timeout_seconds,
                )

class PostgresTenantExternalEffectDrain:
    """Post-begin barrier proving the tombstone exists and no live effect still owns its lock."""

    def __init__(
        self,
        authority: TenantEffectAuthority,
        *,
        timeout_seconds: float,
    ) -> None:
        self._authority = authority
        # Reuse request validation at construction so the erasure worker cannot enter its retry
        # loop with an unbounded cleanup deadline.
        TenantEffectRequest(
            tenant_id=UUID(int=0),
            kind=TenantEffectKind.ERASURE_DRAIN,
            timeout_seconds=timeout_seconds,
            mode=TenantEffectMode.ERASURE_CLEANUP,
        )
        self._timeout_seconds = timeout_seconds

    async def drain_tenant_effects(self, tenant_id: UUID) -> None:
        await self._authority.run(
            TenantEffectRequest(
                tenant_id=tenant_id,
                kind=TenantEffectKind.ERASURE_DRAIN,
                timeout_seconds=self._timeout_seconds,
                mode=TenantEffectMode.ERASURE_CLEANUP,
            ),
            _settled_barrier,
        )


async def _settled_barrier() -> None:
    """The acquired lock and observed tombstone are the complete barrier effect."""


def _is_lock_timeout(error: DBAPIError) -> bool:
    """Recognize PostgreSQL lock timeout without persisting driver or statement text."""
    original = error.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    # PostgreSQL uses query_canceled for both statement_timeout and lock_timeout.  This adapter
    # sets only the transaction-local lock timeout before the single lock statement.
    return sqlstate in {"55P03", "57014"}
