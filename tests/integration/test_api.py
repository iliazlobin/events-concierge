"""API surface smoke test: health, onboarding, and the paginated feed (scroll) endpoint, driven
in-process over ASGI. Temporal is not required (intake degrades gracefully when it is absent)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from events_concierge.adapters.mock.notifier import MockNotifier
from events_concierge.adapters.postgres.tenant_repos import (
    PostgresHandoffRepository,
    PostgresLifecycleRepository,
)
from events_concierge.api.app import create_app
from events_concierge.application.outbox import OutboxRelay
from events_concierge.domain.enums import (
    HandoffReason,
    HandoffState,
    LifecycleState,
    Source,
)
from events_concierge.domain.ids import registration_workflow_id
from events_concierge.domain.lifecycle import HandoffTask
from events_concierge.infra.db import system_session_scope
from events_concierge.ports.notifications import NotificationKind

pytestmark = pytest.mark.integration


class RecordingStarter:
    """Port-faithful Temporal starter seam that records exactly one parent effect per workflow ID."""

    def __init__(self) -> None:
        self.effects: set[tuple[UUID, UUID]] = set()

    async def start(self, tenant_id: UUID, request_id: UUID) -> None:
        self.effects.add((tenant_id, request_id))


class RecordingLifecycleSignaler:
    """Port-faithful Temporal signal seam for the un-RSVP API contract."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.handoff_completions: list[tuple[str, str, str]] = []

    async def signal_unrsvp(self, workflow_id: str, request_id: str) -> None:
        self.commands.append((workflow_id, request_id))

    async def signal_handoff_completed(
        self,
        workflow_id: str,
        task_id: str,
        completion_id: str,
    ) -> None:
        self.handoff_completions.append((workflow_id, task_id, completion_id))


def _auth_headers(tenant_id: UUID | str) -> dict[str, str]:
    """The offline adapter's explicit request-context seam; production injects an OIDC BFF."""
    return {"X-EC-Tenant-ID": str(tenant_id)}


async def test_api_health_onboard_and_feed(db: None) -> None:
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            assert (await client.get("/healthz")).json() == {"status": "ok"}

            onboard = await client.post("/v1/onboard", json={"notify_email": "user@example.com"})
            assert onboard.status_code == 201
            tenant_id = onboard.json()["tenant_id"]
            assert onboard.json()["relay_inbox"].endswith("@u.concierge.test")

            feed = await client.post(
                "/v1/feed",
                json={"text": "jazz or a tech meetup"},
                headers=_auth_headers(tenant_id),
            )
            assert feed.status_code == 200
            body = feed.json()
            assert "items" in body and "next_cursor" in body
            for item in body["items"]:
                assert {
                    "canonical_event_id",
                    "title",
                    "price_status",
                    "score",
                    "lanes",
                    "conflict",
                } <= item.keys()

            # Inbound contracts validate and fail truthfully if no tenant-scoped lifecycle exists.
            assert (
                await client.post(
                    "/v1/unrsvp",
                    json={"canonical_event_id": str(uuid4())},
                    headers=_auth_headers(tenant_id),
                )
            ).status_code == 404
            assert (await client.post("/v1/tasks/tok123/done")).status_code == 404
            assert (
                await client.post("/v1/unrsvp", json={}, headers=_auth_headers(tenant_id))
            ).status_code == 422
            assert (await client.post("/v1/feed", json={"text": "jazz"})).status_code == 401


