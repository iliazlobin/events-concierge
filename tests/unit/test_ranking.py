"""Unit tests for the ranking adapters (FR-4.x): deterministic unit-norm embeddings and
topical rerank ordering."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest

from events_concierge.adapters.ranking.cohere import CohereRerankCrossEncoder
from events_concierge.adapters.ranking.embedding import DeterministicEmbedding
from events_concierge.adapters.ranking.ranker import (
    DeterministicFeatureRescorer,
    HybridRanker,
    InMemoryRankingProfiles,
    LightGBMFeatureRescorer,
    PersonalizedRanker,
)
from events_concierge.domain.events import CanonicalEvent
from events_concierge.domain.request import EventRequest, RequestConstraints
from events_concierge.ports.ranking import RankingFeatures, UserRankingProfile


def _event(title: str, description: str) -> CanonicalEvent:
    return CanonicalEvent(
        canonical_event_id=uuid4(),
        title=title,
        start_at=datetime(2026, 8, 1, 18, 0, tzinfo=UTC),
        description=description,
    )


def _request(raw_text: str) -> EventRequest:
    return EventRequest(
        request_id=uuid4(),
        tenant_id=uuid4(),
        raw_text=raw_text,
        constraints=RequestConstraints(),
    )


@pytest.mark.asyncio
async def test_embedding_is_deterministic() -> None:
    embedding = DeterministicEmbedding(dim=64)
    first = await embedding.embed(["Jazz night in the park"])
    second = await embedding.embed(["Jazz night in the park"])
    assert first == second


@pytest.mark.asyncio
async def test_embedding_is_unit_norm() -> None:
    embedding = DeterministicEmbedding(dim=128)
    vectors = await embedding.embed(["outdoor jazz concert", "python engineering meetup"])
    for vector in vectors:
        assert len(vector) == 128
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-9)


@pytest.mark.asyncio
async def test_embedding_zero_vector_guard() -> None:
    embedding = DeterministicEmbedding(dim=32)
    (vector,) = await embedding.embed(["!!! ---"])
    assert vector == [0.0] * 32


@pytest.mark.asyncio
async def test_shared_tokens_raise_cosine() -> None:
    embedding = DeterministicEmbedding(dim=256)
    base, near, far = await embedding.embed(
        ["live jazz music concert", "jazz concert evening", "python engineering workshop"]
    )

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert cosine(base, near) > cosine(base, far)


@pytest.mark.asyncio
async def test_ranker_orders_topical_match_first() -> None:
    ranker = HybridRanker(DeterministicEmbedding(dim=256))
    request = _request("outdoor live jazz music concert")
    match = _event("Live Jazz Concert", "An evening of live jazz music outdoors")
    unrelated = _event("Python Engineering Workshop", "Hands-on backend python systems training")

    ranked = await ranker.rerank(request, [unrelated, match])

    assert ranked[0][0].canonical_event_id == match.canonical_event_id
    assert ranked[0][1] >= ranked[1][1]


@pytest.mark.asyncio
async def test_ranker_uses_precomputed_intent_embedding() -> None:
    embedding = DeterministicEmbedding(dim=128)
    (intent,) = await embedding.embed(["art gallery opening exhibition"])
    ranker = HybridRanker(embedding)
    request = _request("unrelated raw text")
    request.intent_embedding = intent
    match = _event("Art Gallery Opening", "Contemporary art exhibition opening reception")
    unrelated = _event("Marathon Training Run", "Long distance running practice")

    ranked = await ranker.rerank(request, [unrelated, match])

    assert ranked[0][0].canonical_event_id == match.canonical_event_id


@pytest.mark.asyncio
async def test_ranker_empty_candidates() -> None:
    ranker = HybridRanker(DeterministicEmbedding())
    assert await ranker.rerank(_request("anything"), []) == []


class FixtureCrossEncoder:
    def __init__(self, scores: list[float]) -> None:
        self._scores = scores

    async def score(self, query: str, documents: list[str]) -> list[float]:
        del query
        assert len(documents) == len(self._scores)
        return self._scores


class CapturingRescorer:
    def __init__(self) -> None:
        self.features: list[RankingFeatures] = []

    async def rescore(self, features: list[RankingFeatures]) -> list[float]:
        self.features = features
        return [feature.cross_encoder_score for feature in features]


class FixtureLightGBMPredictor:
    def __init__(self) -> None:
        self.rows: list[list[float]] = []

    def predict(self, rows: list[list[float]]) -> list[float]:
        self.rows = rows
        return [sum(row) for row in rows]


@pytest.mark.asyncio
async def test_personalized_ranker_uses_neutral_features_without_a_profile() -> None:
    rescorer = CapturingRescorer()
    ranker = PersonalizedRanker(
        embedding=DeterministicEmbedding(dim=16),
        cross_encoder=FixtureCrossEncoder([0.25, 0.75]),
        profiles=InMemoryRankingProfiles(),
        rescorer=rescorer,
    )
    request = _request("anything")
    request.intent_embedding = [0.0] * 16
    first = _event("First event", "one")
    second = _event("Second event", "two")

    ranked = await ranker.rerank(request, [first, second])

    assert [event.canonical_event_id for event, _ in ranked] == [
        second.canonical_event_id,
        first.canonical_event_id,
    ]
    assert all(feature.explicit_affinity_score == 0.0 for feature in rescorer.features)
    assert all(feature.implicit_affinity_score == 0.0 for feature in rescorer.features)


@pytest.mark.asyncio
async def test_personalized_ranker_profiles_reorder_the_same_candidate_set() -> None:
    jazz_user = uuid4()
    python_user = uuid4()
    profiles = InMemoryRankingProfiles(
        {
            jazz_user: UserRankingProfile(explicit_affinities={"jazz": 1.0}),
            python_user: UserRankingProfile(explicit_affinities={"python": 1.0}),
        }
    )
    ranker = PersonalizedRanker(
        embedding=DeterministicEmbedding(dim=16),
        cross_encoder=FixtureCrossEncoder([0.5, 0.5]),
        profiles=profiles,
        rescorer=DeterministicFeatureRescorer(),
    )
    python = _event("Python meetup", "backend systems")
    jazz = _event("Jazz concert", "live music")
    candidates = [python, jazz]
    jazz_request = _request("anything")
    jazz_request.tenant_id = jazz_user
    jazz_request.intent_embedding = [0.0] * 16
    python_request = _request("anything")
    python_request.tenant_id = python_user
    python_request.intent_embedding = [0.0] * 16

    jazz_ranked = await ranker.rerank(jazz_request, candidates)
    python_ranked = await ranker.rerank(python_request, candidates)

    assert jazz_ranked[0][0].canonical_event_id == jazz.canonical_event_id
    assert python_ranked[0][0].canonical_event_id == python.canonical_event_id


@pytest.mark.asyncio
async def test_lightgbm_rescorer_passes_stable_feature_rows_to_predictor() -> None:
    predictor = FixtureLightGBMPredictor()
    rescorer = LightGBMFeatureRescorer(predictor)
    features = [
        RankingFeatures(
            cross_encoder_score=0.8,
            request_event_cosine=0.4,
            explicit_affinity_score=0.2,
            implicit_affinity_score=-0.1,
        )
    ]

    scores = await rescorer.rescore(features)

    assert predictor.rows == [[0.8, 0.4, 0.2, -0.1]]
    assert scores == [1.3]


@pytest.mark.asyncio
async def test_cohere_cross_encoder_uses_v2_rerank_fixture_without_network() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://api.cohere.com/v2/rerank"
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body == {
            "model": "rerank-v3.5",
            "query": "live jazz",
            "documents": ["Python meetup", "Live jazz concert"],
            "top_n": 2,
        }
        return httpx.Response(
            200,
            json={
                "results": [
                    {"index": 1, "relevance_score": 0.96},
                    {"index": 0, "relevance_score": 0.04},
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        cross_encoder = CohereRerankCrossEncoder("test-key", client=client)
        scores = await cross_encoder.score("live jazz", ["Python meetup", "Live jazz concert"])

    assert scores == [0.04, 0.96]


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "must be an object"),
        ({}, "results list"),
        ({"results": "not-a-list"}, "results list"),
        ({"results": ["not-an-object"]}, "must be an object"),
        ({"results": [{"index": "0", "relevance_score": 0.5}]}, "index must be an integer"),
        ({"results": [{"index": 2, "relevance_score": 0.5}]}, "outside"),
        ({"results": [{"index": 0, "relevance_score": "0.5"}]}, "must be numeric"),
        (
            {
                "results": [
                    {"index": 0, "relevance_score": 0.5},
                    {"index": 0, "relevance_score": 0.4},
                ]
            },
            "duplicate",
        ),
        ({"results": [{"index": 0, "relevance_score": float("nan")}]}, "finite"),
        ({"results": [{"index": 0, "relevance_score": 0.5}]}, "every input"),
    ],
)
def test_cohere_cross_encoder_rejects_malformed_provider_payloads(
    payload: object, message: str
) -> None:
    """A malformed rerank response cannot silently corrupt candidate ordering (FR-4.2)."""
    with pytest.raises(ValueError, match=message):
        CohereRerankCrossEncoder._parse_scores(payload, document_count=2)
