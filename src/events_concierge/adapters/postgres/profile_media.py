"""Distributed media/index serialization without monopolizing the application pool."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from uuid import UUID
from weakref import WeakKeyDictionary

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncEngine

from ...application.tenant_effects import settle_tenant_effect
from ...infra.db import get_engine
from ...ports.profile_avatar import ProfileMediaMutationBusyError

_MIN_POOL_CAPACITY = 2
_MIN_LOCK_TIMEOUT_SECONDS = 0.1
_MAX_LOCK_TIMEOUT_SECONDS = 30.0
_MAX_MUTATION_TIMEOUT_SECONDS = 60.0
_ADMISSIONS: WeakKeyDictionary[AsyncEngine, asyncio.Semaphore] = WeakKeyDictionary()


class PostgresProfileMediaMutationGuard:
    """Hold a separate per-tenant lock across the complete object/index sequence.

    All guards using one engine share a single bounded admission slot, acquired before checking
    out a connection, so waiting media requests never pin every
    pool connection while an admitted request needs a second one for erasure authority or index
    access. Distinct application processes coordinate through PostgreSQL, not this local slot.

    Lock order is media mutation -> erasure authority. The index uses its own transaction outside
    erasure authority but inside this media guard, avoiding same-key upload/delete ABA races and
    trigger re-entry deadlocks. Neither this adapter nor its callers change avatar key format.
    """

    def __init__(
        self,
        *,
        pool_capacity: int,
        lock_timeout_seconds: float,
        mutation_timeout_seconds: float,
        engine: AsyncEngine | None = None,
    ) -> None:
        if pool_capacity < _MIN_POOL_CAPACITY:
            raise ValueError(
                "profile media mutations require a database pool capacity of at least 2"
            )
        if (
            not math.isfinite(lock_timeout_seconds)
            or not _MIN_LOCK_TIMEOUT_SECONDS <= lock_timeout_seconds <= _MAX_LOCK_TIMEOUT_SECONDS
        ):
            raise ValueError("profile media lock timeout must be between 0.1 and 30 seconds")
        if (
            not math.isfinite(mutation_timeout_seconds)
            or not 0 < mutation_timeout_seconds <= _MAX_MUTATION_TIMEOUT_SECONDS
        ):
            raise ValueError(
                "profile media mutation timeout must be positive and at most 60 seconds"
            )
        self._lock_timeout_seconds = lock_timeout_seconds
        self._mutation_timeout_seconds = mutation_timeout_seconds
        self._engine = engine

    async def run[T](self, tenant_id: UUID, mutation: Callable[[], Awaitable[T]]) -> T:
        if not isinstance(tenant_id, UUID):
            raise TypeError("profile media tenant_id must be a UUID")
        engine = self._engine if self._engine is not None else get_engine()
        # No await separates lookup from creation. Even two independently constructed containers
        # sharing one pool cannot each pin its last connection while waiting for nested I/O.
        admission = _ADMISSIONS.setdefault(engine, asyncio.Semaphore(1))
        try:
            await asyncio.wait_for(admission.acquire(), timeout=self._lock_timeout_seconds)
        except TimeoutError as error:
            raise ProfileMediaMutationBusyError(
                "profile media admission deadline expired"
            ) from error
        try:
            async with engine.begin() as session:
                await session.execute(
                    text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                    {"tenant_id": str(tenant_id)},
                )
                await session.execute(
                    text("SELECT set_config('lock_timeout', :timeout, true)"),
                    {"timeout": f"{int(self._lock_timeout_seconds * 1000)}ms"},
                )
                try:
                    await session.execute(
                        text("""SELECT pg_advisory_xact_lock(hashtextextended(
                            'profile-media-mutation:' || CAST(:tenant_id AS text), 0
                        ))"""),
                        {"tenant_id": tenant_id},
                    )
                except DBAPIError as error:
                    sqlstate = getattr(error.orig, "sqlstate", None) or getattr(
                        error.orig, "pgcode", None
                    )
                    if sqlstate in {"55P03", "57014"}:
                        raise ProfileMediaMutationBusyError(
                            "profile media lock deadline expired"
                        ) from error
                    raise
                # A cancelled request must not release the media lock while a blocking GCS SDK
                # operation or its later index write is still running in a child task.
                return await settle_tenant_effect(
                    mutation(), timeout_seconds=self._mutation_timeout_seconds
                )
        except PoolTimeoutError as error:
            raise ProfileMediaMutationBusyError(
                "profile media database admission deadline expired"
            ) from error
        finally:
            admission.release()
