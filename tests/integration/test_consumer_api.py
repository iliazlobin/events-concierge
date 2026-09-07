"""Authenticated consumer read, preference, and handoff command contracts."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from events_concierge.api.app import create_app
from events_concierge.domain.enums import HandoffReason, Lane, LifecycleState, Source
from events_concierge.domain.ids import registration_workflow_id
from events_concierge.domain.lifecycle import HandoffTask
from events_concierge.domain.request import EventRequest, RequestConstraints, TimeWindow
from events_concierge.infra.db import system_session_scope
from events_concierge.ports.auth import CsrfVerificationFailedError
from events_concierge.ports.ranking import RankingProfileUpdate, UserRankingProfile

pytestmark = pytest.mark.integration


class RecordingLifecycleSignaler:
    """Record the exact retained workflow command selected by authenticated task ingress."""

    def __init__(self) -> None:
        self.handoff_completions: list[tuple[str, str, str]] = []

    async def signal_handoff_completed(
        self,
        workflow_id: str,
        task_id: str,
        completion_id: str,
    ) -> None:
        self.handoff_completions.append((workflow_id, task_id, completion_id))


class HeaderBoundCsrfProtection:
    """Production-shaped test verifier bound to one explicit request header."""

    def __init__(self, expected_token: str) -> None:
        self.expected_token = expected_token
        self.calls: list[tuple[UUID, str | None]] = []

    async def verify_state_change(
        self,
        tenant_id: UUID,
        headers: Mapping[str, str],
    ) -> None:
        token = headers.get("x-test-csrf")
        self.calls.append((tenant_id, token))
        if token != self.expected_token:
            raise CsrfVerificationFailedError("invalid test CSRF evidence")


def _auth_headers(tenant_id: UUID | str) -> dict[str, str]:
    return {"X-EC-Tenant-ID": str(tenant_id)}


async def _onboard(client: AsyncClient, label: str) -> UUID:
    response = await client.post("/v1/onboard", json={"notify_email": f"{label}@example.test"})
    assert response.status_code == 201
    return UUID(response.json()["tenant_id"])


async def _seed_event(canonical_event_id: UUID, label: str) -> None:
    async with system_session_scope() as session:
        await session.execute(
            text(
                """INSERT INTO public.canonical_events (
                       canonical_event_id, title, start_at, end_at, venue_name, city_norm,
                       description, price_status
                   )
                   VALUES (
                       :canonical_event_id, :title, :start_at, :end_at, :venue_name, :city,
                       :description, 'free'
                   )"""
            ),
            {
                "canonical_event_id": canonical_event_id,
                "title": f"{label} event",
                "start_at": datetime(2099, 6, 1, 18, tzinfo=UTC),
                "end_at": datetime(2099, 6, 1, 20, tzinfo=UTC),
                "venue_name": f"{label} hall",
                "city": "san francisco",
                "description": f"{label} consumer projection",
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
                "source_event_id": f"consumer-{label}-{canonical_event_id}",
                "canonical_event_id": canonical_event_id,
                "url": f"https://events.example.test/{canonical_event_id}",
            },
        )


async def _seed_scheduled_registration(container: Any, tenant_id: UUID, label: str) -> UUID:
    canonical_event_id = uuid4()
    await _seed_event(canonical_event_id, label)
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle = await container.lifecycle_repo.get_or_create(
        tenant_id,
        canonical_event_id,
        workflow_id,
    )
    lifecycle.lane = Lane.HANDOFF
    await container.lifecycle_repo.transition(
        lifecycle,
        LifecycleState.REGISTERED,
        f"{workflow_id}:consumer-registered",
        {
            "event_summary": f"{label} event",
            "registration_source": Source.LUMA.value,
        },
    )
    await container.lifecycle_repo.transition(
        lifecycle,
        LifecycleState.SCHEDULED,
        f"{workflow_id}:consumer-scheduled",
        {"event_summary": f"{label} event"},
    )
    return canonical_event_id


async def _seed_newer_competing_source(canonical_event_id: UUID) -> None:
    """Add a newer link that must not replace a lifecycle's chosen registration provider."""
    async with system_session_scope() as session:
        await session.execute(
            text(
                """INSERT INTO public.event_source_links (
                       source, source_event_id, canonical_event_id, registration_url,
                       last_seen_at, price_status
                   )
                   VALUES (
                       'meetup', :source_event_id, :canonical_event_id, :url,
                       clock_timestamp(), 'free'
                   )"""
            ),
            {
                "source_event_id": f"consumer-competing-{canonical_event_id}",
                "canonical_event_id": canonical_event_id,
                "url": f"https://meetup.example.test/{canonical_event_id}",
            },
        )


