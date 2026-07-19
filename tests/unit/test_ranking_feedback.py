"""Unit contracts for bounded, replay-safe implicit ranking feedback (FR-2.1/FR-4.3)."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from events_concierge.adapters.mock.ranking_feedback import InMemoryRankingFeedback
from events_concierge.adapters.ranking.embedding import DeterministicEmbedding
from events_concierge.adapters.ranking.ranker import (
    DeterministicFeatureRescorer,
    InMemoryRankingProfiles,
    PersonalizedRanker,
)
from events_concierge.application.ranking_feedback import (
    FeedbackAwareRankingProfiles,
    RankingFeedbackService,
    UnknownFeedbackEventError,
)
from events_concierge.domain.events import CandidateEvent, CanonicalEvent
from events_concierge.domain.ranking_feedback import (
    FeedbackSignalKind,
    RankingFeedbackReceipt,
    RankingFeedbackSignal,
    feedback_feature_deltas,
    implicit_affinity_bound,
)
from events_concierge.domain.request import EventRequest, RequestConstraints
from events_concierge.ports.ranking_feedback import (
    RankingFeedbackConflictError,
    RankingFeedbackRecordStatus,
)


class EqualCrossEncoder:
    """Fixture cross-encoder that makes feedback the only changing ranking feature."""

    async def score(self, query: str, documents: list[str]) -> list[float]:
        del query
        return [0.5] * len(documents)


class FixtureCatalog:
    """Small catalog double; feedback service uses only its canonical lookup seam."""

    def __init__(self, events: list[CanonicalEvent]) -> None:
        self._events = {event.canonical_event_id: event for event in events}

    async def upsert_candidates(self, candidates: list[CandidateEvent]) -> list[CanonicalEvent]:
        del candidates
        raise AssertionError("feedback recording must not mutate the catalog")

    async def retrieve(
        self,
        constraints: RequestConstraints,
        intent_embedding: list[float] | None,
        limit: int,
    ) -> list[CanonicalEvent]:
        del constraints, intent_embedding, limit
        raise AssertionError("feedback recording must not retrieve a feed")

    async def get(self, canonical_event_id: UUID) -> CanonicalEvent | None:
        return self._events.get(canonical_event_id)

    def replace(self, event: CanonicalEvent) -> None:
        """Simulate a later catalog enrichment without changing the canonical identity."""
        self._events[event.canonical_event_id] = event


def _event(
    title: str,
    description: str,
    *,
    canonical_event_id: UUID | None = None,
) -> CanonicalEvent:
    return CanonicalEvent(
        canonical_event_id=canonical_event_id or uuid4(),
        title=title,
        start_at=datetime(2026, 8, 1, 18, 0, tzinfo=UTC),
        description=description,
    )


def _request(tenant_id: UUID) -> EventRequest:
    return EventRequest(
        request_id=uuid4(),
        tenant_id=tenant_id,
        raw_text="anything",
        constraints=RequestConstraints(),
        intent_embedding=[0.0] * 16,
    )


def test_feature_extraction_is_server_owned_bounded_and_signed_by_kind() -> None:
    """Verbose public metadata cannot turn one signal into unbounded or client-selected weight."""
    event = _event(
        "Jazz Jazz After Dark",
        " ".join(f"term{index}" for index in range(40)),
    )

    scroll = feedback_feature_deltas(event, FeedbackSignalKind.SCROLL)
    dwell = feedback_feature_deltas(event, FeedbackSignalKind.DWELL)
    click = feedback_feature_deltas(event, FeedbackSignalKind.CLICK)
    dismiss = feedback_feature_deltas(event, FeedbackSignalKind.DISMISS)

    assert len(click) == 24
    assert "jazz" in click
    assert all(label.isascii() and label == label.lower() for label in click)
    assert math.isclose(sum(scroll.values()), 0.25)
    assert math.isclose(sum(dwell.values()), 0.75)
    assert math.isclose(sum(click.values()), 1.0)
    assert math.isclose(sum(dismiss.values()), -1.0)
    assert set(click) == set(dismiss)


async def test_feedback_replays_exact_signal_once_and_rejects_changed_payload() -> None:
    """At-least-once delivery is harmless, while an idempotency-key collision is explicit."""
    tenant_id = uuid4()
    event = _event("Jazz concert", "live music")
    signal = RankingFeedbackSignal(uuid4(), event.canonical_event_id, FeedbackSignalKind.CLICK)
    store = InMemoryRankingFeedback()
    service = RankingFeedbackService(FixtureCatalog([event]), store)

    first = await service.record(tenant_id, signal)
    replay = await service.record(tenant_id, signal)
    affinities = await store.get_implicit_affinities(tenant_id)

    assert first is RankingFeedbackRecordStatus.RECORDED
    assert replay is RankingFeedbackRecordStatus.REPLAYED
    assert math.isclose(sum(affinities.values()), 1.0)

    conflict = RankingFeedbackReceipt(
        signal=RankingFeedbackSignal(
            signal.signal_id,
            event.canonical_event_id,
            FeedbackSignalKind.DISMISS,
        ),
        feature_deltas=feedback_feature_deltas(event, FeedbackSignalKind.DISMISS),
    )
    with pytest.raises(RankingFeedbackConflictError, match="different payload"):
        await store.record_feedback(tenant_id, conflict)


async def test_catalog_enrichment_does_not_turn_a_signal_retry_into_a_conflict() -> None:
    """The first derived snapshot remains the preference effect after mutable event enrichment."""
    tenant_id = uuid4()
    original = _event("Jazz concert", "saxophone improvisation")
    catalog = FixtureCatalog([original])
    store = InMemoryRankingFeedback()
    service = RankingFeedbackService(catalog, store)
    signal = RankingFeedbackSignal(uuid4(), original.canonical_event_id, FeedbackSignalKind.CLICK)

    first = await service.record(tenant_id, signal)
    first_affinities = await store.get_implicit_affinities(tenant_id)
    catalog.replace(
        _event(
            "Jazz concert",
            "enriched folktronica afterparty details",
            canonical_event_id=original.canonical_event_id,
        )
    )
    replay = await service.record(tenant_id, signal)

    assert first is RankingFeedbackRecordStatus.RECORDED
    assert replay is RankingFeedbackRecordStatus.REPLAYED
    assert await store.get_implicit_affinities(tenant_id) == first_affinities
    assert "folktronica" not in first_affinities


async def test_unknown_canonical_event_creates_no_feedback_receipt() -> None:
    """A body-supplied UUID cannot create unvalidated taste labels or a dangling receipt."""
    tenant_id = uuid4()
    store = InMemoryRankingFeedback()
    service = RankingFeedbackService(FixtureCatalog([]), store)

    with pytest.raises(UnknownFeedbackEventError):
        await service.record(
            tenant_id,
            RankingFeedbackSignal(uuid4(), uuid4(), FeedbackSignalKind.DWELL),
        )

    assert await store.get_implicit_affinities(tenant_id) == {}


async def test_feedback_aggregate_is_tenant_local_and_bounded() -> None:
    """Repeated self-signals cannot produce an unbounded profile feature or leak to another tenant."""
    owner = uuid4()
    other = uuid4()
    event = _event("Jazz concert", "live music")
    store = InMemoryRankingFeedback()
    service = RankingFeedbackService(FixtureCatalog([event]), store)

    for _ in range(24):
        await service.record(
            owner,
            RankingFeedbackSignal(uuid4(), event.canonical_event_id, FeedbackSignalKind.CLICK),
        )

    owner_affinities = await store.get_implicit_affinities(owner)
    assert owner_affinities["jazz"] == implicit_affinity_bound()
    assert await store.get_implicit_affinities(other) == {}


async def test_recorded_implicit_feedback_reorders_a_subsequent_feed() -> None:
    """Dwell feedback changes FR-4.3 ordering independently from declared-affinity coverage."""
    tenant_id = uuid4()
    python = _event(
        "Python meetup",
        "backend systems",
        canonical_event_id=UUID("00000000-0000-0000-0000-000000000001"),
    )
    jazz = _event(
        "Jazz concert",
        "live music",
        canonical_event_id=UUID("00000000-0000-0000-0000-000000000002"),
    )
    feedback = InMemoryRankingFeedback()
    profiles = FeedbackAwareRankingProfiles(InMemoryRankingProfiles(), feedback)
    ranker = PersonalizedRanker(
        embedding=DeterministicEmbedding(dim=16),
        cross_encoder=EqualCrossEncoder(),
        profiles=profiles,
        rescorer=DeterministicFeatureRescorer(),
    )
    request = _request(tenant_id)

    before = await ranker.rerank(request, [python, jazz])
    status = await RankingFeedbackService(FixtureCatalog([python, jazz]), feedback).record(
        tenant_id,
        RankingFeedbackSignal(uuid4(), jazz.canonical_event_id, FeedbackSignalKind.DWELL),
    )
    after = await ranker.rerank(request, [python, jazz])

    assert before[0][0].canonical_event_id == python.canonical_event_id
    assert status is RankingFeedbackRecordStatus.RECORDED
    assert after[0][0].canonical_event_id == jazz.canonical_event_id


async def test_recorded_dismissal_demotes_a_subsequent_feed() -> None:
    """A dismissal lowers an otherwise equal baseline-top event on the next FR-4.3 re-score."""
    tenant_id = uuid4()
    python = _event(
        "Python meetup",
        "backend systems",
        canonical_event_id=UUID("00000000-0000-0000-0000-000000000001"),
    )
    jazz = _event(
        "Jazz concert",
        "live music",
        canonical_event_id=UUID("00000000-0000-0000-0000-000000000002"),
    )
    feedback = InMemoryRankingFeedback()
    ranker = PersonalizedRanker(
        embedding=DeterministicEmbedding(dim=16),
        cross_encoder=EqualCrossEncoder(),
        profiles=FeedbackAwareRankingProfiles(InMemoryRankingProfiles(), feedback),
        rescorer=DeterministicFeatureRescorer(),
    )
    request = _request(tenant_id)

    before = await ranker.rerank(request, [python, jazz])
    status = await RankingFeedbackService(FixtureCatalog([python, jazz]), feedback).record(
        tenant_id,
        RankingFeedbackSignal(uuid4(), python.canonical_event_id, FeedbackSignalKind.DISMISS),
    )
    after = await ranker.rerank(request, [python, jazz])

    assert before[0][0].canonical_event_id == python.canonical_event_id
    assert status is RankingFeedbackRecordStatus.RECORDED
    assert after[0][0].canonical_event_id == jazz.canonical_event_id