async def test_mark_done_uses_one_time_capability_and_signals_exact_retained_workflow(
    db: None,
) -> None:
    """No tenant/body identity is accepted; retries converge on one stable workflow command."""
    tenant_id, canonical_event_id = uuid4(), uuid4()
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    token = secrets.token_urlsafe(32)
    lifecycle_repo = PostgresLifecycleRepository()
    handoff_repo = PostgresHandoffRepository()
    lifecycle = await lifecycle_repo.get_or_create(tenant_id, canonical_event_id, workflow_id)
    task = HandoffTask(
        task_id=f"{workflow_id}:handoff",
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        canonical_event_id=canonical_event_id,
        reason=HandoffReason.DEFERRED_REGISTER,
        deep_link="https://example.test/register",
        event_summary="Capability fixture",
        ttl_expires_at=datetime.now(UTC) + timedelta(days=1),
        state=HandoffState.OPEN,
        completion_token=token,
    )
    await handoff_repo.create_and_transition(
        task,
        lifecycle,
        LifecycleState.HANDOFF,
        f"{workflow_id}:handoff:1",
        {
            "task_id": task.task_id,
            "workflow_id": workflow_id,
            "event_summary": task.event_summary,
            "deep_link": task.deep_link,
            "completion_url": f"/v1/tasks/{token}/done",
        },
    )

    app = create_app()
    signaler = RecordingLifecycleSignaler()
    async with app.router.lifespan_context(app):
        app.state.lifecycle_signaler = signaler
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            landing = await client.get(f"/v1/tasks/{token}/done")
            assert signaler.handoff_completions == []
            accepted = await client.post(f"/v1/tasks/{token}/done")
            replay_before_workflow = await client.post(f"/v1/tasks/{token}/done")
            unknown = await client.post(f"/v1/tasks/{secrets.token_urlsafe(32)}/done")

            completion_id = f"{workflow_id}:handoff-completion:{task.task_id}:1"
            await handoff_repo.record_completion_review(
                task,
                completion_id=completion_id,
                detail="independent registration verification returned not_present",
            )
            review_notifier = MockNotifier()
            review_relay = OutboxRelay(
                app.state.container.outbox_repo,
                review_notifier,
            )
            for _ in range(100):
                relay_stats = await review_relay.relay_once(limit=100)
                if any(
                    item.kind is NotificationKind.HANDOFF_REVIEW_REQUIRED
                    and item.tenant_id == tenant_id
                    for item in review_notifier.sent
                ):
                    break
                if relay_stats.claimed == 0:
                    break
            replay_after_consumption = await client.post(f"/v1/tasks/{token}/done")
            used_landing = await client.get(f"/v1/tasks/{token}/done")

    assert landing.status_code == 200
    assert 'form method="post"' in landing.text
    assert landing.headers["cache-control"] == "no-store, max-age=0"
    assert landing.headers["referrer-policy"] == "no-referrer"
    assert accepted.status_code == 202
    assert accepted.json() == {"status": "accepted"}
    assert replay_before_workflow.status_code == 202
    assert unknown.status_code == 404
    assert replay_after_consumption.status_code == 202
    assert replay_after_consumption.json() == {"status": "already_accepted"}
    assert used_landing.status_code == 200
    assert "Completion already submitted" in used_landing.text
    assert 'form method="post"' not in used_landing.text
    assert signaler.handoff_completions == [
        (workflow_id, task.task_id, completion_id),
        (workflow_id, task.task_id, completion_id),
    ]
    review_notifications = [
        item
        for item in review_notifier.sent
        if item.kind is NotificationKind.HANDOFF_REVIEW_REQUIRED and item.tenant_id == tenant_id
    ]
    assert len(review_notifications) == 1, [
        (item.kind.value, item.subject) for item in review_notifier.sent
    ]
    assert "no calendar entry was added" in review_notifications[0].body


async def test_api_request_replay_queues_and_starts_one_parent_workflow(db: None) -> None:
    """HTTP retries normalize to one persisted start instruction and one parent effect (AC-48)."""
    app = create_app()
    starter = RecordingStarter()
    async with app.router.lifespan_context(app):
        app.state.request_starter = starter
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            onboard = await client.post("/v1/onboard", json={"notify_email": "request@example.com"})
            tenant_id = onboard.json()["tenant_id"]
            first = await client.post(
                "/v1/requests",
                json={"text": "  Jazz   after work "},
                headers=_auth_headers(tenant_id),
            )
            replay = await client.post(
                "/v1/requests",
                json={"text": "jazz after work"},
                headers=_auth_headers(tenant_id),
            )

    assert first.status_code == 200
    assert replay.status_code == 200
    assert first.json()["request_id"] == replay.json()["request_id"]
    assert first.json()["workflow_id"] == replay.json()["workflow_id"]
    assert first.json()["workflow_started"] is True
    assert replay.json()["workflow_started"] is True
    assert len(starter.effects) == 1


