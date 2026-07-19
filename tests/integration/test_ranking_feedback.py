"""Durable implicit-ranking feedback guarantees (FR-2.1, FR-4.3, NFR-7/8).

These checks exercise the real non-superuser application role.  Feedback is an append-only,
tenant-scoped receipt stream: RLS hides another user's interactions, a caller-minted signal id
converges an at-least-once retry, and the persisted aggregate reaches the existing personalized
ranker rather than stopping at JSONB storage.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import RowMapping

from events_concierge.adapters.postgres.catalog import PostgresCatalogRepository
from events_concierge.adapters.postgres.ranking import PostgresRankingProfileRepository
from events_concierge.adapters.postgres.ranking_feedback import PostgresRankingFeedbackRepository
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.adapters.ranking.embedding import DeterministicEmbedding
from events_concierge.adapters.ranking.ranker import (
    DeterministicCrossEncoder,
    DeterministicFeatureRescorer,
    PersonalizedRanker,
)
from events_concierge.application.ranking_feedback import (
    FeedbackAwareRankingProfiles,
    RankingFeedbackService,
)
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import Source
from events_concierge.domain.events import CandidateEvent, CanonicalEvent
from events_concierge.domain.ranking_feedback import (
    FeedbackSignalKind,
    RankingFeedbackReceipt,
    RankingFeedbackSignal,
)
from events_concierge.domain.request import EventRequest, RequestConstraints, TimeWindow
from events_concierge.infra.db import tenant_session_scope
from events_concierge.ports.ranking_feedback import (
    RankingFeedbackConflictError,
    RankingFeedbackRecordStatus,
)

pytestmark = pytest.mark.integration

_VISIBLE_RECEIPTS = text(
    """
    SELECT tenant_id, signal_id, canonical_event_id, signal_kind
    FROM public.tenant_ranking_feedback_receipts
    ORDER BY tenant_id, signal_id
    """
)


async def test_ranking_feedback_obeys_forced_rls_for_owner_other_unset_and_empty_context(
    db: None,
) -> None:
    """A receipt is visible only under its tenant's exact non-superuser RLS context."""
    owner = await _add_tenant("ranking-feedback-owner")
    other = await _add_tenant("ranking-feedback-other")
    event = await _seed_event("ranking-feedback-rls")
    feedback = PostgresRankingFeedbackRepository()
    owner_signal = RankingFeedbackSignal(uuid4(), event.canonical_event_id, FeedbackSignalKind.CLICK)
    other_signal = RankingFeedbackSignal(uuid4(), event.canonical_event_id, FeedbackSignalKind.DISMISS)

    assert (
        await feedback.record_feedback(owner, _receipt(owner_signal, {"jazz": 1.0}))
        is RankingFeedbackRecordStatus.RECORDED
    )
    assert (
        await feedback.record_feedback(other, _receipt(other_signal, {"hiking": -1.0}))
        is RankingFeedbackRecordStatus.RECORDED
    )

    async with tenant_session_scope(owner) as session:
        owner_rows = (await session.execute(_VISIBLE_RECEIPTS)).mappings().all()
    async with tenant_session_scope(other) as session:
        other_rows = (await session.execute(_VISIBLE_RECEIPTS)).mappings().all()
    async with tenant_session_scope(None) as session:
        unset_rows = (await session.execute(_VISIBLE_RECEIPTS)).mappings().all()
    async with tenant_session_scope(None) as session:
        empty_context = (
            await session.execute(text("SELECT set_config('app.tenant_id', '', true)"))
        ).scalar_one()
        empty_rows = (await session.execute(_VISIBLE_RECEIPTS)).mappings().all()

    assert _rows(owner_rows) == [
        (owner, owner_signal.signal_id, event.canonical_event_id, FeedbackSignalKind.CLICK.value)
    ]
    assert _rows(other_rows) == [
        (other, other_signal.signal_id, event.canonical_event_id, FeedbackSignalKind.DISMISS.value)
    ]
    assert unset_rows == []
    assert empty_context == ""
    assert empty_rows == []


