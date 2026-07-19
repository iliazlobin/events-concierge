"""Fixture-only central change-detection adapters.

These adapters perform no source polling or Temporal call.  They model the global fingerprint
ledger and at-least-once fanout boundary so the application suite remains offline while ADR-008's
deduplication and recovery semantics are exercised.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from ...domain.enums import Source
from ...ports.change_detection import (
    DetectedEventChange,
    EventChangeInsert,
    LifecycleWatch,
    OrganizerChangeDelivery,
    WatchedEvent,
    WatchPollLease,
    WatchPollState,
    fanout_retry_delay_seconds,
)

_ERROR_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,127}")


class FixtureOrganizerChangeDetector:
    """Return a stable recorded list of normalized public changes (FR-8.7a, ADR-008)."""

    def __init__(self, changes: list[DetectedEventChange]) -> None:
        self._changes = list(changes)
        self.calls = 0

    async def collect(self) -> list[DetectedEventChange]:
        """Replay fixtures on every pass so repository fingerprint dedup is load-bearing."""
        self.calls += 1
        return list(self._changes)


class FixtureWatchPollDetector:
    """Return per-watch fixture observations without embedding a real source client."""

    def __init__(
        self,
        observations: dict[tuple[UUID, Source], list[DetectedEventChange]] | None = None,
        failures: set[tuple[UUID, Source]] | None = None,
    ) -> None:
        self._observations = {key: list(changes) for key, changes in (observations or {}).items()}
        self._failures = set(failures or set())
        self.calls: list[WatchedEvent] = []

    async def poll(self, watch: WatchedEvent) -> list[DetectedEventChange]:
        """Return this public watch's normalized fixture changes or a controlled failure."""
        self.calls.append(watch)
        key = (watch.canonical_event_id, watch.source)
        if key in self._failures:
            raise RuntimeError("fixture watch poll failure")
        return list(self._observations.get(key, []))


class MockOrganizerChangeFanout:
    """Record workflow signal attempts with an optional post-effect acknowledgement-loss seam."""

    def __init__(self, *, raise_after_effect_once: bool = False) -> None:
        self.deliveries: list[OrganizerChangeDelivery] = []
        self._raise_after_effect_once = raise_after_effect_once
        self._raised = False

    async def signal_organizer_change(self, delivery: OrganizerChangeDelivery) -> None:
        """Model a Temporal signal that may have arrived before its caller loses the acknowledgement."""
        self.deliveries.append(delivery)
        if self._raise_after_effect_once and not self._raised:
            self._raised = True
            raise RuntimeError("simulated organizer-change signal acknowledgement loss")


@dataclass(slots=True)
class _DeliveryState:
    change: DetectedEventChange
    tenant_id: UUID
    workflow_id: str
    next_attempt_at: datetime
    attempt_count: int = 0
    delivered: bool = False
    lease_token: str | None = None
    lease_expires_at: datetime | None = None
    last_error: str | None = None


@dataclass(slots=True)
class _WatchPollState:
    """Mutable fixture counterpart of the derived PostgreSQL timing cursor."""

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


@dataclass(frozen=True, slots=True)
class _RecordedChange:
    """A public change plus its durable-ledger insertion time for activation-cutoff tests."""

    change: DetectedEventChange
    detected_at: datetime