async def _remove_source_link(canonical_event_id: UUID, source: Source) -> None:
    """Simulate a selected provider link disappearing while another provider remains."""
    async with system_session_scope() as session:
        await session.execute(
            text(
                """DELETE FROM public.event_source_links
                   WHERE canonical_event_id = :canonical_event_id
                     AND source = :source"""
            ),
            {"canonical_event_id": canonical_event_id, "source": source.value},
        )


async def _seed_registration_with_missing_selected_link(
    container: Any,
    tenant_id: UUID,
) -> UUID:
    canonical_event_id = await _seed_scheduled_registration(
        container,
        tenant_id,
        "owner-missing-source",
    )
    await _seed_newer_competing_source(canonical_event_id)
    await _remove_source_link(canonical_event_id, Source.LUMA)
    return canonical_event_id


async def _seed_handoff(
    container: Any,
    tenant_id: UUID,
    label: str,
    *,
    expires_at: datetime | None = None,
) -> HandoffTask:
    canonical_event_id = uuid4()
    await _seed_event(canonical_event_id, label)
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle = await container.lifecycle_repo.get_or_create(
        tenant_id,
        canonical_event_id,
        workflow_id,
    )
    lifecycle.lane = Lane.HANDOFF
    task = HandoffTask(
        task_id=f"task-{uuid4().hex}",
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        canonical_event_id=canonical_event_id,
        reason=HandoffReason.DEFERRED_REGISTER,
        deep_link=f"https://events.example.test/{canonical_event_id}/register",
        event_summary=f"{label} handoff",
        ttl_expires_at=expires_at or datetime.now(UTC) + timedelta(days=7),
    )
    await container.handoff_repo.create_and_transition(
        task,
        lifecycle,
        LifecycleState.HANDOFF,
        f"{workflow_id}:consumer-handoff",
        {"event_summary": task.event_summary, "task_id": task.task_id},
    )
    return task


async def _seed_failed_candidate(container: Any, tenant_id: UUID, label: str) -> UUID:
    """Create an internal attempted-candidate terminal that must not become a consumer plan."""
    canonical_event_id = uuid4()
    await _seed_event(canonical_event_id, label)
    workflow_id = registration_workflow_id(tenant_id, canonical_event_id)
    lifecycle = await container.lifecycle_repo.get_or_create(
        tenant_id,
        canonical_event_id,
        workflow_id,
    )
    await container.lifecycle_repo.transition(
        lifecycle,
        LifecycleState.FAILED_NO_CANDIDATE,
        f"{workflow_id}:consumer-no-candidate",
        {
            "event_summary": f"{label} internal candidate",
            "notification_suppressed": True,
        },
    )
    return canonical_event_id


def _assert_request_history(
    owner_requests: Any,
    owner_first_page: Any,
    owner_second_page: Any,
    other_requests: Any,
    owner_request_ids: set[UUID],
    linked_request_id: UUID,
    owner_registration: UUID,
    other_request_id: UUID,
    window: TimeWindow,
) -> None:
    """Keep the request/outcome contract readable without inflating the journey fixture."""
    assert owner_requests.status_code == 200
    assert {UUID(item["request_id"]) for item in owner_requests.json()["items"]} == (
        owner_request_ids
    )
    assert all(
        item["window_start"] == window.start.isoformat() for item in owner_requests.json()["items"]
    )
    assert all(
        item["window_end"] == window.end.isoformat() for item in owner_requests.json()["items"]
    )
    requests_by_id = {
        UUID(item["request_id"]): item for item in owner_requests.json()["items"]
    }
    selected_outcome = requests_by_id[linked_request_id]["outcome"]
    assert selected_outcome["canonical_event_id"] == str(owner_registration)
    assert selected_outcome["title"] == "owner event"
    assert selected_outcome["start_at"] == datetime(
        2099, 6, 1, 18, tzinfo=UTC
    ).isoformat()
    assert selected_outcome["state"] == "scheduled"
    assert selected_outcome["lane"] == "handoff"
    assert selected_outcome["source"] == "luma"
    assert datetime.fromisoformat(selected_outcome["updated_at"]).tzinfo is not None
    assert all(
        item["outcome"] is None
        for request_id, item in requests_by_id.items()
        if request_id != linked_request_id
    )
    assert owner_first_page.json()["next_cursor"] == "1"
    assert owner_second_page.json()["next_cursor"] is None
    assert {
        UUID(owner_first_page.json()["items"][0]["request_id"]),
        UUID(owner_second_page.json()["items"][0]["request_id"]),
    } == owner_request_ids
    assert [UUID(item["request_id"]) for item in other_requests.json()["items"]] == [
        other_request_id
    ]


