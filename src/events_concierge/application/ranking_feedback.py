"""Implicit-feedback recording and profile overlay services (FR-2.1, FR-4.3, ADR-001)."""

from __future__ import annotations

from collections.abc import Mapping
from uuid import UUID

from ..domain.ranking_feedback import (
    RankingFeedbackReceipt,
    RankingFeedbackSignal,
    feedback_feature_deltas,
)
from ..ports.ranking import RankingProfilePort, UserRankingProfile
from ..ports.ranking_feedback import RankingFeedbackRecordStatus, RankingFeedbackRepository
from ..ports.repositories import CatalogRepository


class UnknownFeedbackEventError(LookupError):
    """The requested feedback target is not a current canonical catalog event."""


class RankingFeedbackService:
    """Resolve a canonical event before durable, tenant-local implicit feedback recording.

    The catalog remains tenant-neutral by ADR-001; only the derived receipt is tenant-scoped.  The
    HTTP boundary supplies the authenticated tenant and cannot submit raw event text, score, or
    duration (FR-1.1/1.3, FR-2.1, FR-4.3).
    """

    def __init__(self, catalog: CatalogRepository, feedback: RankingFeedbackRepository) -> None:
        self._catalog = catalog
        self._feedback = feedback

    async def record(
        self, tenant_id: UUID, signal: RankingFeedbackSignal
    ) -> RankingFeedbackRecordStatus:
        """Store an immutable derived receipt or converge an at-least-once replay (NFR-8)."""
        event = await self._catalog.get(signal.canonical_event_id)
        if event is None:
            raise UnknownFeedbackEventError("canonical event was not found")
        receipt = RankingFeedbackReceipt(
            signal=signal,
            feature_deltas=feedback_feature_deltas(event, signal.kind),
        )
        return await self._feedback.record_feedback(tenant_id, receipt)


class FeedbackAwareRankingProfiles:
    """Overlay immutable feedback aggregates without rewriting revisioned declared profiles.

    The existing profile repository performs whole-profile monotonic replacement.  This reader
    intentionally never read-modify-writes it, preventing concurrent feedback from losing an
    increment or overwriting declared affinities (FR-2.1, FR-4.3, ADR-001).
    """

    def __init__(self, base: RankingProfilePort, feedback: RankingFeedbackRepository) -> None:
        self._base = base
        self._feedback = feedback

    async def get_profile(self, tenant_id: UUID) -> UserRankingProfile:
        """Return declared profile state plus this tenant's bounded learned deltas."""
        base_profile = await self._base.get_profile(tenant_id)
        derived = await self._feedback.get_implicit_affinities(tenant_id)
        return UserRankingProfile(
            explicit_affinities=base_profile.explicit_affinities,
            implicit_affinities=_merge_implicit_affinities(
                base_profile.implicit_affinities,
                derived,
            ),
        )


def _merge_implicit_affinities(
    base: Mapping[str, float], derived: Mapping[str, float]
) -> Mapping[str, float]:
    """Combine stable profile inputs with bounded receipt aggregates deterministically."""
    merged = dict(base)
    for label, delta in derived.items():
        # ``derived`` is already capped by the feedback repository.  Do not clamp the existing
        # profile value here: whole-profile replacement historically permits any finite affinity,
        # and feedback must not silently rewrite its meaning when a label overlaps.
        combined = merged.get(label, 0.0) + delta
        if combined == 0.0:
            merged.pop(label, None)
        else:
            merged[label] = combined
    return merged
