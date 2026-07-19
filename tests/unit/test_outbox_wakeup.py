"""Offline durability tests for the advisory PostgreSQL outbox wake-up boundary (ADR-009)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, ClassVar, cast

import psycopg
import pytest

from events_concierge.adapters.postgres import outbox_wakeup
from events_concierge.adapters.postgres.outbox_wakeup import PostgresOutboxWakeup
from events_concierge.ports.outbox import OutboxWakeupStatus


@dataclass(frozen=True, slots=True)
class _Notification:
    channel: str


class _Connection:
    """Minimal psycopg seam: no real database is necessary for listener failure semantics."""

    def __init__(self, notifications: list[_Notification] | None = None) -> None:
        self.closed = False
        self.commands: list[str] = []
        self.notifications = notifications or []
        self.notifies_error: BaseException | None = None

    async def execute(self, command: str) -> None:
        self.commands.append(command)

    async def close(self) -> None:
        self.closed = True

    async def notifies(self, *, stop_after: int, **options: float) -> AsyncIterator[_Notification]:
        del options
        if self.notifies_error is not None:
            raise self.notifies_error
        for yielded, notification in enumerate(self.notifications):
            if yielded >= stop_after:
                return
            yield notification


class _ConnectionFactory:
    connections: ClassVar[list[_Connection]] = []
    connect_error: ClassVar[BaseException | None] = None
    calls: ClassVar[list[tuple[str, bool]]] = []

    @classmethod
    async def connect(cls, dsn: str, *, autocommit: bool) -> _Connection:
        cls.calls.append((dsn, autocommit))
        if cls.connect_error is not None:
            raise cls.connect_error
        return cls.connections.pop(0)


def _install_factory(monkeypatch: pytest.MonkeyPatch, *connections: _Connection) -> None:
    _ConnectionFactory.connections = list(connections)
    _ConnectionFactory.connect_error = None
    _ConnectionFactory.calls = []
    monkeypatch.setattr(outbox_wakeup, "AsyncConnection", cast(Any, _ConnectionFactory))


async def test_listener_uses_dedicated_autocommit_connection_and_returns_committed_wakeup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The notifier establishes LISTEN before draining and treats its fixed channel as advisory (ADR-009)."""
    connection = _Connection([_Notification("ec_outbox_ready")])
    _install_factory(monkeypatch, connection)
    wakeup = PostgresOutboxWakeup("postgresql+psycopg://ec_app:secret@db:5433/ec")

    started = await wakeup.start()
    result = await wakeup.wait(2.0)

    assert started.status is OutboxWakeupStatus.LISTENING
    assert result.status is OutboxWakeupStatus.NOTIFIED
    assert _ConnectionFactory.calls == [("postgresql://ec_app:secret@db:5433/ec", True)]
    assert connection.commands == ["LISTEN ec_outbox_ready"]
    await wakeup.aclose()
    assert connection.closed is True


async def test_listener_connect_failure_waits_the_poll_interval_without_disabling_durable_polling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listener outage is a bounded degraded mode, not a source of outbox loss (FR-8.9)."""
    _install_factory(monkeypatch)
    _ConnectionFactory.connect_error = psycopg.OperationalError("listener unavailable")
    slept: list[float] = []
    wakeup = PostgresOutboxWakeup(
        "postgresql+psycopg://ec_app:secret@db:5433/ec",
        sleep=lambda seconds: _record_sleep(slept, seconds),
    )

    result = await wakeup.wait(2.0)

    assert result.status is OutboxWakeupStatus.POLL_FALLBACK
    assert "unavailable" in result.detail
    assert len(slept) == 1
    assert 0.0 < slept[0] <= 2.0


async def test_listener_loss_closes_stale_connection_and_reconnects_on_the_next_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missed signal after a process or socket restart converges through the next poll/reconnect (ADR-009)."""
    lost = _Connection()
    lost.notifies_error = psycopg.OperationalError("server closed the connection")
    recovered = _Connection([_Notification("ec_outbox_ready")])
    _install_factory(monkeypatch, lost, recovered)
    slept: list[float] = []
    wakeup = PostgresOutboxWakeup(
        "postgresql+psycopg://ec_app:secret@db:5433/ec",
        sleep=lambda seconds: _record_sleep(slept, seconds),
    )

    assert (await wakeup.start()).status is OutboxWakeupStatus.LISTENING
    degraded = await wakeup.wait(1.0)
    recovered_result = await wakeup.wait(1.0)

    assert degraded.status is OutboxWakeupStatus.POLL_FALLBACK
    assert lost.closed is True
    assert recovered_result.status is OutboxWakeupStatus.NOTIFIED
    assert len(_ConnectionFactory.calls) == 2
    assert len(slept) == 1


@pytest.mark.parametrize("timeout_seconds", [0.0, -1.0, float("inf"), float("nan")])
async def test_listener_rejects_an_unbounded_poll_interval(timeout_seconds: float) -> None:
    """The advisory path cannot turn an idle relay cycle into an unbounded wait."""
    wakeup = PostgresOutboxWakeup("postgresql+psycopg://ec_app:secret@db:5433/ec")

    with pytest.raises(ValueError, match="finite and positive"):
        await wakeup.wait(timeout_seconds)


async def _record_sleep(slept: list[float], seconds: float) -> None:
    slept.append(seconds)
