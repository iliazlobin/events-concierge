"""Push-wakeup boundary for the transactional outbox (FR-6.6/FR-8.9, ADR-009).

LISTEN/NOTIFY lowers idle notification latency but is never the source of truth: a notifier must
continue polling the durable queue when the listener is unavailable, restarted, or misses a signal.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class OutboxWakeupStatus(StrEnum):
    """One listener lifecycle or bounded idle-wait outcome (ADR-009)."""

    LISTENING = "listening"
    NOTIFIED = "notified"
    TIMED_OUT = "timed_out"
    POLL_FALLBACK = "poll_fallback"


@dataclass(frozen=True, slots=True)
class OutboxWakeupResult:
    """A secret-free wakeup result; PostgreSQL notification payloads are intentionally ignored."""

    status: OutboxWakeupStatus
    detail: str = ""

    @property
    def listening(self) -> bool:
        """Whether the dedicated listener remains usable after this result."""
        return self.status is not OutboxWakeupStatus.POLL_FALLBACK


class OutboxWakeupPort(Protocol):
    """Wait for a committed outbox insertion without weakening the durable poll path (ADR-009)."""

    async def start(self) -> OutboxWakeupResult:
        """Attempt to establish a dedicated listener before the notifier's first drain."""
        ...

    async def wait(self, timeout_seconds: float) -> OutboxWakeupResult:
        """Wake on a signal or return no later than the configured durable-poll interval."""
        ...

    async def aclose(self) -> None:
        """Close any dedicated listener connection during orderly worker shutdown."""
        ...