async def test_ranking_feedback_exact_replay_converges_and_conflicting_signal_id_is_rejected(
    db: None,
) -> None:
    """A replay appends no second delta, while a changed receipt cannot rebind its signal id."""
    tenant_id = await _add_tenant("ranking-feedback-replay")
    event = await _seed_event("ranking-feedback-replay")
    feedback = PostgresRankingFeedbackRepository()
    signal = RankingFeedbackSignal(uuid4(), event.canonical_event_id, FeedbackSignalKind.CLICK)
    first = _receipt(signal, {"jazz": 1.0})
    enriched_retry = _receipt(signal, {"saxophone": 1.0})
    conflicting = _receipt(
        RankingFeedbackSignal(signal.signal_id, event.canonical_event_id, FeedbackSignalKind.DISMISS),
        {"jazz": -1.0},
    )

    assert await feedback.record_feedback(tenant_id, first) is RankingFeedbackRecordStatus.RECORDED
    # The first map is an immutable server-derived snapshot. A later catalog enrichment must not
    # turn the same client signal retry into a conflict or add a second delta.
    assert (
        await feedback.record_feedback(tenant_id, enriched_retry)
        is RankingFeedbackRecordStatus.REPLAYED
    )
    assert await feedback.record_feedback(tenant_id, first) is RankingFeedbackRecordStatus.REPLAYED
    with pytest.raises(RankingFeedbackConflictError, match="already bound"):
        await feedback.record_feedback(tenant_id, conflicting)

    async with tenant_session_scope(tenant_id) as session:
        count = (
            await session.execute(
                text(
                    """SELECT count(*) FROM public.tenant_ranking_feedback_receipts
                       WHERE tenant_id = :tenant_id AND signal_id = :signal_id"""
                ),
                {"tenant_id": tenant_id, "signal_id": signal.signal_id},
            )
        ).scalar_one()

    assert int(count) == 1
    assert await feedback.get_implicit_affinities(tenant_id) == {"jazz": 1.0}


async def test_app_role_cannot_update_or_delete_a_ranking_feedback_receipt(db: None) -> None:
    """The runtime role may append/read its RLS-scoped receipts but can never rewrite history."""
    tenant_id = await _add_tenant("ranking-feedback-immutable")
    event = await _seed_event("ranking-feedback-immutable")
    feedback = PostgresRankingFeedbackRepository()
    signal = RankingFeedbackSignal(uuid4(), event.canonical_event_id, FeedbackSignalKind.DWELL)
    receipt = _receipt(signal, {"music": 0.75})
    assert (
        await feedback.record_feedback(tenant_id, receipt)
        is RankingFeedbackRecordStatus.RECORDED
    )

    with pytest.raises(Exception, match="permission denied"):
        async with tenant_session_scope(tenant_id) as session:
            await session.execute(
                text(
                    """UPDATE public.tenant_ranking_feedback_receipts
                       SET signal_kind = 'dismiss'
                       WHERE tenant_id = :tenant_id AND signal_id = :signal_id"""
                ),
                {"tenant_id": tenant_id, "signal_id": signal.signal_id},
            )
    with pytest.raises(Exception, match="permission denied"):
        async with tenant_session_scope(tenant_id) as session:
            await session.execute(
                text(
                    """DELETE FROM public.tenant_ranking_feedback_receipts
                       WHERE tenant_id = :tenant_id AND signal_id = :signal_id"""
                ),
                {"tenant_id": tenant_id, "signal_id": signal.signal_id},
            )

    assert await feedback.record_feedback(tenant_id, receipt) is RankingFeedbackRecordStatus.REPLAYED
    assert await feedback.get_implicit_affinities(tenant_id) == {"music": 0.75}


async def test_app_role_rejects_cross_tenant_and_malformed_feedback_receipts(db: None) -> None:
    """RLS WITH CHECK and the JSONB constraint guard direct app-role writes, not only the adapter."""
    owner = await _add_tenant("ranking-feedback-direct-owner")
    other = await _add_tenant("ranking-feedback-direct-other")
    event = await _seed_event("ranking-feedback-direct")

    with pytest.raises(Exception, match="row-level security"):
        async with tenant_session_scope(owner) as session:
            await session.execute(
                text(
                    """INSERT INTO public.tenant_ranking_feedback_receipts (
                           tenant_id, signal_id, canonical_event_id, signal_kind, feature_deltas
                       )
                       VALUES (
                           :tenant_id, :signal_id, :canonical_event_id, 'click',
                           CAST(:feature_deltas AS jsonb)
                       )"""
                ),
                {
                    "tenant_id": other,
                    "signal_id": uuid4(),
                    "canonical_event_id": event.canonical_event_id,
                    "feature_deltas": '{"jazz":1.0}',
                },
            )

    with pytest.raises(
        Exception, match="tenant_ranking_feedback_receipts_feature_deltas_valid"
    ):
        async with tenant_session_scope(owner) as session:
            await session.execute(
                text(
                    """INSERT INTO public.tenant_ranking_feedback_receipts (
                           tenant_id, signal_id, canonical_event_id, signal_kind, feature_deltas
                       )
                       VALUES (
                           :tenant_id, :signal_id, :canonical_event_id, 'click',
                           CAST(:feature_deltas AS jsonb)
                       )"""
                ),
                {
                    "tenant_id": owner,
                    "signal_id": uuid4(),
                    "canonical_event_id": event.canonical_event_id,
                    "feature_deltas": '{"not an allowed token":true}',
                },
            )

    async with tenant_session_scope(owner) as session:
        count = (
            await session.execute(
                text(
                    """SELECT count(*) FROM public.tenant_ranking_feedback_receipts
                       WHERE tenant_id = :tenant_id"""
                ),
                {"tenant_id": owner},
            )
        ).scalar_one()
    assert int(count) == 0