async def test_api_tenant_comes_only_from_auth_context_and_rejects_body_spoofing(db: None) -> None:
    """A caller cannot select another tenant through JSON, even in the offline auth seam (FR-1.1/1.3)."""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            owner = (
                await client.post("/v1/onboard", json={"notify_email": "owner@example.com"})
            ).json()["tenant_id"]
            attacker = (
                await client.post("/v1/onboard", json={"notify_email": "attacker@example.com"})
            ).json()["tenant_id"]
            spoofed = await client.post(
                "/v1/requests",
                json={"tenant_id": attacker, "text": "this tenant field must be rejected"},
                headers=_auth_headers(owner),
            )
            accepted = await client.post(
                "/v1/requests",
                json={"text": "find jazz for the authenticated owner"},
                headers=_auth_headers(owner),
            )

        request_id = UUID(accepted.json()["request_id"])
        owner_request = await app.state.container.request_repo.get(UUID(owner), request_id)
        attacker_request = await app.state.container.request_repo.get(UUID(attacker), request_id)

    assert spoofed.status_code == 422
    assert accepted.status_code == 200
    assert owner_request is not None
    assert attacker_request is None


async def test_api_feedback_is_authenticated_replay_safe_and_server_controlled(db: None) -> None:
    """Feedback cannot select a tenant or score, and an at-least-once retry stays one receipt."""
    app = create_app()
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            owner = UUID(
                (
                    await client.post(
                        "/v1/onboard", json={"notify_email": "feedback-owner@example.com"}
                    )
                ).json()["tenant_id"]
            )
            attacker = UUID(
                (
                    await client.post(
                        "/v1/onboard", json={"notify_email": "feedback-attacker@example.com"}
                    )
                ).json()["tenant_id"]
            )
            canonical_event_id = uuid4()
            await _seed_registered_source(canonical_event_id)
            signal_id = uuid4()
            body = {
                "signal_id": str(signal_id),
                "canonical_event_id": str(canonical_event_id),
                "kind": "click",
            }

            first = await client.post(
                "/v1/feed-feedback", json=body, headers=_auth_headers(owner)
            )
            replay = await client.post(
                "/v1/feed-feedback", json=body, headers=_auth_headers(owner)
            )
            conflict = await client.post(
                "/v1/feed-feedback",
                json={**body, "kind": "dismiss"},
                headers=_auth_headers(owner),
            )
            spoofed = await client.post(
                "/v1/feed-feedback",
                json={**body, "tenant_id": str(attacker)},
                headers=_auth_headers(owner),
            )
            client_weight = await client.post(
                "/v1/feed-feedback",
                json={**body, "weight": 999},
                headers=_auth_headers(owner),
            )
            missing_event = await client.post(
                "/v1/feed-feedback",
                json={
                    "signal_id": str(uuid4()),
                    "canonical_event_id": str(uuid4()),
                    "kind": "dwell",
                },
                headers=_auth_headers(owner),
            )
            unauthenticated = await client.post("/v1/feed-feedback", json=body)

        owner_affinities = await app.state.container.ranking_feedback_repo.get_implicit_affinities(
            owner
        )
        attacker_affinities = (
            await app.state.container.ranking_feedback_repo.get_implicit_affinities(attacker)
        )

    assert first.status_code == 202
    assert first.json() == {"status": "recorded"}
    assert replay.status_code == 202
    assert replay.json() == {"status": "replayed"}
    assert conflict.status_code == 409
    assert spoofed.status_code == 422
    assert client_weight.status_code == 422
    assert missing_event.status_code == 404
    assert unauthenticated.status_code == 401
    assert owner_affinities
    assert attacker_affinities == {}


