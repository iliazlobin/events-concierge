"""Ports for fixture-backed, central organizer-change detection.

The detector is deliberately separate from provider polling and the lifecycle workflow.  It records
one public event change globally, then a durable fanout queue delivers an opaque command to every
affected tenant workflow.  This preserves ADR-008's ``O(distinct events)`` detection shape while
keeping provider-specific feeders and Temporal clients outside the application layer.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..domain.enums import EventStatus, Source

_FANOUT_RETRY_BASE_SECONDS = 1
_FANOUT_RETRY_MAX_SECONDS = 300


def organizer_change_fingerprint(
    canonical_event_id: UUID,
    event_status: EventStatus,
    start_at: datetime | None,
    end_at: datetime | None,
) -> str:
    """Return ADR-008's stable event/status/date deduplication fingerprint.

    The source is intentionally not part of the hash: a poll and a relay observation of the same
    canonical change must converge to one ``event_changes`` row and one signal per workflow.
    """
    material = "|".join(
        (
            str(canonical_event_id),
            event_status.value,
            _normalized_timestamp(start_at),
            _normalized_timestamp(end_at),
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def fanout_retry_delay_seconds(attempt_count: int) -> int:
    """Return a bounded exponential delay after a failed leased workflow signal (ADR-008/NFR-8).

    ``attempt_count`` is incremented at lease acquisition, so the first failed attempt waits one
    second, then two, four, and so on up to five minutes.  A fresh detector observation never
    bypasses this queue delay because its fingerprint has already been persisted.
    """
    if attempt_count < 1:
        raise ValueError("fanout attempt_count must be positive")
    # Cap the exponent before evaluating the power so a corrupt/unexpectedly large attempt count
    # cannot create an enormous integer.  The cap of nine reaches the five-minute ceiling.
    exponent = min(attempt_count - 1, 9)
    delay = _FANOUT_RETRY_BASE_SECONDS << exponent
    return _FANOUT_RETRY_MAX_SECONDS if delay > _FANOUT_RETRY_MAX_SECONDS else delay


def _normalized_timestamp(value: datetime | None) -> str:
    """Serialize a timestamp into one timezone-independent fingerprint component."""
    if value is None:
        return ""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("organizer-change timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class DetectedEventChange:
    """One normalized public organizer cancellation/reschedule (FR-8.7/8.7a, ADR-008).

    There is no tenant identifier or user-derived field here.  The source-specific detector uses
    public data only and supplies the canonical event identity plus the deterministic fingerprint.
    """

    fingerprint: str
    canonical_event_id: UUID
    source: Source
    event_status: EventStatus
    start_at: datetime | None = None
    end_at: datetime | None = None
    time_zone: str | None = None
    title: str | None = None
    venue_name: str | None = None

    def __post_init__(self) -> None:
        if not self.fingerprint.strip():
            raise ValueError("organizer-change fingerprint must not be empty")
        if self.event_status not in (EventStatus.CANCELLED, EventStatus.RESCHEDULED):
            raise ValueError("organizer change must be cancelled or rescheduled")
        _normalized_timestamp(self.start_at)
        _normalized_timestamp(self.end_at)
        if self.event_status is EventStatus.RESCHEDULED and self.start_at is None:
            raise ValueError("a reschedule requires a new start_at")
        if self.start_at is not None and self.end_at is not None and self.end_at <= self.start_at:
            raise ValueError("organizer-change end_at must be after start_at")
        if self.time_zone is not None:
            try:
                ZoneInfo(self.time_zone)
            except ZoneInfoNotFoundError as error:
                raise ValueError("organizer-change time_zone must be an IANA zone") from error


@dataclass(frozen=True, slots=True)
class LifecycleWatch:
    """An opaque active-lifecycle subscription to one public canonical event/source pair.

    ``watch_registry`` stores only the distinct event/source key so feeders know which source
    surfaces need observation. Tenant and workflow identifiers live in the opaque delivery-control
    plane; a canonical change fans out across *all* source subscriptions for that event because its
    fingerprint deliberately excludes feeder provenance. ``active_since`` is the immutable
    registered-transition timestamp carried by the projection outbox; registration backfills only
    changes detected at or after it, never historical changes from before this lifecycle activated
    (ADR-008).
    """

    tenant_id: UUID
    workflow_id: str
    canonical_event_id: UUID
    source: Source
    active_since: datetime

    def __post_init__(self) -> None:
        if not self.workflow_id.strip():
            raise ValueError("watch workflow_id must not be empty")
        _normalized_timestamp(self.active_since)


@dataclass(frozen=True, slots=True)
class WatchedEvent:
    """A global, no-tenant-PII watch-registry row and its opaque subscriber count."""

    canonical_event_id: UUID
    source: Source
    subscriber_count: int
    created_at: datetime

    def __post_init__(self) -> None:
        """Keep the distinct-watch registration baseline usable for outage staleness checks."""
        if self.subscriber_count < 0:
            raise ValueError("watch subscriber_count must be nonnegative")
        _require_timestamp(self.created_at, "watch created_at")


@dataclass(frozen=True, slots=True)
class WatchPollCadence:
    """Caller-supplied cadence and alarm policy for one source's public watch poll.

    No production default lives here: ADR-008's source-specific 3h/6h parameters remain pending
    owner ratification. A scheduler must receive an explicit plan before it can claim source work.
    """

    poll_interval: timedelta
    stale_after: timedelta
    failure_alarm_threshold: int

    def __post_init__(self) -> None:
        """Reject a cadence that cannot make its own staleness semantics meaningful."""
        if self.poll_interval.total_seconds() <= 0:
            raise ValueError("watch poll interval must be positive")
        if self.stale_after.total_seconds() <= 0:
            raise ValueError("watch poll stale_after must be positive")
        if self.stale_after < self.poll_interval:
            raise ValueError("watch poll stale_after must not be shorter than its interval")
        if self.failure_alarm_threshold <= 0:
            raise ValueError("watch poll failure_alarm_threshold must be positive")


@dataclass(frozen=True, slots=True)
class WatchPollPlan:
    """Immutable, explicitly supplied source cadence map for fixture-only scheduling."""

    cadence_by_source: Mapping[Source, WatchPollCadence]

    def __post_init__(self) -> None:
        """Copy only typed source/cadence values so caller mutation cannot change a running pass."""
        normalized: dict[Source, WatchPollCadence] = {}
        for source, cadence in self.cadence_by_source.items():
            if not isinstance(source, Source):
                raise TypeError("watch poll cadence keys must be Source values")
            if not isinstance(cadence, WatchPollCadence):
                raise TypeError("watch poll cadence values must be WatchPollCadence values")
            normalized[source] = cadence
        object.__setattr__(self, "cadence_by_source", MappingProxyType(normalized))

    def cadence_for(self, source: Source) -> WatchPollCadence | None:
        """Return no cadence rather than silently inventing a provider policy."""
        return self.cadence_by_source.get(source)


@dataclass(frozen=True, slots=True)
class WatchPollLease:
    """One leased public source/event poll attempt, safe to retry after worker loss."""

    canonical_event_id: UUID
    source: Source
    attempt_count: int
    lease_token: str

    def __post_init__(self) -> None:
        if self.attempt_count <= 0:
            raise ValueError("watch poll lease attempt_count must be positive")
        if not self.lease_token.strip():
            raise ValueError("watch poll lease_token must not be empty")


@dataclass(frozen=True, slots=True)
class WatchPollState:
    """Persisted public watch-poll timing facts used to expose stale coverage (ADR-008)."""

    canonical_event_id: UUID
    source: Source
    first_seen_at: datetime
    last_attempt_at: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    consecutive_failures: int
    next_due_at: datetime
    attempt_count: int
    lease_token: str | None
    lease_expires_at: datetime | None
    last_error_type: str | None

    def __post_init__(self) -> None:
        _require_timestamp(self.first_seen_at, "watch poll first_seen_at")
        _require_timestamp(self.next_due_at, "watch poll next_due_at")
        for label, value in (
            ("watch poll last_attempt_at", self.last_attempt_at),
            ("watch poll last_success_at", self.last_success_at),
            ("watch poll last_failure_at", self.last_failure_at),
            ("watch poll lease_expires_at", self.lease_expires_at),
        ):
            if value is not None:
                _require_timestamp(value, label)
        if self.consecutive_failures < 0 or self.attempt_count < 0:
            raise ValueError("watch poll counts must be nonnegative")
        if (self.lease_token is None) is not (self.lease_expires_at is None):
            raise ValueError("watch poll lease token and expiry must be set together")
        if self.lease_token is not None and not self.lease_token.strip():
            raise ValueError("watch poll lease_token must not be empty")
        if self.last_error_type is not None and not self.last_error_type.strip():
            raise ValueError("watch poll last_error_type must not be empty")


class WatchPollHealthStatus(StrEnum):
    """PII-free coverage/staleness projection for one distinct canonical watch."""

    HEALTHY = "healthy"
    PENDING_INITIAL_SUCCESS = "pending_initial_success"
    STALE = "stale"
    FAILURE_ALARM = "failure_alarm"
    UNTRACKED = "untracked"
    UNPLANNED = "unplanned"


@dataclass(frozen=True, slots=True)
class WatchPollHealth:
    """One source/event's declared cadence health with no tenant identity or event payload."""

    canonical_event_id: UUID
    source: Source
    subscriber_count: int
    status: WatchPollHealthStatus
    last_success_at: datetime | None
    consecutive_failures: int | None


