"""Request-workflow discovery preserves the intake feed's semantic retrieval inputs."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from events_concierge.composition import Container
from events_concierge.domain.request import EventRequest, Feed, RequestConstraints
from events_concierge.workflows import activities
from events_concierge.workflows.dto import RequestInput


class _RequestRepository:
    def __init__(self, request: EventRequest) -> None:
        self._request = request

    async def get(self, tenant_id: UUID, request_id: UUID) -> EventRequest | None:
        assert tenant_id == self._request.tenant_id
        assert request_id == self._request.request_id
        return self._request

    async def register_workflow_targets(
        self,
        tenant_id: UUID,
        workflow_ids: tuple[str, ...],
    ) -> None:
        del tenant_id, workflow_ids


class _Embedding:
    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.calls: list[list[str]] = []

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(texts)
        return [self.vector]


class _Feed:
    def __init__(self) -> None:
        self.request: EventRequest | None = None
        self.limit: int | None = None

    async def build_feed(self, request: EventRequest, *, limit: int) -> Feed:
        self.request = request
        self.limit = limit
        return Feed(request_id=request.request_id, items=())


@pytest.mark.asyncio
async def test_discovery_rehydrates_persisted_request_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workflow retrieval must not replace semantic RRF with earliest-row retrieval."""
    tenant_id, request_id = uuid4(), uuid4()
    request = EventRequest(
        request_id=request_id,
        tenant_id=tenant_id,
        raw_text="Find one free technology event in San Francisco",
        constraints=RequestConstraints(categories=("tech",), budget_free=True),
    )
    embedding = _Embedding([0.25, 0.75])
    feed = _Feed()
    container = cast(
        "Container",
        SimpleNamespace(
            request_repo=_RequestRepository(request),
            embedding=embedding,
            feed=feed,
        ),
    )
    monkeypatch.setattr(activities, "_container", container)

    result = await activities.discover_and_rank(
        RequestInput(tenant_id=str(tenant_id), request_id=str(request_id))
    )

    assert result.candidate_ids == []
    assert embedding.calls == [[request.raw_text]]
    assert feed.limit == 9
    assert feed.request is not request
    assert feed.request is not None
    assert feed.request.intent_embedding == [0.25, 0.75]
    assert request.intent_embedding is None
