"""Offline ADR-003 start-outbox tests: deterministic intake and lost-ack replay safety."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from events_concierge.adapters.ranking.embedding import DeterministicEmbedding
from events_concierge.application.parsing import HeuristicRequestParser
from events_concierge.application.request_start import (
    RequestIntakeService,
    RequestStartWorker,
    RequestStartWorkerStats,
)
from events_concierge.domain.request import EventRequest
from events_concierge.ports.repositories import RequestStartRecord


class FakeRequestRepository:
    """In-memory repository seam preserving start leases and duplicate identities for unit tests."""

    def __init__(self, records: list[RequestStartRecord] | None = None) -> None:
        self.requests: dict[tuple[UUID, UUID], EventRequest] = {}
        self.dedup: dict[str, tuple[UUID, UUID]] = {}
        self._ready = list(records or [])
        self.enqueued = 0
        self.started: set[tuple[UUID, UUID]] = set()
        self.rescheduled: list[tuple[RequestStartRecord, datetime, str]] = []
        self.live_lease_checks: list[RequestStartRecord] = []
        self._lease_sequence = 0
        self.workflow_targets: dict[UUID, set[str]] = {}
        self.erasure_fenced = False

    async def add(self, request: EventRequest) -> None:
        self.requests.setdefault((request.tenant_id, request.request_id), request)

    async def get(self, tenant_id: UUID, request_id: UUID) -> EventRequest | None:
        return self.requests.get((tenant_id, request_id))

    async def register_workflow_targets(
        self,
        tenant_id: UUID,
        workflow_ids: tuple[str, ...],
    ) -> None:
        self.workflow_targets.setdefault(tenant_id, set()).update(workflow_ids)

    async def add_and_enqueue_start(self, request: EventRequest, dedup_key: str) -> None:
        existing = self.dedup.get(dedup_key)
        identity = (request.tenant_id, request.request_id)
        if existing is None:
            self.dedup[dedup_key] = identity
            self.requests[identity] = request
            self.enqueued += 1
            return
        if existing != identity:
            raise RuntimeError("dedup key bound to a different request")

    async def claim_start(
        self, tenant_id: UUID, request_id: UUID, lease_seconds: int
    ) -> RequestStartRecord | None:
        del lease_seconds
        for index, record in enumerate(self._ready):
            if (record.tenant_id, record.request_id) == (tenant_id, request_id):
                return self._ready.pop(index)
        return None

    async def claim_start_batch(self, limit: int, lease_seconds: int) -> list[RequestStartRecord]:
        del lease_seconds
        records = self._ready[:limit]
        del self._ready[:limit]
        return records

    async def has_live_start_lease(self, record: RequestStartRecord) -> bool:
        """Keep ordinary start fixtures authorized through their final Temporal-entry fence."""
        self.live_lease_checks.append(record)
        return True

    @asynccontextmanager
    async def request_start_guard(self, record: RequestStartRecord) -> AsyncIterator[bool]:
        del record
        yield not self.erasure_fenced

    async def account_erasure_fenced(self, tenant_id: UUID) -> bool:
        del tenant_id
        return self.erasure_fenced

    async def mark_start_started(self, record: RequestStartRecord) -> bool:
        self.started.add((record.tenant_id, record.request_id))
        return True

    async def reschedule_start(
        self, record: RequestStartRecord, *, retry_at: datetime, error: str
    ) -> bool:
        self.rescheduled.append((record, retry_at, error))
        self._lease_sequence += 1
        self._ready.append(
            RequestStartRecord(
                request_id=record.request_id,
                tenant_id=record.tenant_id,
                attempt_count=record.attempt_count + 1,
                lease_token=f"retry-{self._lease_sequence}",
            )
        )
        return True

    async def start_has_started(self, tenant_id: UUID, request_id: UUID) -> bool:
        return (tenant_id, request_id) in self.started

    async def mark_failed_no_candidate(
        self, tenant_id: UUID, request_id: UUID, transition_id: str
    ) -> bool:
        del tenant_id, request_id, transition_id
        return True


class LostAcknowledgementStarter:
    """Temporal seam: the first start creates the workflow but loses its client acknowledgement."""

    def __init__(self) -> None:
        self.calls = 0
        self.effects: set[tuple[UUID, UUID]] = set()

    async def start(self, tenant_id: UUID, request_id: UUID) -> None:
        self.calls += 1
        identity = (tenant_id, request_id)
        if identity in self.effects:
            # Mirrors Temporal's reject-duplicate response being normalized to success by the port.
            return
        self.effects.add(identity)
        raise RuntimeError("simulated Temporal start acknowledgement loss")

    async def cancel(self, tenant_id: UUID, request_id: UUID) -> None:
        self.effects.discard((tenant_id, request_id))


class _StaleStartAckLeaseRepository(FakeRequestRepository):
    """Two fresh queue leases where the first post-start acknowledgement loses authority."""

    def __init__(self, record: RequestStartRecord) -> None:
        super().__init__()
        self._first = record
        self._fresh = RequestStartRecord(
            request_id=record.request_id,
            tenant_id=record.tenant_id,
            attempt_count=record.attempt_count,
            lease_token="fresh-start-lease",
        )
        self._claim_count = 0
        self._first_ack_lost = False
        self.acknowledged: list[str] = []

    @property
    def fresh(self) -> RequestStartRecord:
        """Expose the replacement lease for exact reclaim assertions only."""
        return self._fresh

    async def claim_start_batch(self, limit: int, lease_seconds: int) -> list[RequestStartRecord]:
        assert limit > 0
        assert lease_seconds > 0
        if self._claim_count == 0:
            self._claim_count += 1
            return [self._first]
        if self._claim_count == 1:
            assert self._first_ack_lost
            self._claim_count += 1
            return [self._fresh]
        return []

    async def mark_start_started(self, record: RequestStartRecord) -> bool:
        self.acknowledged.append(record.lease_token)
        if record == self._first:
            self._first_ack_lost = True
            return False
        assert record == self._fresh
        self.started.add((record.tenant_id, record.request_id))
        return True


class _StalePreStartLeaseRepository(FakeRequestRepository):
    """Two queue leases where the first loses authority before Temporal can be invoked."""

    def __init__(self, record: RequestStartRecord) -> None:
        super().__init__()
        self._first = record
        self._fresh = RequestStartRecord(
            request_id=record.request_id,
            tenant_id=record.tenant_id,
            attempt_count=record.attempt_count,
            lease_token="fresh-pre-start-lease",
        )
        self._claim_count = 0
        self._first_lease_lost = False
        self.acknowledged: list[str] = []

    @property
    def fresh(self) -> RequestStartRecord:
        """Expose the replacement lease for exact recovery assertions only."""
        return self._fresh

    async def claim_start_batch(self, limit: int, lease_seconds: int) -> list[RequestStartRecord]:
        assert limit > 0
        assert lease_seconds > 0
        if self._claim_count == 0:
            self._claim_count += 1
            return [self._first]
        if self._claim_count == 1:
            assert self._first_lease_lost
            self._claim_count += 1
            return [self._fresh]
        return []

    async def has_live_start_lease(self, record: RequestStartRecord) -> bool:
        self.live_lease_checks.append(record)
        if record == self._first:
            self._first_lease_lost = True
            return False
        assert record == self._fresh
        return True

    async def mark_start_started(self, record: RequestStartRecord) -> bool:
        self.acknowledged.append(record.lease_token)
        assert record == self._fresh
        self.started.add((record.tenant_id, record.request_id))
        return True


class _StaleRetryLeaseRepository(FakeRequestRepository):
    """Model a Temporal/check failure whose terminal retry loses its exact queue lease."""

    async def reschedule_start(
        self, record: RequestStartRecord, *, retry_at: datetime, error: str
    ) -> bool:
        del record, retry_at, error
        return False


class _DuplicateAcceptingStarter:
    """Model Temporal's deterministic reject-duplicate convergence after a visible parent start."""

    def __init__(self) -> None:
        self.calls = 0
        self.effects: set[tuple[UUID, UUID]] = set()

    async def start(self, tenant_id: UUID, request_id: UUID) -> None:
        self.calls += 1
        self.effects.add((tenant_id, request_id))

    async def cancel(self, tenant_id: UUID, request_id: UUID) -> None:
        self.effects.discard((tenant_id, request_id))