@dataclass(frozen=True, slots=True)
class WatchPollHealthReport:
    """Aggregate public watch coverage usable by an operational alarm (NFR-17)."""

    watches: tuple[WatchPollHealth, ...]

    @property
    def coverage_loss_count(self) -> int:
        """Count sources without a plan/cursor or with stale/failure-alarmed coverage."""
        alerting = {
            WatchPollHealthStatus.STALE,
            WatchPollHealthStatus.FAILURE_ALARM,
            WatchPollHealthStatus.UNTRACKED,
            WatchPollHealthStatus.UNPLANNED,
        }
        return sum(watch.status in alerting for watch in self.watches)

    @property
    def requires_attention(self) -> bool:
        """Expose one alarm bit without turning a transient initial poll into a false success."""
        return self.coverage_loss_count > 0


def assess_watch_poll_health(
    watches: list[WatchedEvent],
    states: list[WatchPollState],
    plan: WatchPollPlan,
    *,
    now: datetime,
) -> WatchPollHealthReport:
    """Compute stale/unplanned coverage from supplied policy values without provider I/O.

    A newly registered watch has one full configured ``stale_after`` grace period to acquire its
    first cursor and observation. Once that registration age exceeds the grace period, a missing
    cursor is an ``UNTRACKED`` coverage-loss signal rather than a silent fresh start.
    """
    _require_timestamp(now, "watch poll health now")
    states_by_key = {(state.canonical_event_id, state.source): state for state in states}
    health: list[WatchPollHealth] = []
    for watch in sorted(
        watches, key=lambda item: (str(item.canonical_event_id), item.source.value)
    ):
        cadence = plan.cadence_for(watch.source)
        state = states_by_key.get((watch.canonical_event_id, watch.source))
        if cadence is None:
            status = WatchPollHealthStatus.UNPLANNED
        elif state is None:
            status = (
                WatchPollHealthStatus.UNTRACKED
                if now >= watch.created_at + cadence.stale_after
                else WatchPollHealthStatus.PENDING_INITIAL_SUCCESS
            )
        elif state.last_success_at is None:
            status = (
                WatchPollHealthStatus.FAILURE_ALARM
                if state.consecutive_failures >= cadence.failure_alarm_threshold
                else (
                    WatchPollHealthStatus.STALE
                    if now >= state.first_seen_at + cadence.stale_after
                    else WatchPollHealthStatus.PENDING_INITIAL_SUCCESS
                )
            )
        elif state.consecutive_failures >= cadence.failure_alarm_threshold:
            status = WatchPollHealthStatus.FAILURE_ALARM
        elif now >= state.last_success_at + cadence.stale_after:
            status = WatchPollHealthStatus.STALE
        else:
            status = WatchPollHealthStatus.HEALTHY
        health.append(
            WatchPollHealth(
                canonical_event_id=watch.canonical_event_id,
                source=watch.source,
                subscriber_count=watch.subscriber_count,
                status=status,
                last_success_at=state.last_success_at if state is not None else None,
                consecutive_failures=state.consecutive_failures if state is not None else None,
            )
        )
    return WatchPollHealthReport(tuple(health))


