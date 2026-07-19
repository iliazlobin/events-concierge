"""Loss-proof EventRequest intake and Temporal-start relay (FR-6.8, ADR-003).

The HTTP/API edge atomically records a deterministic request plus an opaque start instruction.  A
small relay owns the fallible engine call: a client timeout after Temporal accepted the start is
safe because the deterministic parent workflow ID rejects the replay as an already-started success.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ..domain import ids
from ..domain.request import EventRequest
from ..infra.logging import get_logger
from ..ports.repositories import RequestRepository, RequestStartRecord
from ..ports.workflows import RequestWorkflowStarter
from .parsing import HeuristicRequestParser

_log = get_logger(__name__)
_START_RETRY_DELAYS = (
    timedelta(seconds=2),
    timedelta(seconds=5),
    timedelta(seconds=15),
    timedelta(seconds=30),
    timedelta(minutes=1),
    timedelta(minutes=5),
)


@dataclass(frozen=True, slots=True)
class RequestStartRelayStats:
    """One bounded relay pass, suitable for worker metrics and deterministic tests."""

    claimed: int = 0
    started: int = 0
    retried: int = 0
    lost_leases: int = 0


class RequestIntakeService:
    """Create one deterministic EventRequest and its durable start-outbox record (AC-48)."""

    def __init__(
        self,
        requests: RequestRepository,
        parser: HeuristicRequestParser,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._requests = requests
        self._parser = parser
        self._now = now or (lambda: datetime.now(UTC))

    async def accept(self, tenant_id: UUID, raw_text: str) -> EventRequest:
        """Persist an intake retry exactly once inside its UTC-hour deduplication window.

        Parsing deliberately receives the deterministic request ID rather than minting one itself.
        The row plus start instruction are one database transaction, so an API/Temporal outage
        cannot produce an acknowledged-but-lost request (FR-6.8, NFR-8).
        """
        bucket = _time_bucket(self._now())
        request_id = ids.intake_request_id(tenant_id, raw_text, bucket)
        dedup_key = ids.request_dedup_key(tenant_id, raw_text, bucket)
        request = await self._parser.parse(tenant_id, request_id, raw_text)
        await self._requests.add_and_enqueue_start(request, dedup_key)
        return request


class RequestStartRelay:
    """Relay pending starts to Temporal with leases and perpetual, bounded-backoff retry (ADR-003)."""

    def __init__(
        self,
        requests: RequestRepository,
        starter: RequestWorkflowStarter,
        *,
        now: Callable[[], datetime] | None = None,
        lease_seconds: int = 60,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._requests = requests
        self._starter = starter
        self._now = now or (lambda: datetime.now(UTC))
        self._lease_seconds = lease_seconds

    async def relay_once(self, *, limit: int = 50) -> RequestStartRelayStats:
        """Claim and attempt a bounded batch; the worker supplies the idle poll/reconnect loop."""
        if limit < 1:
            raise ValueError("limit must be positive")
        records = await self._requests.claim_start_batch(limit, self._lease_seconds)
        stats = RequestStartRelayStats(claimed=len(records))
        for record in records:
            stats = await self._relay_record(record, stats)
        return stats

    async def relay_request(self, tenant_id: UUID, request_id: UUID) -> bool:
        """Try the just-accepted request immediately without racing the durable worker.

        A competing lease is not an error: the persisted status tells the caller whether another
        relay has already acknowledged the same deterministic Temporal workflow.
        """
        record = await self._requests.claim_start(tenant_id, request_id, self._lease_seconds)
        if record is not None:
            await self._relay_record(record, RequestStartRelayStats(claimed=1))
        return await self._requests.start_has_started(tenant_id, request_id)

    async def _relay_record(
        self, record: RequestStartRecord, stats: RequestStartRelayStats
    ) -> RequestStartRelayStats:
        try:
            if not await self._requests.has_live_start_lease(record):
                # A fresh relay may now own the start row. Do not reach Temporal, acknowledge,
                # or reschedule under the observed stale token; the durable queue recovers it.
                return _with(stats, lost_leases=stats.lost_leases + 1)
            # The start outbox deliberately carries only opaque identity.  Raw request text is
            # re-read under RLS by the activity, keeping it out of Temporal history (ADR-011).
            await self._starter.start(record.tenant_id, record.request_id)
        except Exception as exc:
            return await self._retry(record, stats, str(exc))

        acknowledged = await self._requests.mark_start_started(record)
        if not acknowledged:
            # Temporal may already have the workflow while this lease was reclaimed.  Do not issue
            # a second engine call here or report a scheduled retry; reject-duplicate convergence
            # on a fresh queue claim is the safe recovery.
            _log.warning(
                "request workflow start acknowledgement lost",
                request_id=str(record.request_id),
                tenant_id=str(record.tenant_id),
            )
            return _with(stats, lost_leases=stats.lost_leases + 1)
        return _with(stats, started=stats.started + 1)

    async def _retry(
        self, record: RequestStartRecord, stats: RequestStartRelayStats, error: str
    ) -> RequestStartRelayStats:
        delay = _retry_delay(record.attempt_count)
        if not await self._requests.reschedule_start(
            record,
            retry_at=self._now() + delay,
            error=error,
        ):
            # The Temporal/check error occurred, but a fresh worker may already own the exact
            # queue row. Its terminal guard deliberately leaves that stale row unchanged.
            return _with(stats, lost_leases=stats.lost_leases + 1)
        _log.warning(
            "request workflow start will retry",
            request_id=str(record.request_id),
            tenant_id=str(record.tenant_id),
            attempts=record.attempt_count + 1,
            retry_in_seconds=int(delay.total_seconds()),
            error=error[:1000],
        )
        return _with(stats, retried=stats.retried + 1)


def _time_bucket(moment: datetime) -> str:
    """Return the documented coarse, deterministic UTC-hour intake bucket (ADR-003)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("intake clock must return a timezone-aware datetime")
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H")


def _retry_delay(attempt_count: int) -> timedelta:
    """Cap retries at five minutes but never abandon an accepted EventRequest (ADR-003)."""
    if attempt_count < 0:
        raise ValueError("attempt_count must not be negative")
    return _START_RETRY_DELAYS[min(attempt_count, len(_START_RETRY_DELAYS) - 1)]


def _with(stats: RequestStartRelayStats, **changes: int) -> RequestStartRelayStats:
    """Return a new immutable relay statistic without shared mutable worker counters."""
    return RequestStartRelayStats(
        claimed=changes.get("claimed", stats.claimed),
        started=changes.get("started", stats.started),
        retried=changes.get("retried", stats.retried),
        lost_leases=changes.get("lost_leases", stats.lost_leases),
    )