async def test_api_unrsvp_signals_the_actual_tenant_scoped_lifecycle(db: None) -> None:
    """The API never guesses a workflow id or reports an undurable command as accepted (FR-6.7/8.8)."""
    app = create_app()
    signaler = RecordingLifecycleSignaler()
    async with app.router.lifespan_context(app):
        app.state.lifecycle_signaler = signaler
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            onboard = await client.post("/v1/onboard", json={"notify_email": "unrsvp@example.com"})
            tenant_id = UUID(onboard.json()["tenant_id"])
            canonical_event_id = uuid4()
            workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
            await _seed_registered_source(canonical_event_id)
            lifecycle = await app.state.container.lifecycle_repo.get_or_create(
                tenant_id, canonical_event_id, workflow_id
            )
            await app.state.container.lifecycle_repo.transition(
                lifecycle,
                LifecycleState.REGISTERED,
                f"{workflow_id}:registered",
                {
                    "workflow_id": workflow_id,
                    "event_summary": "API fixture",
                    "registration_source": Source.LUMA.value,
                },
            )
            await app.state.container.lifecycle_repo.transition(
                lifecycle,
                LifecycleState.SCHEDULED,
                f"{workflow_id}:scheduled",
                {"workflow_id": workflow_id, "event_summary": "API fixture"},
            )
            request_id = uuid4()
            other_tenant = (
                await client.post("/v1/onboard", json={"notify_email": "other@example.com"})
            ).json()["tenant_id"]
            cross_tenant = await client.post(
                "/v1/unrsvp",
                json={
                    "canonical_event_id": str(canonical_event_id),
                    "request_id": str(uuid4()),
                },
                headers=_auth_headers(other_tenant),
            )
            response = await client.post(
                "/v1/unrsvp",
                json={
                    "canonical_event_id": str(canonical_event_id),
                    "request_id": str(request_id),
                },
                headers=_auth_headers(tenant_id),
            )

    assert cross_tenant.status_code == 404
    assert response.status_code == 202
    assert response.json()["workflow_id"] == workflow_id
    assert response.json()["request_id"] == str(request_id)
    assert signaler.commands == [(workflow_id, str(request_id))]


async def test_api_unrsvp_rejects_a_prebooking_lifecycle_without_signalling(db: None) -> None:
    """A withdrawal cannot overtake the calendar stage of an in-flight registration (FR-8.8)."""
    app = create_app()
    signaler = RecordingLifecycleSignaler()
    async with app.router.lifespan_context(app):
        app.state.lifecycle_signaler = signaler
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            onboard = await client.post("/v1/onboard", json={"notify_email": "early@example.com"})
            tenant_id = UUID(onboard.json()["tenant_id"])
            canonical_event_id = uuid4()
            workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
            await _seed_registered_source(canonical_event_id)
            lifecycle = await app.state.container.lifecycle_repo.get_or_create(
                tenant_id, canonical_event_id, workflow_id
            )
            await app.state.container.lifecycle_repo.transition(
                lifecycle,
                LifecycleState.REGISTERED,
                f"{workflow_id}:registered",
                {
                    "workflow_id": workflow_id,
                    "event_summary": "Prebooking fixture",
                    "registration_source": Source.LUMA.value,
                },
            )
            response = await client.post(
                "/v1/unrsvp",
                json={
                    "canonical_event_id": str(canonical_event_id),
                    "request_id": str(uuid4()),
                },
                headers=_auth_headers(tenant_id),
            )

    assert response.status_code == 409
    assert signaler.commands == []


async def _seed_registered_source(canonical_event_id: UUID) -> None:
    """Create the source-linked public event required by a guarded REGISTERED transition."""
    async with system_session_scope() as session:
        await session.execute(
            text(
                """
                INSERT INTO canonical_events
                    (canonical_event_id, title, start_at, description, price_status)
                VALUES (:canonical_event_id, :title, :start_at, :description, 'free')
                """
            ),
            {
                "canonical_event_id": canonical_event_id,
                "title": "API registered lifecycle fixture",
                "start_at": datetime(2099, 1, 1, tzinfo=UTC),
                "description": "source-bound lifecycle fixture",
            },
        )
        await session.execute(
            text(
                """
                INSERT INTO event_source_links
                    (source, source_event_id, canonical_event_id, registration_url, price_status)
                VALUES (:source, :source_event_id, :canonical_event_id, :registration_url, 'free')
                """
            ),
            {
                "source": Source.LUMA.value,
                "source_event_id": f"api-luma-{canonical_event_id}",
                "canonical_event_id": canonical_event_id,
                "registration_url": f"https://luma.test/{canonical_event_id}",
            },
        )