async def test_persisted_feedback_reorders_a_later_personalized_ranker_feed(db: None) -> None:
    """A recorded public-event click materially reorders a later real per-tenant re-score."""
    tenant_id = await _add_tenant("ranking-feedback-rerank")
    embedding = DeterministicEmbedding()
    catalog = PostgresCatalogRepository(embedding)
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=220)
    jazz_tag, systems_tag = uuid4().hex, uuid4().hex
    seeded = await catalog.upsert_candidates(
        [
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id=f"ranking-feedback-jazz-{jazz_tag}",
                title=f"Obsidian jazz {jazz_tag}",
                description="saxophone rhythm",
                start_at=start,
                registration_url=f"https://example.test/ranking-feedback/{jazz_tag}",
            ),
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id=f"ranking-feedback-systems-{systems_tag}",
                title=f"Compiler graph {systems_tag}",
                description="neural systems",
                start_at=start + timedelta(minutes=2),
                registration_url=f"https://example.test/ranking-feedback/{systems_tag}",
            ),
        ]
    )
    retrieved = await catalog.retrieve(
        RequestConstraints(
            time_window=TimeWindow(
                start=start - timedelta(minutes=1), end=start + timedelta(minutes=3)
            )
        ),
        intent_embedding=None,
        limit=10,
    )
    seeded_ids = {event.canonical_event_id for event in seeded}
    candidates = [event for event in retrieved if event.canonical_event_id in seeded_ids]
    assert {event.canonical_event_id for event in candidates} == seeded_ids

    feedback = PostgresRankingFeedbackRepository()
    profiles = FeedbackAwareRankingProfiles(PostgresRankingProfileRepository(), feedback)
    ranker = PersonalizedRanker(
        embedding=embedding,
        cross_encoder=DeterministicCrossEncoder(),
        profiles=profiles,
        rescorer=DeterministicFeatureRescorer(),
    )
    request = EventRequest(
        request_id=uuid4(),
        tenant_id=tenant_id,
        raw_text="qzxvfeedbacknomatch",
        constraints=RequestConstraints(),
        intent_embedding=[0.0] * 384,
    )

    before = await ranker.rerank(request, candidates)
    target = before[-1][0]
    before_scores = {event.canonical_event_id: score for event, score in before}
    assert set(before_scores.values()) == {0.0}

    status = await RankingFeedbackService(catalog, feedback).record(
        tenant_id,
        RankingFeedbackSignal(uuid4(), target.canonical_event_id, FeedbackSignalKind.CLICK),
    )
    after = await ranker.rerank(request, candidates)

    assert status is RankingFeedbackRecordStatus.RECORDED
    assert after[0][0].canonical_event_id == target.canonical_event_id
    assert after[0][1] > before_scores[target.canonical_event_id]


async def _add_tenant(prefix: str) -> UUID:
    """Provision a real tenant because receipts FK to the tenant identity boundary."""
    tenant_id = uuid4()
    tag = f"{prefix}-{tenant_id.hex}"
    await PostgresTenantRepository().add(
        Tenant(tenant_id, f"oidc|{tag}", f"{tag}@example.com", f"{tag}@u.test")
    )
    return tenant_id


async def _seed_event(prefix: str) -> CanonicalEvent:
    """Persist a catalog event for the receipt's canonical-event foreign key."""
    tag = uuid4().hex
    return (
        await PostgresCatalogRepository(DeterministicEmbedding()).upsert_candidates(
            [
                CandidateEvent(
                    source=Source.PUBLIC_JSONLD,
                    source_event_id=f"{prefix}-{tag}",
                    title=f"{prefix} jazz {tag}",
                    description="durable feedback fixture",
                    start_at=datetime.now(UTC).replace(microsecond=0) + timedelta(days=180),
                    registration_url=f"https://example.test/{prefix}/{tag}",
                )
            ]
        )
    )[0]


def _receipt(
    signal: RankingFeedbackSignal, feature_deltas: dict[str, float]
) -> RankingFeedbackReceipt:
    """Build one explicitly bounded immutable receipt for direct repository assertions."""
    return RankingFeedbackReceipt(signal=signal, feature_deltas=feature_deltas)


def _rows(rows: Sequence[RowMapping]) -> list[tuple[UUID, UUID, UUID, str]]:
    """Validate the app-role projection shape before asserting RLS visibility exactly."""
    normalized: list[tuple[UUID, UUID, UUID, str]] = []
    for row in rows:
        tenant_id = row["tenant_id"]
        signal_id = row["signal_id"]
        canonical_event_id = row["canonical_event_id"]
        signal_kind = row["signal_kind"]
        if (
            not isinstance(tenant_id, UUID)
            or not isinstance(signal_id, UUID)
            or not isinstance(canonical_event_id, UUID)
            or not isinstance(signal_kind, str)
        ):
            raise RuntimeError("ranking-feedback query returned an unexpected row shape")
        normalized.append((tenant_id, signal_id, canonical_event_id, signal_kind))
    return normalized