@dataclass(frozen=True, slots=True)
class EventChangeInsert:
    """Result of recording a unique detector observation and its fanout work."""

    inserted: bool
    queued_deliveries: int


@dataclass(frozen=True, slots=True)
class OrganizerChangeDelivery:
    """One leased at-least-once fanout command (FR-8.7/8.9, ADR-008)."""

    change: DetectedEventChange
    tenant_id: UUID
    workflow_id: str
    attempt_count: int
    lease_token: str


class OrganizerChangeDetectorPort(Protocol):
    """Read normalized fixture observations only; live feeder implementations are intentionally deferred."""

    async def collect(self) -> list[DetectedEventChange]:
        """Return public normalized changes without calling a lifecycle or notification system."""
        ...


class WatchPollDetectorPort(Protocol):
    """Fixture/provider boundary for one distinct public event/source poll."""

    async def poll(self, watch: WatchedEvent) -> list[DetectedEventChange]:
        """Return normalized changes for exactly this watched source/event, with no tenant data."""
        ...


class WatchRegistryPort(Protocol):
    """Maintain opaque active lifecycle subscriptions and their global distinct watch keys (ADR-008)."""

    async def register(self, watch: LifecycleWatch) -> bool:
        """Add an active workflow interest and backfill post-activation canonical changes.

        Return ``False`` for an existing subscription or a stale/invalid projection whose locked
        lifecycle is no longer active or no longer matches the requested source.
        """
        ...

    async def unregister(self, watch: LifecycleWatch) -> bool:
        """Remove a terminal workflow interest and retire its final-source pending delivery.

        Return ``False`` for an active lifecycle, an invalid/stale projection, or an already absent
        subscription.  This prevents arbitrary app-role calls from disabling an active watch.
        """
        ...

    async def list_watches(self) -> list[WatchedEvent]:
        """Return distinct global watch keys without exposing tenant identifiers or user data."""
        ...


