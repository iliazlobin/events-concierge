"""Offline and production-ready ranking adapters for FR-4.2 and FR-4.3.

Retrieval fusion (dense ANN + sparse tsvector via RRF) stays in the catalog repository.  This
module owns only post-retrieval ranking: deterministic local doubles for development, profile
feature assembly, and the composite ``RankerPort`` implementation.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from uuid import UUID

import numpy as np

from ...domain.events import CanonicalEvent
from ...domain.ranking_feedback import ranking_event_document, ranking_tokens
from ...domain.request import EventRequest
from ...ports.ranking import (
    CrossEncoderPort,
    EmbeddingPort,
    FeatureRescorerPort,
    LightGBMPredictor,
    RankingFeatures,
    RankingProfilePort,
    RankingProfileUpdate,
    RankingProfileUpdateResult,
    RankingProfileUpdateStatus,
    UserRankingProfile,
)


class HybridRanker:
    """Implements RankerPort. Cosine-scores candidates against the request intent embedding."""

    def __init__(self, embedding: EmbeddingPort) -> None:
        self._embedding = embedding

    async def rerank(
        self, request: EventRequest, candidates: list[CanonicalEvent]
    ) -> list[tuple[CanonicalEvent, float]]:
        if not candidates:
            return []

        if request.intent_embedding is not None:
            intent = np.asarray(request.intent_embedding, dtype=np.float64)
        else:
            intent = np.asarray(
                (await self._embedding.embed([request.raw_text]))[0], dtype=np.float64
            )

        candidate_vectors = await self._embedding.embed(
            [f"{c.title} {c.description}" for c in candidates]
        )
        scored = [
            (candidate, self._cosine(intent, np.asarray(vector, dtype=np.float64)))
            for candidate, vector in zip(candidates, candidate_vectors, strict=True)
        ]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored

    @staticmethod
    def _cosine(a: np.ndarray, b: np.ndarray) -> float:
        if a.shape != b.shape:
            raise ValueError("cannot calculate cosine for vectors with different dimensions")
        norm = float(np.linalg.norm(a) * np.linalg.norm(b))
        if norm == 0.0:
            return 0.0
        return float(np.dot(a, b) / norm)


class DeterministicCrossEncoder:
    """Local CrossEncoderPort double using joint query/document token-frequency cosine (FR-4.2).

    It is deliberately reproducible and requires no model download.  Production composition can
    replace it with ``CohereRerankCrossEncoder`` while the deterministic embedding remains the
    local test double for candidate-content vectors.
    """

    async def score(self, query: str, documents: list[str]) -> list[float]:
        query_terms = Counter(ranking_tokens(query))
        return [self._score_one(query_terms, document) for document in documents]

    @staticmethod
    def _score_one(query_terms: Counter[str], document: str) -> float:
        document_terms = Counter(ranking_tokens(document))
        numerator = sum(count * document_terms[token] for token, count in query_terms.items())
        query_norm = math.sqrt(sum(count * count for count in query_terms.values()))
        document_norm = math.sqrt(sum(count * count for count in document_terms.values()))
        if query_norm == 0.0 or document_norm == 0.0:
            return 0.0
        return numerator / (query_norm * document_norm)


class InMemoryRankingProfiles:
    """Offline revisioned profile repository with a neutral cold-start fallback.

    This deterministic test double follows the durable replacement contract without deciding
    deferred onboarding, feedback collection, or RLS persistence design (FR-2.1, FR-4.3/FR-4.4,
    ADR-001).
    """

    def __init__(self, profiles: Mapping[UUID, UserRankingProfile] | None = None) -> None:
        self._profiles = {
            tenant_id: (profile, 0) for tenant_id, profile in (profiles or {}).items()
        }

    async def get_profile(self, tenant_id: UUID) -> UserRankingProfile:
        stored = self._profiles.get(tenant_id)
        return stored[0] if stored is not None else UserRankingProfile()

    async def replace_profile(
        self, tenant_id: UUID, update: RankingProfileUpdate
    ) -> RankingProfileUpdateResult:
        """Converge a test delivery using the same revision semantics as PostgreSQL."""
        stored = self._profiles.get(tenant_id)
        if stored is None or update.revision > stored[1]:
            self._profiles[tenant_id] = (update.profile, update.revision)
            return RankingProfileUpdateResult(
                status=RankingProfileUpdateStatus.APPLIED,
                profile=update.profile,
                revision=update.revision,
            )

        profile, revision = stored
        if revision == update.revision:
            if profile != update.profile:
                raise ValueError("ranking profile revision is already bound to a different profile")
            return RankingProfileUpdateResult(
                status=RankingProfileUpdateStatus.REPLAYED,
                profile=profile,
                revision=revision,
            )
        return RankingProfileUpdateResult(
            status=RankingProfileUpdateStatus.STALE,
            profile=profile,
            revision=revision,
        )

    def set_profile(
        self, tenant_id: UUID, profile: UserRankingProfile, *, revision: int = 0
    ) -> None:
        """Install explicit fixture state, preserving revision-zero constructor compatibility."""
        if not isinstance(profile, UserRankingProfile):
            raise TypeError("ranking profile fixture requires a UserRankingProfile")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise ValueError("ranking profile fixture revision must be a non-negative integer")
        self._profiles[tenant_id] = (profile, revision)


class DeterministicFeatureRescorer:
    """Stable local FeatureRescorerPort double for the FR-4.3 feature contract."""

    async def rescore(self, features: list[RankingFeatures]) -> list[float]:
        return [
            (
                0.60 * feature.cross_encoder_score
                + 0.25 * feature.request_event_cosine
                + 0.10 * feature.explicit_affinity_score
                + 0.05 * feature.implicit_affinity_score
            )
            for feature in features
        ]


class LightGBMFeatureRescorer:
    """FeatureRescorerPort backed by an injected LightGBM/LambdaMART-compatible predictor.

    A provisioned deployment injects a loaded model exposing ``predict(rows)``.  Keeping that
    dependency outside the adapter avoids model downloads and lets the offline suite supply a
    deterministic fake while preserving the real inference boundary (FR-4.3).
    """

    def __init__(self, predictor: LightGBMPredictor) -> None:
        self._predictor = predictor

    async def rescore(self, features: list[RankingFeatures]) -> list[float]:
        scores = list(self._predictor.predict([feature.as_row() for feature in features]))
        if len(scores) != len(features):
            raise ValueError(
                "LightGBM predictor returned a score count different from its input rows"
            )
        return [float(score) for score in scores]


class PersonalizedRanker:
    """Composite RankerPort: cross-encoder relevance followed by per-user feature re-scoring.

    The candidate set is already RRF-fused.  Each event receives cross-encoder relevance,
    request↔event embedding cosine, and profile affinity features before the injected rescorer
    produces its final score (FR-4.2/FR-4.3).  An absent profile supplies neutral affinity values,
    preserving the cold-start no-error floor in FR-4.4.
    """

    def __init__(
        self,
        embedding: EmbeddingPort,
        cross_encoder: CrossEncoderPort,
        profiles: RankingProfilePort,
        rescorer: FeatureRescorerPort,
    ) -> None:
        self._embedding = embedding
        self._cross_encoder = cross_encoder
        self._profiles = profiles
        self._rescorer = rescorer

    async def rerank(
        self, request: EventRequest, candidates: list[CanonicalEvent]
    ) -> list[tuple[CanonicalEvent, float]]:
        if not candidates:
            return []

        documents = [self._event_document(candidate) for candidate in candidates]
        profile = await self._profiles.get_profile(request.tenant_id)
        intent = await self._intent_vector(request)
        candidate_vectors = await self._candidate_vectors(candidates, documents)
        cross_encoder_scores = await self._cross_encoder.score(request.raw_text, documents)

        if len(cross_encoder_scores) != len(candidates):
            raise ValueError(
                "cross-encoder returned a score count different from its input documents"
            )

        features = [
            RankingFeatures(
                cross_encoder_score=cross_encoder_score,
                request_event_cosine=HybridRanker._cosine(
                    intent, np.asarray(candidate_vector, dtype=np.float64)
                ),
                explicit_affinity_score=self._affinity_score(profile.explicit_affinities, document),
                implicit_affinity_score=self._affinity_score(profile.implicit_affinities, document),
            )
            for document, candidate_vector, cross_encoder_score in zip(
                documents, candidate_vectors, cross_encoder_scores, strict=True
            )
        ]
        scores = await self._rescorer.rescore(features)
        if len(scores) != len(candidates):
            raise ValueError(
                "feature rescorer returned a score count different from its input rows"
            )

        scored = list(zip(candidates, scores, strict=True))
        scored.sort(key=lambda pair: (-pair[1], str(pair[0].canonical_event_id)))
        return [(candidate, score) for candidate, score in scored]

    async def _intent_vector(self, request: EventRequest) -> np.ndarray:
        if request.intent_embedding is not None:
            return np.asarray(request.intent_embedding, dtype=np.float64)
        vectors = await self._embedding.embed([request.raw_text])
        if len(vectors) != 1:
            raise ValueError(
                "embedding provider returned a vector count different from its input texts"
            )
        return np.asarray(vectors[0], dtype=np.float64)

    async def _candidate_vectors(
        self, candidates: list[CanonicalEvent], documents: list[str]
    ) -> list[list[float]]:
        missing_documents = [
            document
            for candidate, document in zip(candidates, documents, strict=True)
            if candidate.embedding is None
        ]
        missing_vectors = (
            await self._embedding.embed(missing_documents) if missing_documents else []
        )
        if len(missing_vectors) != len(missing_documents):
            raise ValueError(
                "embedding provider returned a vector count different from its input texts"
            )

        missing_index = 0
        vectors: list[list[float]] = []
        for candidate in candidates:
            if candidate.embedding is not None:
                vectors.append(candidate.embedding)
            else:
                vectors.append(missing_vectors[missing_index])
                missing_index += 1
        return vectors

    @staticmethod
    def _event_document(candidate: CanonicalEvent) -> str:
        return ranking_event_document(candidate)

    @staticmethod
    def _affinity_score(affinities: Mapping[str, float], document: str) -> float:
        document_terms = set(ranking_tokens(document))
        weighted_terms = [
            (set(ranking_tokens(label)), weight)
            for label, weight in affinities.items()
            if ranking_tokens(label) and math.isfinite(weight)
        ]
        denominator = sum(abs(weight) for _, weight in weighted_terms)
        if not document_terms or denominator == 0.0:
            return 0.0
        return (
            sum(weight for terms, weight in weighted_terms if terms.issubset(document_terms))
            / denominator
        )
