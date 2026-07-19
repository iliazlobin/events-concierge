"""Ranking ports: embeddings, cross-encoder relevance, and per-user re-scoring (FR-4.2/4.3).

The catalog repository owns dense/sparse RRF retrieval.  These ports keep the post-retrieval model
stack swappable: a real hosted cross-encoder and LightGBM-compatible predictor can be used in a
provisioned deployment, while deterministic implementations keep local tests fully offline.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from numbers import Real
from types import MappingProxyType
from typing import Protocol
from uuid import UUID

from ..domain.events import CanonicalEvent
from ..domain.request import EventRequest


class EmbeddingPort(Protocol):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one dense vector per input text (intent embeddings, event descriptions)."""
        ...


@dataclass(frozen=True, slots=True)
class UserRankingProfile:
    """Tenant-local, immutable ranking inputs for the FR-4.3 re-score layer.

    ``explicit_affinities`` represent declared preferences.  ``implicit_affinities`` may include
    the separately persisted P17 feedback overlay. This value object deliberately does not decide
    collection, decay, or model-training policy; values are normalized before they cross an
    adapter boundary so a retry has a stable JSON representation (FR-2.1, FR-4.3, FR-4.4,
    ADR-001).
    """

    explicit_affinities: Mapping[str, float] = field(default_factory=dict)
    implicit_affinities: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Copy caller-owned maps into canonical, immutable finite affinity maps."""
        object.__setattr__(
            self,
            "explicit_affinities",
            _normalize_affinities(self.explicit_affinities, "explicit_affinities"),
        )
        object.__setattr__(
            self,
            "implicit_affinities",
            _normalize_affinities(self.implicit_affinities, "implicit_affinities"),
        )


class RankingProfileUpdateStatus(StrEnum):
    """Converged outcome for one revisioned profile replacement (FR-2.1, FR-4.3)."""

    APPLIED = "applied"
    REPLAYED = "replayed"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class RankingProfileUpdate:
    """One whole-profile replacement with caller-minted monotonic revision.

    The profile store owns neither click/dwell collection nor affinity merge/decay semantics.
    A strictly positive revision makes an at-least-once delivery distinguishable from a newer
    replacement, while an exact same-revision replay remains harmless (FR-2.1, FR-4.3, ADR-001).
    """

    profile: UserRankingProfile
    revision: int

    def __post_init__(self) -> None:
        """Reject an ambiguous or non-monotonic caller revision before persistence."""
        if not isinstance(self.profile, UserRankingProfile):
            raise TypeError("ranking profile update requires a UserRankingProfile")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 1
        ):
            raise ValueError("ranking profile update revision must be a positive integer")


@dataclass(frozen=True, slots=True)
class RankingProfileUpdateResult:
    """Stored profile state observed after a revisioned replacement attempt."""

    status: RankingProfileUpdateStatus
    profile: UserRankingProfile
    revision: int

    def __post_init__(self) -> None:
        """Keep return values closed and suitable for deterministic retry handling."""
        if not isinstance(self.status, RankingProfileUpdateStatus):
            raise TypeError("ranking profile update result has an unknown status")
        if not isinstance(self.profile, UserRankingProfile):
            raise TypeError("ranking profile update result requires a UserRankingProfile")
        if (
            isinstance(self.revision, bool)
            or not isinstance(self.revision, int)
            or self.revision < 0
        ):
            raise ValueError("ranking profile update result revision must be non-negative")


def _normalize_affinities(affinities: Mapping[str, float], field_name: str) -> Mapping[str, float]:
    """Normalize safely serializable labels and finite weights for every profile adapter."""
    if not isinstance(affinities, Mapping):
        raise TypeError(f"{field_name} must be a mapping")

    normalized: dict[str, float] = {}
    for raw_label, raw_weight in affinities.items():
        if not isinstance(raw_label, str):
            raise TypeError(f"{field_name} labels must be strings")
        label = raw_label.strip()
        if not label:
            raise ValueError(f"{field_name} labels must not be blank")
        if isinstance(raw_weight, bool) or not isinstance(raw_weight, Real):
            raise TypeError(f"{field_name} weights must be non-boolean real numbers")
        weight = float(raw_weight)
        if not isfinite(weight):
            raise ValueError(f"{field_name} weights must be finite")
        if label in normalized:
            raise ValueError(f"{field_name} contains duplicate normalized label {label!r}")
        normalized[label] = weight
    return MappingProxyType(normalized)


@dataclass(frozen=True, slots=True)
class RankingFeatures:
    """Normalized model inputs for one request/candidate pair (FR-4.2/4.3)."""

    cross_encoder_score: float
    request_event_cosine: float
    explicit_affinity_score: float
    implicit_affinity_score: float

    def as_row(self) -> list[float]:
        """Return the stable feature ordering consumed by a LightGBM-compatible predictor."""
        return [
            self.cross_encoder_score,
            self.request_event_cosine,
            self.explicit_affinity_score,
            self.implicit_affinity_score,
        ]


class CrossEncoderPort(Protocol):
    async def score(self, query: str, documents: list[str]) -> list[float]:
        """Return one relevance score per document, preserving input order (FR-4.2)."""
        ...


class RankingProfilePort(Protocol):
    async def get_profile(self, tenant_id: UUID) -> UserRankingProfile:
        """Return only the requesting tenant's explicit and implicit ranking features (FR-2.1)."""
        ...


class RankingProfileRepository(RankingProfilePort, Protocol):
    """Tenant-isolated durable profile replacement boundary (FR-1.2--FR-1.4, FR-2.1)."""

    async def replace_profile(
        self, tenant_id: UUID, update: RankingProfileUpdate
    ) -> RankingProfileUpdateResult:
        """Apply/replay/reject one whole profile without inventing feedback merge semantics."""
        ...


class FeatureRescorerPort(Protocol):
    async def rescore(self, features: list[RankingFeatures]) -> list[float]:
        """Return one personalized final score per feature row (FR-4.3)."""
        ...


class LightGBMPredictor(Protocol):
    def predict(self, rows: list[list[float]]) -> list[float]:
        """The small inference surface shared by a loaded LightGBM Booster and test fakes."""
        ...


class RankerPort(Protocol):
    async def rerank(
        self, request: EventRequest, candidates: list[CanonicalEvent]
    ) -> list[tuple[CanonicalEvent, float]]:
        """Re-score candidates for this user/request, returning (event, score) in ranked order.

        The adapter fuses dense + sparse retrieval (RRF), applies the cross-encoder, and layers the
        per-user feature re-score; scroll/dwell signals feed back as implicit preferences."""
        ...
