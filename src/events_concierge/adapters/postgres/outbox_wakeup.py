"""Dedicated PostgreSQL LISTEN/NOTIFY wake-up adapter for the ADR-009 outbox relay.

The listener owns one autocommit psycopg connection rather than borrowing the SQLAlchemy request
pool: PostgreSQL session-scoped LISTEN state must survive relay transactions.  It is advisory only;
every transport or protocol failure returns a bounded poll-fallback result and never affects durable
outbox correctness (FR-6.6/FR-8.9, ADR-009).
"""

from __future__ import annotations

import asyncio
import math
import time
from collections.abc import Awaitable, Callable
from typing import Any, Final

import psycopg
from psycopg import AsyncConnection
from sqlalchemy.engine import make_url

from ...ports.outbox import OutboxWakeupPort, OutboxWakeupResult, OutboxWakeupStatus

_OUTBOX_WAKEUP_CHANNEL: Final = "ec_outbox_ready"


class PostgresOutboxWakeup(OutboxWakeupPort):
    """A reconnecting, bounded PostgreSQL listener that fails back to durable polling (ADR-009)."""

    def __init__(
        self,
        database_url: str,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        if not database_url:
            raise ValueError("database_url must not be empty")
        self._dsn = _listener_dsn(database_url)
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._connection: AsyncConnection[Any] | None = None

    async def start(self) -> OutboxWakeupResult:
        """Open an autocommit connection and LISTEN before the notifier's first drain."""
        existing = self._connection
        if existing is not None and not existing.closed:
            return OutboxWakeupResult(OutboxWakeupStatus.LISTENING)
        await self._close_connection()
        connection: AsyncConnection[Any] | None = None
        try:
            connection = await AsyncConnection.connect(self._dsn, autocommit=True)
            await connection.execute(f"LISTEN {_OUTBOX_WAKEUP_CHANNEL}")
        except (psycopg.Error, OSError, ValueError) as exc:
            await self._close_connection(connection)
            return _fallback_result(exc)
        if connection is None:
            return OutboxWakeupResult(
                OutboxWakeupStatus.POLL_FALLBACK,
                "PostgreSQL outbox listener did not open a connection",
            )
        self._connection = connection
        return OutboxWakeupResult(OutboxWakeupStatus.LISTENING)

    async def wait(self, timeout_seconds: float) -> OutboxWakeupResult:
        """Wait once for a committed insert, then return no later than the poll interval."""
        _validate_timeout(timeout_seconds)
        started_at = self._clock()
        connection = self._connection
        if connection is None or connection.closed:
            started = await self.start()
            if not started.listening:
                await self._sleep_remaining(started_at, timeout_seconds)
                return started
            connection = self._connection
        if connection is None:
            # The impossible-looking branch remains fail-safe if a custom connection implementation
            # changes state between `start()` and the local assignment.
            await self._sleep_remaining(started_at, timeout_seconds)
            return OutboxWakeupResult(
                OutboxWakeupStatus.POLL_FALLBACK,
                "PostgreSQL outbox listener did not retain a connection",
            )
        try:
            async for notification in connection.notifies(timeout=timeout_seconds, stop_after=1):
                if notification.channel == _OUTBOX_WAKEUP_CHANNEL:
                    return OutboxWakeupResult(OutboxWakeupStatus.NOTIFIED)
            return OutboxWakeupResult(OutboxWakeupStatus.TIMED_OUT)
        except (psycopg.Error, OSError, ValueError) as exc:
            await self._close_connection(connection)
            await self._sleep_remaining(started_at, timeout_seconds)
            return _fallback_result(exc)

    async def aclose(self) -> None:
        """Close the dedicated connection; a later worker start may reconnect cleanly."""
        await self._close_connection()

    async def _close_connection(self, connection: AsyncConnection[Any] | None = None) -> None:
        candidate = connection if connection is not None else self._connection
        if candidate is None:
            return
        if candidate is self._connection:
            self._connection = None
        try:
            await candidate.close()
        except (psycopg.Error, OSError, ValueError):
            return

    async def _sleep_remaining(self, started_at: float, timeout_seconds: float) -> None:
        remaining = max(timeout_seconds - (self._clock() - started_at), 0.0)
        if remaining > 0.0:
            await self._sleep(remaining)


def _listener_dsn(database_url: str) -> str:
    """Convert SQLAlchemy's async driver URL into the native psycopg listener DSN."""
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def _validate_timeout(timeout_seconds: float) -> None:
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
        raise ValueError("outbox wakeup timeout_seconds must be finite and positive")


def _fallback_result(exc: BaseException) -> OutboxWakeupResult:
    """Do not expose an exception across the worker boundary; polling remains authoritative."""
    return OutboxWakeupResult(
        OutboxWakeupStatus.POLL_FALLBACK,
        f"PostgreSQL outbox listener unavailable: {str(exc)[:500]}",
    )
