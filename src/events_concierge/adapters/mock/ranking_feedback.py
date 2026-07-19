"""Offline deterministic implicit-feedback repository (FR-2.1, FR-4.3, NFR-8)."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from uuid import UUID

from ...domain.ranking_feedback import RankingFeedbackReceipt, bound_implicit_affinity
from ...ports.ranking_feedback import (
    RankingFeedbackConflictError,
    RankingFeedbackRecordStatus,
)


class InMemoryRankingFeedback:
    """Replay-safe feedback double with the same bounded aggregate contract as PostgreSQL."""

    def __init__(self) -> None:
        self._receipts: dict[tuple[UUID, UUID], RankingFeedbackReceipt] = {}

    async def record_feedback(
        self, tenant_id: UUID, receipt: RankingFeedbackReceipt
    ) -> RankingFeedbackRecordStatus:
        """Store the first derived snapshot; changed client-visible signals are conflicts."""
        key = (tenant_id, receipt.signal.signal_id)
        stored = self._receipts.get(key)
        if stored is None:
            self._receipts[key] = receipt
            return RankingFeedbackRecordStatus.RECORDED
        if stored.signal != receipt.signal:
            raise RankingFeedbackConflictError(
                "ranking feedback signal id is already bound to a different payload"
            )
        return RankingFeedbackRecordStatus.REPLAYED

    async def get_implicit_affinities(self, tenant_id: UUID) -> Mapping[str, float]:
        """Aggregate a tenant's immutable receipt deltas without cross-tenant visibility."""
        totals: dict[str, float] = {}
        for (stored_tenant_id, _), receipt in self._receipts.items():
            if stored_tenant_id != tenant_id:
                continue
            for label, delta in receipt.feature_deltas.items():
                totals[label] = totals.get(label, 0.0) + delta
        return MappingProxyType(
            {
                label: bounded
                for label, total in sorted(totals.items())
                if (bounded := bound_implicit_affinity(total)) != 0.0
            }
        )
