"""Tenant-safe PostgreSQL request-to-selected-lifecycle projection tests."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from events_concierge.adapters.postgres.consumer import PostgresConsumerReadRepository
from events_concierge.adapters.postgres.tenant_repos import (
    PostgresLifecycleRepository,
    PostgresRequestRepository,
    PostgresTenantRepository,
)
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import Lane, LifecycleState, Source
from events_concierge.domain.ids import registration_workflow_id
from events_concierge.domain.lifecycle import Lifecycle
from events_concierge.domain.request import EventRequest, RequestConstraints
from events_concierge.infra.db import system_session_scope, tenant_session_scope
from events_concierge.ports.consumer import (
    ConsumerRequestOutcomeConflictError,
    ConsumerRequestOutcomeLinkStatus,
)

pytestmark = pytest.mark.integration


async def _provision_tenant(tenant_id: UUID, label: str) -> None:
    await PostgresTenantRepository().add(
        Tenant(
            tenant_id=tenant_id,
            oidc_subject=f"oidc|request-outcome-{label}-{tenant_id}",
            notify_email=f"{label}-{tenant_id}@example.test",
            relay_inbox=f"{label}-{tenant_id}@u.example.test",
        )
    )


async def _seed_event(canonical_event_id: UUID, title: str) -> None:
    async with system_session_scope() as session:
        await session.execute(
            text(
                """INSERT INTO public.canonical_events (
                       canonical_event_id, title, start_at, description, price_status
                   )
                   VALUES (:canonical_event_id, :title, :start_at, :description, 'free')"""
            ),
            {
                "canonical_event_id": canonical_event_id,
                "title": title,
                "start_at": datetime(2099, 7, 22, 19, tzinfo=UTC),
                "description": f"{title} projection fixture",
            },
        )
        await session.execute(
            text(
                """INSERT INTO public.event_source_links (
                       source, source_event_id, canonical_event_id, registration_url, price_status
                   )
                   VALUES ('luma', :source_event_id, :canonical_event_id, :url, 'free')"""
            ),
            {
                "source_event_id": f"request-outcome-{canonical_event_id}",
                "canonical_event_id": canonical_event_id,
                "url": f"https://events.example.test/{canonical_event_id}",
            },
        )


async def _seed_request(tenant_id: UUID, label: str) -> EventRequest:
    request = EventRequest(
        request_id=uuid4(),
        tenant_id=tenant_id,
        raw_text=f"find {label}",
        constraints=RequestConstraints(categories=(label,)),
    )
    await PostgresRequestRepository().add(request)
    return request


async def _seed_lifecycle(
    tenant_id: UUID,
    label: str,
    target_state: LifecycleState,
) -> Lifecycle:
    canonical_event_id = uuid4()
    await _seed_event(canonical_event_id, f"{label} event")
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    repository = PostgresLifecycleRepository()
    lifecycle = await repository.get_or_create(tenant_id, canonical_event_id, workflow_id)
    lifecycle.lane = Lane.HANDOFF
    if target_state is LifecycleState.FOUND:
        return lifecycle
    if target_state is LifecycleState.FAILED_NO_CANDIDATE:
        await repository.transition(
            lifecycle,
            target_state,
            f"{workflow_id}:projection-failed",
            {"event_summary": f"{label} internal attempt", "notification_suppressed": True},
        )
        return lifecycle
    await repository.transition(
        lifecycle,
        LifecycleState.REGISTERED,
        f"{workflow_id}:projection-registered",
        {"event_summary": f"{label} event", "registration_source": Source.LUMA.value},
    )
    if target_state is LifecycleState.SCHEDULED:
        await repository.transition(
            lifecycle,
            LifecycleState.SCHEDULED,
            f"{workflow_id}:projection-scheduled",
            {"event_summary": f"{label} event"},
        )
    return lifecycle


async def _force_link_outcome(tenant_id: UUID, request_id: UUID, lifecycle_id: UUID) -> None:
    """Bypass the adapter to prove its read path still hides malformed historical links."""
    async with tenant_session_scope(tenant_id) as session:
        await session.execute(
            text(
                """INSERT INTO public.request_outcome_links
                       (tenant_id, request_id, lifecycle_id)
                   VALUES (:tenant_id, :request_id, :lifecycle_id)"""
            ),
            {
                "tenant_id": tenant_id,
                "request_id": request_id,
                "lifecycle_id": lifecycle_id,
            },
        )


async def test_request_projection_exposes_only_the_explicit_selected_lifecycle(db: None) -> None:
    owner, other = uuid4(), uuid4()
    await _provision_tenant(owner, "owner")
    await _provision_tenant(other, "other")
    linked_request = await _seed_request(owner, "linked jazz")
    unlinked_request = await _seed_request(owner, "unlinked theater")
    failed_request = await _seed_request(owner, "private failed attempt")
    premature_request = await _seed_request(owner, "premature attempt")
    other_request = await _seed_request(other, "other tenant")

    selected = await _seed_lifecycle(owner, "selected jazz", LifecycleState.SCHEDULED)
    conflicting = await _seed_lifecycle(owner, "conflicting pick", LifecycleState.REGISTERED)
    failed = await _seed_lifecycle(owner, "failed candidate", LifecycleState.FAILED_NO_CANDIDATE)
    premature = await _seed_lifecycle(owner, "premature candidate", LifecycleState.FOUND)
    other_selected = await _seed_lifecycle(other, "other selected", LifecycleState.REGISTERED)
    repository = PostgresConsumerReadRepository()
    assert (
        await repository.link_request_outcome(
            owner,
            linked_request.request_id,
            selected.lifecycle_id,
        )
        is ConsumerRequestOutcomeLinkStatus.LINKED
    )
    assert (
        await repository.link_request_outcome(
            owner,
            linked_request.request_id,
            selected.lifecycle_id,
        )
        is ConsumerRequestOutcomeLinkStatus.REPLAYED
    )
    with pytest.raises(ConsumerRequestOutcomeConflictError, match="different selected lifecycle"):
        await repository.link_request_outcome(
            owner,
            linked_request.request_id,
            conflicting.lifecycle_id,
        )
    # Defense in depth: even a premature/malformed link to a failed attempt stays invisible.
    await _force_link_outcome(owner, failed_request.request_id, failed.lifecycle_id)
    await _force_link_outcome(owner, premature_request.request_id, premature.lifecycle_id)
    await repository.link_request_outcome(
        other,
        other_request.request_id,
        other_selected.lifecycle_id,
    )

    owner_rows = await repository.list_requests(owner, offset=0, limit=10)
    other_rows = await repository.list_requests(other, offset=0, limit=10)
    owner_by_id = {row.request_id: row for row in owner_rows}

    assert set(owner_by_id) == {
        linked_request.request_id,
        unlinked_request.request_id,
        failed_request.request_id,
        premature_request.request_id,
    }
    assert owner_by_id[unlinked_request.request_id].outcome is None
    assert owner_by_id[failed_request.request_id].outcome is None
    assert owner_by_id[premature_request.request_id].outcome is None
    outcome = owner_by_id[linked_request.request_id].outcome
    assert outcome is not None
    assert outcome.canonical_event_id == selected.canonical_event_id
    assert outcome.title == "selected jazz event"
    assert outcome.state is LifecycleState.SCHEDULED
    assert outcome.lane is Lane.HANDOFF
    assert outcome.source is Source.LUMA
    assert not hasattr(outcome, "workflow_id")
    assert not hasattr(outcome, "completion_token")

    assert [row.request_id for row in other_rows] == [other_request.request_id]
    assert other_rows[0].outcome is not None
    assert other_rows[0].outcome.canonical_event_id == other_selected.canonical_event_id

    await PostgresLifecycleRepository().transition(
        selected,
        LifecycleState.COMPLETED,
        f"{selected.workflow_id}:projection-completed",
        {"event_summary": "selected jazz event"},
    )
    refreshed = await repository.list_requests(owner, offset=0, limit=10)
    refreshed_outcome = next(
        row.outcome for row in refreshed if row.request_id == linked_request.request_id
    )
    assert refreshed_outcome is not None
    assert refreshed_outcome.state is LifecycleState.COMPLETED
    assert (
        await repository.link_request_outcome(
            owner,
            linked_request.request_id,
            selected.lifecycle_id,
        )
        is ConsumerRequestOutcomeLinkStatus.REPLAYED
    )


async def test_request_outcome_link_rejects_cross_tenant_pairs(db: None) -> None:
    owner, other = uuid4(), uuid4()
    await _provision_tenant(owner, "cross-owner")
    await _provision_tenant(other, "cross-other")
    owner_request = await _seed_request(owner, "owner request")
    other_lifecycle = await _seed_lifecycle(other, "other lifecycle", LifecycleState.REGISTERED)

    repository = PostgresConsumerReadRepository()
    with pytest.raises(ValueError, match="tenant-visible request"):
        await repository.link_request_outcome(
            owner,
            owner_request.request_id,
            other_lifecycle.lifecycle_id,
        )
    with pytest.raises(IntegrityError):
        await _force_link_outcome(owner, owner_request.request_id, other_lifecycle.lifecycle_id)

    rows = await repository.list_requests(owner, offset=0, limit=10)
    assert len(rows) == 1
    assert rows[0].request_id == owner_request.request_id
    assert rows[0].outcome is None