async def test_consumer_read_routes_require_auth_and_isolate_each_tenants_rows(db: None) -> None:
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        owner = await _onboard(client, f"consumer-owner-{uuid4().hex}")
        other = await _onboard(client, f"consumer-other-{uuid4().hex}")
        window = TimeWindow(
            datetime(2099, 5, 1, tzinfo=UTC),
            datetime(2099, 7, 1, tzinfo=UTC),
        )
        owner_request_ids = {uuid4(), uuid4()}
        for number, request_id in enumerate(owner_request_ids, start=1):
            await app.state.container.request_repo.add(
                EventRequest(
                    request_id=request_id,
                    tenant_id=owner,
                    raw_text=f"owner request {number}",
                    constraints=RequestConstraints(
                        time_window=window,
                        categories=("jazz",),
                        budget_free=True,
                    ),
                )
            )
        other_request_id = uuid4()
        await app.state.container.request_repo.add(
            EventRequest(
                request_id=other_request_id,
                tenant_id=other,
                raw_text="other tenant request",
                constraints=RequestConstraints(categories=("hiking",)),
            )
        )
        owner_registration = await _seed_scheduled_registration(app.state.container, owner, "owner")
        selected_lifecycle = await app.state.container.lifecycle_repo.find_active(
            owner, owner_registration
        )
        assert selected_lifecycle is not None
        linked_request_id = min(owner_request_ids, key=str)
        await app.state.container.consumer.link_request_outcome(
            owner,
            linked_request_id,
            selected_lifecycle.lifecycle_id,
        )
        await _seed_newer_competing_source(owner_registration)
        owner_missing_source_registration = await _seed_registration_with_missing_selected_link(
            app.state.container,
            owner,
        )
        other_registration = await _seed_scheduled_registration(app.state.container, other, "other")
        owner_task = await _seed_handoff(app.state.container, owner, "owner")
        other_task = await _seed_handoff(app.state.container, other, "other")
        owner_failed_candidate = await _seed_failed_candidate(
            app.state.container,
            owner,
            "owner-failed",
        )

        for path in ("/v1/me", "/v1/requests", "/v1/registrations", "/v1/tasks"):
            assert (await client.get(path)).status_code == 401

        owner_requests = await client.get("/v1/requests", headers=_auth_headers(owner))
        owner_first_page = await client.get(
            "/v1/requests?limit=1",
            headers=_auth_headers(owner),
        )
        owner_second_page = await client.get(
            f"/v1/requests?limit=1&cursor={owner_first_page.json()['next_cursor']}",
            headers=_auth_headers(owner),
        )
        other_requests = await client.get("/v1/requests", headers=_auth_headers(other))
        owner_registrations = await client.get("/v1/registrations", headers=_auth_headers(owner))
        other_registrations = await client.get("/v1/registrations", headers=_auth_headers(other))
        owner_tasks = await client.get("/v1/tasks", headers=_auth_headers(owner))
        other_tasks = await client.get("/v1/tasks", headers=_auth_headers(other))

    _assert_request_history(
        owner_requests,
        owner_first_page,
        owner_second_page,
        other_requests,
        owner_request_ids,
        linked_request_id,
        owner_registration,
        other_request_id,
        window,
    )

    assert {UUID(item["canonical_event_id"]) for item in owner_registrations.json()["items"]} == {
        owner_registration,
        owner_missing_source_registration,
        owner_task.canonical_event_id,
    }
    assert owner_failed_candidate not in {
        UUID(item["canonical_event_id"]) for item in owner_registrations.json()["items"]
    }
    assert other_registration in {
        UUID(item["canonical_event_id"]) for item in other_registrations.json()["items"]
    }
    assert owner_registration not in {
        UUID(item["canonical_event_id"]) for item in other_registrations.json()["items"]
    }
    scheduled = next(
        item
        for item in owner_registrations.json()["items"]
        if UUID(item["canonical_event_id"]) == owner_registration
    )
    assert scheduled["can_withdraw"] is True
    assert scheduled["source"] == "luma"
    assert scheduled["registration_url"] == (f"https://events.example.test/{owner_registration}")
    assert "workflow_id" not in scheduled
    missing_source_link = next(
        item
        for item in owner_registrations.json()["items"]
        if UUID(item["canonical_event_id"]) == owner_missing_source_registration
    )
    assert missing_source_link["source"] == "luma"
    assert missing_source_link["registration_url"] is None

    assert [item["task_id"] for item in owner_tasks.json()["items"]] == [owner_task.task_id]
    assert [item["task_id"] for item in other_tasks.json()["items"]] == [other_task.task_id]
    assert "workflow_id" not in owner_tasks.json()["items"][0]
    assert "metadata" not in owner_tasks.json()["items"][0]
    assert "completion_token" not in owner_tasks.json()["items"][0]


