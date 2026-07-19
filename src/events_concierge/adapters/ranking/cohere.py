"""Cohere Rerank v3.5 cross-encoder adapter (FR-4.2).

The adapter uses Cohere's V2 ``/rerank`` endpoint through an injectable ``httpx.AsyncClient``.
That keeps provider credentials and transport configuration at composition time while allowing
offline fixture tests to exercise the production request/response contract.
"""

from __future__ import annotations

import math
from typing import cast

import httpx

_RERANK_URL = "https://api.cohere.com/v2/rerank"


class CohereRerankCrossEncoder:
    """CrossEncoderPort implementation for Cohere ``rerank-v3.5`` (FR-4.2).

    An injected client is useful for proxies, retry-aware transports, and offline tests.  Without
    one, a short-lived client is created for the call so this adapter never leaks a connection.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = "rerank-v3.5",
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        if not api_key.strip():
            raise ValueError("Cohere API key must not be empty")
        if not model.strip():
            raise ValueError("Cohere rerank model must not be empty")
        if timeout_s <= 0.0:
            raise ValueError("Cohere timeout must be positive")
        self._api_key = api_key
        self._model = model
        self._client = client
        self._timeout_s = timeout_s

    async def score(self, query: str, documents: list[str]) -> list[float]:
        if not documents:
            return []
        if self._client is not None:
            return await self._request(self._client, query, documents)

        async with httpx.AsyncClient(timeout=self._timeout_s) as client:
            return await self._request(client, query, documents)

    async def _request(
        self, client: httpx.AsyncClient, query: str, documents: list[str]
    ) -> list[float]:
        response = await client.post(
            _RERANK_URL,
            headers={"Authorization": f"Bearer {self._api_key}"},
            json={
                "model": self._model,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
            },
        )
        response.raise_for_status()
        return self._parse_scores(response.json(), len(documents))

    @staticmethod
    def _parse_scores(payload: object, document_count: int) -> list[float]:
        if not isinstance(payload, dict):
            raise ValueError("Cohere rerank response must be an object")
        results = payload.get("results")
        if not isinstance(results, list):
            raise ValueError("Cohere rerank response must include a results list")

        scores: list[float | None] = [None] * document_count
        for result in results:
            if not isinstance(result, dict):
                raise ValueError("Cohere rerank result must be an object")
            index = result.get("index")
            relevance_score = result.get("relevance_score")
            if isinstance(index, bool) or not isinstance(index, int):
                raise ValueError("Cohere rerank result index must be an integer")
            if index < 0 or index >= document_count:
                raise ValueError("Cohere rerank result index is outside the input document range")
            if isinstance(relevance_score, bool) or not isinstance(relevance_score, (int, float)):
                raise ValueError("Cohere rerank relevance_score must be numeric")
            score = float(relevance_score)
            if not math.isfinite(score):
                raise ValueError("Cohere rerank relevance_score must be finite")
            if scores[index] is not None:
                raise ValueError("Cohere rerank response contains a duplicate document index")
            scores[index] = score

        if any(score is None for score in scores):
            raise ValueError("Cohere rerank response did not score every input document")
        return [cast(float, score) for score in scores]