class InMemoryChangeDetectionRepository:
    """One-process implementation of the global registry/change/delivery ledgers for unit tests."""

    def __init__(
        self,
        *,
        lose_live_delivery_lease_once: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """Create an offline ledger with an injectable clock and pre-signal loss seam (NFR-8)."""
        self._watches: dict[tuple[UUID, Source], dict[tuple[UUID, str], LifecycleWatch]] = {}
        self._watch_created_at: dict[tuple[UUID, Source], datetime] = {}
        self._changes: dict[str, _RecordedChange] = {}
        self._deliveries: dict[tuple[str, UUID, str], _DeliveryState] = {}
        self._lease_sequence = 0
        self._lose_live_delivery_lease_once = lose_live_delivery_lease_once
        self._now = now if now is not None else lambda: datetime.now(UTC)

    @property
    def change_count(self) -> int:
        """Expose only a test count, never tenant payloads."""
        return len(self._changes)

    @property
    def pending_delivery_count(self) -> int:
        """Expose test queue pressure without reaching into private delivery state."""
        return sum(not delivery.delivered for delivery in self._deliveries.values())

    @property
    def delivered_delivery_count(self) -> int:
        """Expose completed delivery count for at-least-once unit assertions."""
        return sum(delivery.delivered for delivery in self._deliveries.values())

    async def register(self, watch: LifecycleWatch) -> bool:
        """Add one opaque subscriber and backfill earlier canonical changes atomically.

        This mirrors the PostgreSQL registration function's catch-up behavior: a detector record
        that lands after lifecycle activation but before the async projection cannot be lost.
        """
        key = (watch.canonical_event_id, watch.source)
        subscriptions = self._watches.setdefault(key, {})
        subscription_key = (watch.tenant_id, watch.workflow_id)
        if subscription_key in subscriptions:
            return False
        if not subscriptions:
            self._watch_created_at[key] = self._now()
        subscriptions[subscription_key] = watch
        for recorded in self._changes.values():
            if (
                recorded.change.canonical_event_id != watch.canonical_event_id
                or recorded.detected_at < watch.active_since
            ):
                continue
            delivery_key = (recorded.change.fingerprint, watch.tenant_id, watch.workflow_id)
            if delivery_key in self._deliveries:
                continue
            self._deliveries[delivery_key] = _DeliveryState(
                change=recorded.change,
                tenant_id=watch.tenant_id,
                workflow_id=watch.workflow_id,
                next_attempt_at=self._now(),
            )
        return True

    async def unregister(self, watch: LifecycleWatch) -> bool:
        """Remove one subscriber, retire its last-source pending work, and prune the key."""
        key = (watch.canonical_event_id, watch.source)
        subscriptions = self._watches.get(key)
        if subscriptions is None:
            return False
        removed = subscriptions.pop((watch.tenant_id, watch.workflow_id), None)
        if not subscriptions:
            self._watches.pop(key, None)
            self._watch_created_at.pop(key, None)
        if removed is None:
            return False
        has_other_source_watch = any(
            canonical_event_id == watch.canonical_event_id
            and (watch.tenant_id, watch.workflow_id) in source_subscriptions
            for (canonical_event_id, _source), source_subscriptions in self._watches.items()
        )
        if has_other_source_watch:
            return True
        for state in self._deliveries.values():
            if (
                state.tenant_id == watch.tenant_id
                and state.workflow_id == watch.workflow_id
                and state.change.canonical_event_id == watch.canonical_event_id
                and not state.delivered
            ):
                state.delivered = True
                state.lease_token = None
                state.lease_expires_at = None
                state.last_error = "retired after lifecycle watch removal"
        return True

    async def list_watches(self) -> list[WatchedEvent]:
        """Return deterministic global watch rows with only opaque subscriber counts."""
        return [
            WatchedEvent(
                canonical_event_id=canonical_event_id,
                source=source,
                subscriber_count=len(subscriptions),
                created_at=self._watch_created_at[(canonical_event_id, source)],
            )
            for (canonical_event_id, source), subscriptions in sorted(
                self._watches.items(), key=lambda item: (str(item[0][0]), item[0][1].value)
            )
        ]

    async def record(self, change: DetectedEventChange) -> EventChangeInsert:
        """Insert one fingerprint and queue every current canonical-event subscriber once.

        ``change.source`` identifies the feeder that observed the public change; it must not
        restrict fanout because the global fingerprint intentionally has no source component.
        """
        if change.fingerprint in self._changes:
            return EventChangeInsert(inserted=False, queued_deliveries=0)
        detected_at = self._now()
        self._changes[change.fingerprint] = _RecordedChange(change=change, detected_at=detected_at)
        queued = 0
        for (canonical_event_id, _source), subscriptions in self._watches.items():
            if canonical_event_id != change.canonical_event_id:
                continue
            for watch in subscriptions.values():
                if detected_at < watch.active_since:
                    continue
                delivery_key = (change.fingerprint, watch.tenant_id, watch.workflow_id)
                if delivery_key in self._deliveries:
                    continue
                self._deliveries[delivery_key] = _DeliveryState(
                    change=change,
                    tenant_id=watch.tenant_id,
                    workflow_id=watch.workflow_id,
                    next_attempt_at=self._now(),
                )
                queued += 1
        return EventChangeInsert(inserted=True, queued_deliveries=queued)

    async def claim_deliveries(
        self, limit: int, lease_seconds: int
    ) -> list[OrganizerChangeDelivery]:
        """Lease unacknowledged in-memory records; tests release failures explicitly."""
        if limit < 1:
            raise ValueError("delivery claim limit must be positive")
        if lease_seconds < 1:
            raise ValueError("delivery lease_seconds must be positive")
        claimed: list[OrganizerChangeDelivery] = []
        now = self._now()
        for key in sorted(self._deliveries, key=lambda item: (item[0], str(item[1]), item[2])):
            if len(claimed) >= limit:
                break
            state = self._deliveries[key]
            if (
                state.delivered
                or state.next_attempt_at > now
                or (
                    state.lease_expires_at is not None
                    and state.lease_expires_at > now
                )
            ):
                continue
            self._lease_sequence += 1
            state.attempt_count += 1
            state.lease_token = f"fixture-change-lease-{self._lease_sequence}"
            state.lease_expires_at = now + timedelta(seconds=lease_seconds)
            claimed.append(
                OrganizerChangeDelivery(
                    change=state.change,
                    tenant_id=state.tenant_id,
                    workflow_id=state.workflow_id,
                    attempt_count=state.attempt_count,
                    lease_token=state.lease_token,
                )
            )
        return claimed

    async def has_live_delivery_lease(self, delivery: OrganizerChangeDelivery) -> bool:
        """Model the exact-token/current-clock fence immediately before a Temporal signal."""
        state = self._deliveries.get(
            (delivery.change.fingerprint, delivery.tenant_id, delivery.workflow_id)
        )
        now = self._now()
        if (
            state is None
            or state.delivered
            or state.lease_token != delivery.lease_token
            or state.lease_expires_at is None
            or state.lease_expires_at <= now
        ):
            return False
        if self._lose_live_delivery_lease_once:
            self._lose_live_delivery_lease_once = False
            state.lease_token = None
            state.lease_expires_at = None
            return False
        return True

    async def mark_delivered(self, delivery: OrganizerChangeDelivery) -> bool:
        """Acknowledge only an exact lease token that remains live at the fixture clock."""
        state = self._deliveries.get(
            (delivery.change.fingerprint, delivery.tenant_id, delivery.workflow_id)
        )
        now = self._now()
        if (
            state is None
            or state.delivered
            or state.lease_token != delivery.lease_token
            or state.lease_expires_at is None
            or state.lease_expires_at <= now
        ):
            return False
        state.delivered = True
        state.lease_token = None
        state.lease_expires_at = None
        state.last_error = None
        return True

    async def release_delivery(self, delivery: OrganizerChangeDelivery, *, error: str) -> bool:
        """Release only an unexpired exact lease with bounded backoff before redelivery."""
        state = self._deliveries.get(
            (delivery.change.fingerprint, delivery.tenant_id, delivery.workflow_id)
        )
        now = self._now()
        if (
            state is None
            or state.delivered
            or state.lease_token != delivery.lease_token
            or state.lease_expires_at is None
            or state.lease_expires_at <= now
        ):
            return False
        state.lease_token = None
        state.lease_expires_at = None
        state.next_attempt_at = now + timedelta(
            seconds=fanout_retry_delay_seconds(delivery.attempt_count)
        )
        state.last_error = error[:1000]
        return True


class InMemoryWatchPollRepository:
    """Fixture-only lease/cursor repository for scheduler and staleness contracts."""

    def __init__(
        self,
        *,
        lose_success_ack_once: bool = False,
        lose_live_lease_once: bool = False,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        """Optionally model a lost cursor ACK or pre-detector lease authority (NFR-8)."""
        self._states: dict[tuple[UUID, Source], _WatchPollState] = {}
        self._lease_sequence = 0
        self._lose_success_ack_once = lose_success_ack_once
        self._lose_live_lease_once = lose_live_lease_once
        self._now = now if now is not None else lambda: datetime.now(UTC)

    async def claim_watch_poll(
        self,
        watch: WatchedEvent,
        *,
        now: datetime,
        lease_seconds: int,
    ) -> WatchPollLease | None:
        """Initialize one public watch then claim it only when its cursor is due/unleased."""
        _require_aware(now, "watch poll claim now")
        if lease_seconds <= 0:
            raise ValueError("watch poll lease_seconds must be positive")
        key = (watch.canonical_event_id, watch.source)
        state = self._states.get(key)
        if state is None:
            state = _WatchPollState(
                first_seen_at=watch.created_at,
                last_attempt_at=None,
                last_success_at=None,
                last_failure_at=None,
                consecutive_failures=0,
                next_due_at=now,
                attempt_count=0,
                lease_token=None,
                lease_expires_at=None,
                last_error_type=None,
            )
            self._states[key] = state
        if state.next_due_at > now or (
            state.lease_expires_at is not None and state.lease_expires_at > now
        ):
            return None
        self._lease_sequence += 1
        state.attempt_count += 1
        state.last_attempt_at = now
        state.lease_token = f"fixture-watch-poll-{self._lease_sequence}"
        state.lease_expires_at = now + timedelta(seconds=lease_seconds)
        return WatchPollLease(
            canonical_event_id=watch.canonical_event_id,
            source=watch.source,
            attempt_count=state.attempt_count,
            lease_token=state.lease_token,
        )

    async def has_live_watch_poll_lease(self, lease: WatchPollLease) -> bool:
        """Model the exact-token/current-clock detector-entry fence (NFR-8)."""
        state = self._states.get((lease.canonical_event_id, lease.source))
        now = self._now()
        _require_aware(now, "watch poll lease check now")
        if (
            state is None
            or state.lease_token != lease.lease_token
            or state.lease_expires_at is None
            or state.lease_expires_at <= now
        ):
            return False
        if self._lose_live_lease_once:
            self._lose_live_lease_once = False
            state.lease_token = None
            state.lease_expires_at = None
            return False
        return True

    async def mark_watch_poll_succeeded(
        self,
        lease: WatchPollLease,
        *,
        completed_at: datetime,
        next_due_at: datetime,
    ) -> bool:
        """Advance only the fixture cursor still held by this exact attempt."""
        _require_completion_window(completed_at, next_due_at)
        state = self._states.get((lease.canonical_event_id, lease.source))
        if state is None or state.lease_token != lease.lease_token:
            return False
        if self._lose_success_ack_once:
            self._lose_success_ack_once = False
            return False
        state.last_success_at = completed_at
        state.consecutive_failures = 0
        state.next_due_at = next_due_at
        state.lease_token = None
        state.lease_expires_at = None
        state.last_error_type = None
        return True

    async def release_watch_poll(
        self,
        lease: WatchPollLease,
        *,
        completed_at: datetime,
        next_due_at: datetime,
        error_type: str,
    ) -> bool:
        """Record a bounded fixture failure only while the poll lease is still current."""
        _require_completion_window(completed_at, next_due_at)
        state = self._states.get((lease.canonical_event_id, lease.source))
        if state is None or state.lease_token != lease.lease_token:
            return False
        normalized_error_type = _normalized_error_type(error_type)
        state.last_failure_at = completed_at
        state.consecutive_failures += 1
        state.next_due_at = next_due_at
        state.lease_token = None
        state.lease_expires_at = None
        state.last_error_type = normalized_error_type[:128]
        return True

    async def list_watch_poll_states(self) -> list[WatchPollState]:
        """Return ordered copies so health tests cannot mutate cursor facts out of band."""
        return [
            WatchPollState(
                canonical_event_id=canonical_event_id,
                source=source,
                first_seen_at=state.first_seen_at,
                last_attempt_at=state.last_attempt_at,
                last_success_at=state.last_success_at,
                last_failure_at=state.last_failure_at,
                consecutive_failures=state.consecutive_failures,
                next_due_at=state.next_due_at,
                attempt_count=state.attempt_count,
                lease_token=state.lease_token,
                lease_expires_at=state.lease_expires_at,
                last_error_type=state.last_error_type,
            )
            for (canonical_event_id, source), state in sorted(
                self._states.items(), key=lambda item: (str(item[0][0]), item[0][1].value)
            )
        ]


def _require_aware(value: datetime, label: str) -> None:
    """Reject naive fixture times just as the PostgreSQL adapter rejects ambiguous binds."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


def _require_completion_window(completed_at: datetime, next_due_at: datetime) -> None:
    """Require an explicit, forward-only state transition for one released/acked lease."""
    _require_aware(completed_at, "watch poll completed_at")
    _require_aware(next_due_at, "watch poll next_due_at")
    if next_due_at < completed_at:
        raise ValueError("watch poll next_due_at must not precede completed_at")


def _normalized_error_type(error_type: str) -> str:
    """Mirror the durable adapter's PII-safe exception-class/code constraint."""
    normalized = error_type.strip()
    if _ERROR_TYPE.fullmatch(normalized) is None:
        raise ValueError("watch poll error_type must be a class/code token")
    return normalized
