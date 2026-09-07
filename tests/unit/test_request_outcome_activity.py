"""Focused activity boundary checks for the selected request outcome link."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from events_concierge.domain.enums import LifecycleState
from events_concierge.domain.ids import registration_workflow_id
from events_concierge.domain.lifecycle import Lifecycle
from events_concierge.ports.consumer import ConsumerRequestOutcomeLinkStatus
from events_concierge.workflows import activities
from events_concierge.workflows.dto import LinkRequestOutcomeInput


class _ExactLifecycleRepository:
    def __init__(self, lifecycle: Lifecycle | None) -> None:
        self.lifecycle = lifecycle
        self.lookups: list[tuple[UUID, str]] = []

    async def find_by_workflow_id(
        self, tenant_id: UUID, workflow_id: str
    ) -> Lifecycle | None:
        self.lookups.append((tenant_id, workflow_id))
        return self.lifecycle


class _RecordingConsumerRepository:
    def __init__(self) -> None:
        self.links: list[tuple[UUID, UUID, UUID]] = []

    async def link_request_outcome(
        self, tenant_id: UUID, request_id: UUID, lifecycle_id: UUID
    ) -> ConsumerRequestOutcomeLinkStatus:
        self.links.append((tenant_id, request_id, lifecycle_id))
        return ConsumerRequestOutcomeLinkStatus.REPLAYED


async def test_link_activity_finds_the_exact_lifecycle_after_it_is_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, request_id, canonical_event_id = uuid4(), uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle = Lifecycle(
        lifecycle_id=uuid4(),
        tenant_id=tenant_id,
        canonical_event_id=canonical_event_id,
        workflow_id=workflow_id,
        state=LifecycleState.COMPLETED,
    )
    lifecycle_repository = _ExactLifecycleRepository(lifecycle)
    consumer_repository = _RecordingConsumerRepository()
    monkeypatch.setattr(
        activities,
        "_container",
        SimpleNamespace(
            lifecycle_repo=lifecycle_repository,
            consumer=consumer_repository,
        ),
    )

    result = await activities.link_request_outcome(
        LinkRequestOutcomeInput(
            tenant_id=str(tenant_id),
            request_id=str(request_id),
            canonical_event_id=str(canonical_event_id),
        )
    )

    assert result.status == "replayed"
    assert lifecycle_repository.lookups == [(tenant_id, workflow_id)]
    assert consumer_repository.links == [(tenant_id, request_id, lifecycle.lifecycle_id)]


async def test_link_activity_retries_an_outcome_reported_before_its_lifecycle_is_visible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id, request_id, canonical_event_id = uuid4(), uuid4(), uuid4()
    lifecycle_repository = _ExactLifecycleRepository(None)
    monkeypatch.setattr(
        activities,
        "_container",
        SimpleNamespace(
            lifecycle_repo=lifecycle_repository,
            consumer=_RecordingConsumerRepository(),
        ),
    )

    with pytest.raises(RuntimeError, match="not visible yet"):
        await activities.link_request_outcome(
            LinkRequestOutcomeInput(
                tenant_id=str(tenant_id),
                request_id=str(request_id),
                canonical_event_id=str(canonical_event_id),
            )
        )