class _FenceAfterStartStarter(_DuplicateAcceptingStarter):
    def __init__(self, repository: FakeRequestRepository) -> None:
        super().__init__()
        self._repository = repository
        self.cancelled: list[tuple[UUID, UUID]] = []

    async def start(self, tenant_id: UUID, request_id: UUID) -> None:
        await super().start(tenant_id, request_id)
        self._repository.erasure_fenced = True

    async def cancel(self, tenant_id: UUID, request_id: UUID) -> None:
        self.cancelled.append((tenant_id, request_id))
        await super().cancel(tenant_id, request_id)


async def test_intake_replays_normalized_text_to_one_request_and_start_instruction() -> None:
    repository = FakeRequestRepository()
    service = RequestIntakeService(
        repository,
        HeuristicRequestParser(DeterministicEmbedding()),
        now=lambda: datetime(2026, 7, 17, 19, 32, tzinfo=UTC),
    )
    tenant_id = uuid4()

    first = await service.accept(tenant_id, "  Jazz   Night ")
    replay = await service.accept(tenant_id, "jazz night")

    assert first.request_id == replay.request_id
    assert repository.enqueued == 1
    assert len(repository.requests) == 1
    assert len(repository.dedup) == 1


async def test_start_worker_replays_lost_ack_without_second_parent_workflow_effect() -> None:
    now = datetime(2026, 7, 17, 19, tzinfo=UTC)
    tenant_id, request_id = uuid4(), uuid4()
    record = RequestStartRecord(
        request_id=request_id,
        tenant_id=tenant_id,
        attempt_count=0,
        lease_token="first-lease",
    )
    repository = FakeRequestRepository([record])
    starter = LostAcknowledgementStarter()
    delivery = RequestStartWorker(repository, starter, now=lambda: now)

    first = await delivery.run_once()
    second = await delivery.run_once()

    assert first.claimed == 1 and first.retried == 1 and first.started == 0
    assert second.claimed == 1 and second.started == 1 and second.retried == 0
    assert starter.calls == 2
    assert starter.effects == {(tenant_id, request_id)}
    assert repository.started == {(tenant_id, request_id)}
    assert repository.rescheduled == [
        (record, now + timedelta(seconds=2), "simulated Temporal start acknowledgement loss")
    ]


