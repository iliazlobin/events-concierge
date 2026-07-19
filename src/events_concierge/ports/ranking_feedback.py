"""Tenant-isolated durable implicit-ranking feedback port (FR-2.1, FR-4.3, NFR-7/8)."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from ..domain.ranking_feedback import RankingFeedbackReceipt


class RankingFeedbackRecordStatus(StrEnum):
    """Converged outcome for one caller-minted feedback signal receipt."""

    RECORDED = "recorded"
    REPLAYED = "replayed"


class RankingFeedbackConflictError(ValueError):
    """A signal id was replayed with a payload other than its immutable first receipt."""


class RankingFeedbackRepository(Protocol):
    """Persist immutable feedback receipts and expose only their tenant-local aggregate."""

    async def record_feedback(
        self, tenant_id: UUID, receipt: RankingFeedbackReceipt
    ) -> RankingFeedbackRecordStatus:
        """Record or exactly replay one bounded interaction under FORCE RLS (NFR-7/8)."""
        ...

    async def get_implicit_affinities(self, tenant_id: UUID) -> Mapping[str, float]:
        """Return bounded aggregate deltas for this tenant and no other tenant (FR-4.3)."""
        ...