class WatchPollStateRepository(Protocol):
    """Lease and record timing for public distinct-watch polls (ADR-008/NFR-17)."""

    async def claim_watch_poll(
        self,
        watch: WatchedEvent,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> WatchPollLease | None:
        """Create/claim one due cursor, or return nothing when another worker owns it."""
        ...

    async def has_live_watch_poll_lease(self, lease: WatchPollLease) -> bool:
        """Confirm the exact poll token remains live at the database clock before detector egress (NFR-8)."""
        ...

    async def mark_watch_poll_succeeded(
        self,
        lease: WatchPollLease,
        *,
        completed_at: datetime,
        next_due_at: datetime,
    ) -> bool:
        """Advance a cursor only while its exact poll lease remains held."""
        ...

    async def release_watch_poll(
        self,
        lease: WatchPollLease,
        *,
        completed_at: datetime,
        next_due_at: datetime,
        error_type: str,
    ) -> bool:
        """Record a PII-free failure and make the source eligible at its next planned slot."""
        ...

    async def list_watch_poll_states(self) -> list[WatchPollState]:
        """Return public timing facts for global coverage/staleness assessment."""
        ...


class EventChangeRepository(Protocol):
    """Durably deduplicate public changes and lease their opaque tenant-workflow fanout deliveries."""

    async def record(self, change: DetectedEventChange) -> EventChangeInsert:
        """Insert the fingerprint once and enqueue every active subscription for its canonical event.

        ``change.source`` is feeder provenance, not a fanout filter: identical Luma and Meetup
        observations share the same fingerprint and must reach every affected workflow (ADR-008).
        """
        ...

    async def claim_deliveries(
        self, limit: int, lease_seconds: int
    ) -> list[OrganizerChangeDelivery]:
        """Lease ready deliveries globally; expired leases are safe to redeliver (ADR-008)."""
        ...

    async def has_live_delivery_lease(self, delivery: OrganizerChangeDelivery) -> bool:
        """Confirm the exact fanout lease remains live at the database clock before signaling (NFR-8)."""
        ...

    async def mark_delivered(self, delivery: OrganizerChangeDelivery) -> bool:
        """Acknowledge one signal only while its fanout lease is still held."""
        ...

    async def release_delivery(self, delivery: OrganizerChangeDelivery, *, error: str) -> bool:
        """Release a failed signal with bounded exponential backoff for at-least-once recovery."""
        ...


class OrganizerChangeFanoutPort(Protocol):
    """Signal one registration workflow; a Temporal adapter implements this in the outer layer."""

    async def signal_organizer_change(self, delivery: OrganizerChangeDelivery) -> None:
        """Deliver an idempotent organizer-change command using its fingerprint as the dedup key."""
        ...


def _require_timestamp(value: datetime, label: str) -> None:
    """Reject a naive poll state because staleness calculations span worker processes."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