async def test_unprovisioned_authenticated_claim_cannot_create_orphan_consumer_state(
    db: None,
) -> None:
    app = create_app()
    tenant_id = uuid4()
    headers = _auth_headers(tenant_id)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        request = await client.post(
            "/v1/requests",
            json={"text": "an orphan request must never be accepted"},
            headers=headers,
        )
        feed = await client.post(
            "/v1/feed",
            json={"text": "an unprovisioned account must not browse"},
            headers=headers,
        )
        feedback = await client.post(
            "/v1/feed-feedback",
            json={
                "signal_id": str(uuid4()),
                "canonical_event_id": str(uuid4()),
                "kind": "click",
            },
            headers=headers,
        )
        history = await client.get("/v1/requests", headers=headers)

        async with system_session_scope() as session:
            orphan_requests = (
                await session.execute(
                    text(
                        """SELECT count(*)
                           FROM public.event_requests
                           WHERE tenant_id = :tenant_id"""
                    ),
                    {"tenant_id": tenant_id},
                )
            ).scalar_one()
            orphan_feedback = (
                await session.execute(
                    text(
                        """SELECT count(*)
                           FROM public.tenant_ranking_feedback_receipts
                           WHERE tenant_id = :tenant_id"""
                    ),
                    {"tenant_id": tenant_id},
                )
            ).scalar_one()

    for response in (request, feed, feedback, history):
        assert response.status_code == 404
        assert response.json() == {"detail": "account not found"}
    assert orphan_requests == 0
    assert orphan_feedback == 0


async def test_preference_update_is_revisioned_preserves_implicit_state_and_is_tenant_scoped(
    db: None,
) -> None:
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        owner = await _onboard(client, f"preferences-owner-{uuid4().hex}")
        other = await _onboard(client, f"preferences-other-{uuid4().hex}")
        await app.state.container.ranking_profiles.replace_profile(
            owner,
            RankingProfileUpdate(
                profile=UserRankingProfile(
                    explicit_affinities={"old": 1.0},
                    implicit_affinities={"learned:jazz": 0.75},
                ),
                revision=1,
            ),
        )

        unauthenticated = await client.put(
            "/v1/preferences", json={"interests": ["jazz"], "revision": 2}
        )
        applied = await client.put(
            "/v1/preferences",
            json={"interests": ["  Live   Music ", "JAZZ"], "revision": 2},
            headers=_auth_headers(owner),
        )
        replayed = await client.put(
            "/v1/preferences",
            json={"interests": ["live music", "jazz"], "revision": 2},
            headers=_auth_headers(owner),
        )
        conflicted = await client.put(
            "/v1/preferences",
            json={"interests": ["hiking"], "revision": 2},
            headers=_auth_headers(owner),
        )
        invalid = await client.put(
            "/v1/preferences",
            json={"interests": ["Jazz", " jazz "], "revision": 3},
            headers=_auth_headers(owner),
        )
        owner_me = await client.get("/v1/me", headers=_auth_headers(owner))
        other_me = await client.get("/v1/me", headers=_auth_headers(other))
        missing_me = await client.get("/v1/me", headers=_auth_headers(uuid4()))
        stored = await app.state.container.ranking_profiles.get_profile(owner)

    assert unauthenticated.status_code == 401
    assert applied.status_code == 200
    assert applied.json() == {
        "status": "applied",
        "interests": ["jazz", "live music"],
        "revision": 2,
    }
    assert replayed.status_code == 200
    assert replayed.json()["status"] == "replayed"
    assert conflicted.status_code == 409
    assert conflicted.json() == {"detail": "preference revision conflicts"}
    assert invalid.status_code == 422
    assert owner_me.json()["interests"] == ["jazz", "live music"]
    assert owner_me.json()["preference_revision"] == 2
    assert other_me.json()["interests"] == []
    assert other_me.json()["preference_revision"] == 0
    assert missing_me.status_code == 404
    assert dict(stored.explicit_affinities) == {"jazz": 1.0, "live music": 1.0}
    assert dict(stored.implicit_affinities) == {"learned:jazz": 0.75}