async def test_start_worker_recovers_stale_post_start_ack_lease_without_second_parent_effect() -> (
    None
):
    """A fresh lease replays Temporal's duplicate start after a stale ACK is rejected (ADR-003)."""
    tenant_id, request_id = uuid4(), uuid4()
    record = RequestStartRecord(
        request_id=request_id,
        tenant_id=tenant_id,
        attempt_count=1,
        lease_token="first-start-lease",
    )
    repository = _StaleStartAckLeaseRepository(record)
    starter = _DuplicateAcceptingStarter()
    delivery = RequestStartWorker(repository, starter)

    first = await delivery.run_once()
    recovered = await delivery.run_once()

    assert (first.claimed, first.started, first.retried, first.lost_leases) == (1, 0, 0, 1)
    assert (recovered.claimed, recovered.started, recovered.retried, recovered.lost_leases) == (
        1,
        1,
        0,
        0,
    )
    assert starter.calls == 2
    assert starter.effects == {(tenant_id, request_id)}
    assert repository.started == {(tenant_id, request_id)}
    assert repository.rescheduled == []
    assert repository.fresh.attempt_count == record.attempt_count
    assert repository.fresh.lease_token != record.lease_token
    assert repository.acknowledged == [record.lease_token, repository.fresh.lease_token]


async def test_start_worker_skips_a_stale_pre_temporal_lease_until_a_fresh_claim() -> None:
    """A stale final start lease emits no Temporal effect before its fresh recovery (NFR-8)."""
    tenant_id, request_id = uuid4(), uuid4()
    record = RequestStartRecord(
        request_id=request_id,
        tenant_id=tenant_id,
        attempt_count=1,
        lease_token="first-pre-start-lease",
    )
    repository = _StalePreStartLeaseRepository(record)
    starter = _DuplicateAcceptingStarter()
    delivery = RequestStartWorker(repository, starter)

    first = await delivery.run_once()

    assert first == RequestStartWorkerStats(claimed=1, lost_leases=1)
    assert starter.calls == 0
    assert starter.effects == set()
    assert repository.acknowledged == []
    assert repository.rescheduled == []
    assert repository.started == set()

    recovered = await delivery.run_once()

    assert recovered == RequestStartWorkerStats(claimed=1, started=1)
    assert starter.calls == 1
    assert starter.effects == {(tenant_id, request_id)}
    assert repository.acknowledged == [repository.fresh.lease_token]
    assert repository.rescheduled == []
    assert repository.started == {(tenant_id, request_id)}
    assert repository.fresh.attempt_count == record.attempt_count
    assert repository.fresh.lease_token != record.lease_token


async def test_start_worker_does_not_report_a_lost_retry_lease_as_scheduled() -> None:
    """A failed exact retry write becomes lost authority, not a nonexistent backoff retry."""
    tenant_id, request_id = uuid4(), uuid4()
    record = RequestStartRecord(
        request_id=request_id,
        tenant_id=tenant_id,
        attempt_count=0,
        lease_token="stale-retry-lease",
    )
    repository = _StaleRetryLeaseRepository([record])
    starter = LostAcknowledgementStarter()
    delivery = RequestStartWorker(repository, starter)

    result = await delivery.run_once()

    assert result == RequestStartWorkerStats(claimed=1, lost_leases=1)
    assert starter.calls == 1
    assert starter.effects == {(tenant_id, request_id)}
    assert repository.rescheduled == []


async def test_start_worker_cancels_parent_when_erasure_commits_before_postcheck() -> None:
    tenant_id, request_id = uuid4(), uuid4()
    record = RequestStartRecord(request_id, tenant_id, 0, "erasure-race-lease")
    repository = FakeRequestRepository([record])
    starter = _FenceAfterStartStarter(repository)

    result = await RequestStartWorker(repository, starter).run_once()

    assert result == RequestStartWorkerStats(claimed=1, lost_leases=1)
    assert starter.cancelled == [(tenant_id, request_id)]
    assert starter.effects == set()
    assert repository.started == set()
