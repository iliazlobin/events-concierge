"""PostgreSQL cursor/lease storage for fixture-only central watch polling.

The table is a global public-event control plane like ADR-008's watch registry. It contains no
tenant contacts, credentials, raw provider payloads, or source URLs; timing/error fields exist only
to make coverage loss observable and restart-safe (FR-8.7a, NFR-17, ADR-008).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import text

from ...domain.enums import Source
from ...infra.db import system_session_scope
from ...ports.change_detection import (
    WatchedEvent,
    WatchPollLease,
    WatchPollState,
)

_ERROR_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,127}")
_MAX_LEASE_SECONDS = 3600


class _WatchPollClaimRow(Protocol):
    """Named lease projection returned by the guarded watch-poll capability."""

    canonical_event_id: UUID
    source: str
    attempt_count: int
    lease_token: str | None


class _WatchPollStateRow(Protocol):
    """Named SQL projection for public watch-poll state."""

    canonical_event_id: UUID
    source: str
    first_seen_at: datetime
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    consecutive_failures: int
    next_due_at: datetime
    attempt_count: int
    last_error_type: str | None


class PostgresWatchPollRepository:
    """Lease one public watch poll and persist only its timing/result cursor (ADR-008/NFR-17)."""

    async def claim_watch_poll(
        self,
        watch: WatchedEvent,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> WatchPollLease | None:
        """Initialize then atomically claim a due/expired public source-event cursor."""
        _require_aware(now, "watch poll claim now")
        if lease_seconds <= 0:
            raise ValueError("watch poll lease_seconds must be positive")
        if lease_seconds > _MAX_LEASE_SECONDS:
            raise ValueError(f"watch poll lease_seconds must not exceed {_MAX_LEASE_SECONDS}")
        lease_token = uuid4().hex
        params = {
            "canonical_event_id": watch.canonical_event_id,
            "source": watch.source.value,
            "now": now,
            "lease_seconds": lease_seconds,
            "lease_token": lease_token,
        }
        async with system_session_scope() as session:
            row = cast(
                _WatchPollClaimRow | None,
                (
                    await session.execute(
                        text(
                            """
                            SELECT *
                            FROM public.fn_claim_watch_poll(
                                :canonical_event_id, :source, :now, :lease_seconds, :lease_token
                            )
                            """
                        ),
                        params,
                    )
                ).one_or_none(),
            )
        if row is None:
            return None
        if row.lease_token is None:
            raise RuntimeError("watch poll claim returned no lease token")
        return WatchPollLease(
            canonical_event_id=row.canonical_event_id,
            source=Source(row.source),
            attempt_count=int(row.attempt_count),
            lease_token=row.lease_token,
        )

    async def has_live_watch_poll_lease(self, lease: WatchPollLease) -> bool:
        """Read the final detector-entry lease fence without exposing public cursor rows (NFR-8)."""
        async with system_session_scope() as session:
            live = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_has_live_watch_poll_lease(
                            :canonical_event_id, :source, :lease_token
                        ) AS live
                        """
                    ),
                    {
                        "canonical_event_id": lease.canonical_event_id,
                        "source": lease.source.value,
                        "lease_token": lease.lease_token,
                    },
                )
            ).scalar_one()
        return bool(live)

    async def mark_watch_poll_succeeded(
        self,
        lease: WatchPollLease,
        *,
        completed_at: datetime,
        next_due_at: datetime,
    ) -> bool:
        """Reset failure state and advance only the held cursor to its supplied next slot."""
        _require_completion_window(completed_at, next_due_at)
        async with system_session_scope() as session:
            marked = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_mark_watch_poll_succeeded(
                            :canonical_event_id, :source, :lease_token, :completed_at,
                            :next_due_at
                        ) AS marked
                        """
                    ),
                    _lease_params(
                        lease,
                        completed_at=completed_at,
                        next_due_at=next_due_at,
                    ),
                )
            ).scalar_one()
        return bool(marked)

    async def release_watch_poll(
        self,
        lease: WatchPollLease,
        *,
        completed_at: datetime,
        next_due_at: datetime,
        error_type: str,
    ) -> bool:
        """Release a failed fixture/provider poll without retaining raw error text."""
        _require_completion_window(completed_at, next_due_at)
        normalized_error_type = _normalized_error_type(error_type)
        async with system_session_scope() as session:
            released = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_release_watch_poll(
                            :canonical_event_id, :source, :lease_token, :completed_at,
                            :next_due_at, :error_type
                        ) AS released
                        """
                    ),
                    {
                        **_lease_params(
                            lease,
                            completed_at=completed_at,
                            next_due_at=next_due_at,
                        ),
                        "error_type": normalized_error_type[:128],
                    },
                )
            ).scalar_one()
        return bool(released)

    async def list_watch_poll_states(self) -> list[WatchPollState]:
        """Return ordered public timing facts for a source-cadence staleness calculation."""
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT * FROM public.fn_list_watch_poll_states()
                        """
                    )
                )
            ).all()
        return [self._state_from_row(cast(_WatchPollStateRow, row)) for row in rows]

    @staticmethod
    def _state_from_row(row: _WatchPollStateRow) -> WatchPollState:
        """Map only the explicit, PII-free timing projection returned above."""
        return WatchPollState(
            canonical_event_id=row.canonical_event_id,
            source=Source(row.source),
            first_seen_at=row.first_seen_at,
            last_attempt_at=row.last_attempt_at,
            last_success_at=row.last_success_at,
            last_failure_at=row.last_failure_at,
            consecutive_failures=int(row.consecutive_failures),
            next_due_at=row.next_due_at,
            attempt_count=int(row.attempt_count),
            # The state-list capability intentionally redacts active lease credentials.  Health
            # assessment needs timing/failure facts, never a worker's mutable lease secret.
            lease_token=None,
            lease_expires_at=None,
            last_error_type=row.last_error_type,
        )


def _lease_params(
    lease: WatchPollLease,
    *,
    completed_at: datetime,
    next_due_at: datetime,
) -> dict[str, object]:
    """Bind only a held public lease and its next caller-approved cadence slot."""
    return {
        "canonical_event_id": lease.canonical_event_id,
        "source": lease.source.value,
        "lease_token": lease.lease_token,
        "completed_at": completed_at,
        "next_due_at": next_due_at,
    }


def _require_aware(value: datetime, label: str) -> None:
    """Reject a naive PostgreSQL bind instead of allowing a session timezone to reinterpret it."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _require_completion_window(completed_at: datetime, next_due_at: datetime) -> None:
    """Keep a result cursor monotonic and timezone-explicit at this storage boundary."""
    _require_aware(completed_at, "watch poll completed_at")
    _require_aware(next_due_at, "watch poll next_due_at")
    if next_due_at < completed_at:
        raise ValueError("watch poll next_due_at must not precede completed_at")


def _normalized_error_type(error_type: str) -> str:
    """Store only a bounded exception-class/code token, never a potentially sensitive message."""
    normalized = error_type.strip()
    if _ERROR_TYPE.fullmatch(normalized) is None:
        raise ValueError("watch poll error_type must be a class/code token")
    return normalized