async def test_authenticated_task_completion_is_tenant_bound_replay_safe_and_fail_closed(
    db: None,
) -> None:
    app = create_app()
    signaler = RecordingLifecycleSignaler()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        app.state.lifecycle_signaler = signaler
        owner = await _onboard(client, f"task-owner-{uuid4().hex}")
        other = await _onboard(client, f"task-other-{uuid4().hex}")
        task = await _seed_handoff(app.state.container, owner, "completion")
        expired_task = await _seed_handoff(
            app.state.container,
            owner,
            "expired-completion",
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )

        actionable_tasks = await client.get("/v1/tasks", headers=_auth_headers(owner))
        all_tasks = await client.get("/v1/tasks?state=all", headers=_auth_headers(owner))

        unauthenticated = await client.post(f"/v1/me/tasks/{task.task_id}/done")
        cross_tenant = await client.post(
            f"/v1/me/tasks/{task.task_id}/done",
            headers=_auth_headers(other),
        )
        malformed = await client.post(
            f"/v1/me/tasks/{'x' * 513}/done",
            headers=_auth_headers(owner),
        )
        expired = await client.post(
            f"/v1/me/tasks/{expired_task.task_id}/done",
            headers=_auth_headers(owner),
        )
        accepted = await client.post(
            f"/v1/me/tasks/{task.task_id}/done",
            headers=_auth_headers(owner),
        )
        replayed = await client.post(
            f"/v1/me/tasks/{task.task_id}/done",
            headers=_auth_headers(owner),
        )
        app.state.lifecycle_signaler = None
        unavailable = await client.post(
            f"/v1/me/tasks/{task.task_id}/done",
            headers=_auth_headers(owner),
        )

    completion_id = f"{task.workflow_id}:handoff-completion:{task.task_id}:1"
    assert [item["task_id"] for item in actionable_tasks.json()["items"]] == [task.task_id]
    assert {item["task_id"] for item in all_tasks.json()["items"]} == {
        task.task_id,
        expired_task.task_id,
    }
    assert unauthenticated.status_code == 401
    assert cross_tenant.status_code == 404
    assert malformed.status_code == 404
    assert expired.status_code == 410
    assert expired.json() == {"detail": "handoff task is no longer active"}
    assert accepted.status_code == 202
    assert accepted.json() == {"status": "accepted"}
    assert replayed.status_code == 202
    assert replayed.json() == {"status": "accepted"}
    assert signaler.handoff_completions == [
        (task.workflow_id, task.task_id, completion_id),
        (task.workflow_id, task.task_id, completion_id),
    ]
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "lifecycle engine unavailable; retry command"}


async def test_consumer_mutations_share_one_csrf_gate_while_preview_and_capability_do_not(
    db: None,
) -> None:
    """Cookie-shaped state changes fail before domain work; local/read/capability seams stay distinct."""
    app = create_app()
    verifier = HeaderBoundCsrfProtection("session-bound-token")
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        tenant_id = await _onboard(client, f"csrf-owner-{uuid4().hex}")
        app.state.container.csrf_protection = verifier
        auth = _auth_headers(tenant_id)

        unauthenticated = await client.put(
            "/v1/preferences",
            json={"interests": ["jazz"], "revision": 1},
        )
        rejected = [
            await client.put(
                "/v1/preferences",
                json={"interests": ["jazz"], "revision": 1},
                headers=auth,
            ),
            await client.post(
                "/v1/requests",
                json={"text": "free music this weekend"},
                headers=auth,
            ),
            await client.post(
                "/v1/feed-feedback",
                json={
                    "signal_id": str(uuid4()),
                    "canonical_event_id": str(uuid4()),
                    "kind": "click",
                },
                headers=auth,
            ),
            await client.post(
                "/v1/unrsvp",
                json={"canonical_event_id": str(uuid4())},
                headers=auth,
            ),
            await client.post("/v1/me/tasks/missing-task/done", headers=auth),
        ]

        preview = await client.post(
            "/v1/feed",
            json={"text": "free music this weekend"},
            headers=auth,
        )
        capability = await client.post(f"/v1/tasks/{'A' * 43}/done")
        accepted = await client.put(
            "/v1/preferences",
            json={"interests": ["jazz"], "revision": 1},
            headers={**auth, "X-Test-CSRF": verifier.expected_token},
        )

    assert unauthenticated.status_code == 401
    assert all(response.status_code == 403 for response in rejected)
    assert all(
        response.json() == {"detail": "state-change verification required"} for response in rejected
    )
    assert preview.status_code == 200
    assert capability.status_code == 404
    assert accepted.status_code == 200
    assert verifier.calls == [(tenant_id, None)] * len(rejected) + [
        (tenant_id, verifier.expected_token)
    ]
