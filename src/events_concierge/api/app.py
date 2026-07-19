"""FastAPI application with an injected tenant-authentication boundary.

The offline graph resolves a test-only header through ``AuthContextPort``; it never trusts a tenant
identifier in a request body.  A provisioned graph must inject the real OIDC BFF/session adapter
(FR-1.1/1.3). The feed endpoint reads the persisted catalog; source refresh is a separate durable
worker path, never an HTTP side effect of user intake. Temporal remains optional for registration.
"""

from __future__ import annotations

import asyncio
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from ..application.ranking_feedback import UnknownFeedbackEventError
from ..application.request_start import RequestIntakeService, RequestStartRelay
from ..composition import Container, build_container
from ..config import Settings, get_settings
from ..domain.credentials import Tenant
from ..domain.enums import LifecycleState
from ..domain.ids import request_workflow_id
from ..domain.lifecycle import HandoffCompletionTarget
from ..domain.ranking_feedback import FeedbackSignalKind, RankingFeedbackSignal
from ..domain.request import EventRequest, Feed
from ..infra.db import dispose_engine, system_session_scope
from ..infra.logging import configure_logging, get_logger
from ..ports.auth import AuthenticationFailedError
from ..ports.ranking_feedback import RankingFeedbackConflictError
from ..ports.workflows import RegistrationLifecycleSignaler
from ..workflows.temporal_client import connect_temporal, validate_temporal_settings

_log = get_logger("api")
_READINESS_TIMEOUT_SECONDS = 2.0


class OnboardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notify_email: str = Field(min_length=3, max_length=320)


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    cursor: str | None = Field(default=None, max_length=2048)


class FeedItemOut(BaseModel):
    canonical_event_id: UUID
    title: str
    start_at: str
    price_status: str
    score: float
    rationale: str
    conflict: str
    lanes: list[str]
    registration_urls: list[str]


class FeedOut(BaseModel):
    request_id: UUID
    items: list[FeedItemOut]
    next_cursor: str | None


class FeedFeedbackBody(BaseModel):
    """Self-reported implicit signal with a caller-minted replay key (FR-2.1, FR-4.3, NFR-8)."""

    model_config = ConfigDict(extra="forbid")

    signal_id: UUID
    canonical_event_id: UUID
    kind: FeedbackSignalKind


class FeedFeedbackAccepted(BaseModel):
    """Converged result for one at-least-once feedback delivery."""

    status: str


class RequestAccepted(BaseModel):
    request_id: UUID
    workflow_id: str
    workflow_started: bool
    feed: FeedOut


class UnrsvpBody(BaseModel):
    """Inbound user withdrawal command; the generated id makes reply/API redelivery observable."""

    model_config = ConfigDict(extra="forbid")

    canonical_event_id: UUID
    request_id: UUID = Field(default_factory=uuid4)


class UnrsvpAccepted(BaseModel):
    status: str
    workflow_id: str
    request_id: UUID


class HandoffCompletionAccepted(BaseModel):
    status: str


async def _resolve_handoff_completion_target(
    container: Container,
    token: str,
) -> HandoffCompletionTarget:
    """Resolve one fixed-shape bearer capability without reflecting it in an error."""
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise HTTPException(status_code=404, detail="handoff task not found")
    target = await container.handoff_repo.resolve_completion_token(token)
    if target is None:
        raise HTTPException(status_code=404, detail="handoff task not found")
    return target


def _completion_page(message: str, *, show_form: bool) -> HTMLResponse:
    """Render an inert, scanner-safe confirmation page with no capability referrer."""
    form = (
        (
            "<p>Only continue after you completed registration on the event site.</p>"
            '<form method="post"><button type="submit">Mark registration done</button></form>'
        )
        if show_form
        else ""
    )
    return HTMLResponse(
        content=(
            "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>Events Concierge</title></head><body>"
            f"<main><h1>{message}</h1>{form}</main></body></html>"
        ),
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Content-Security-Policy": (
                "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
        },
    )


def _feed_out(feed: Feed) -> FeedOut:
    return FeedOut(
        request_id=feed.request_id,
        next_cursor=feed.next_cursor,
        items=[
            FeedItemOut(
                canonical_event_id=item.canonical_event.canonical_event_id,
                title=item.canonical_event.title,
                start_at=item.canonical_event.start_at.isoformat(),
                price_status=item.canonical_event.price_status.value,
                score=item.score,
                rationale=item.rationale,
                conflict=item.conflict_verdict.value,
                lanes=[lane.value for lane in item.lane_plan],
                registration_urls=item.canonical_event.registration_urls(),
            )
            for item in feed.items
        ],
    )


async def _parse(container: Container, tenant_id: UUID, text: str) -> tuple[EventRequest, UUID]:
    """Parse against the existing catalog; never fetch public websites on this path (NFR-8)."""
    request_id = uuid4()
    request = await container.parser.parse(tenant_id, request_id, text)
    return request, request_id


async def _authenticated_tenant(request: Request) -> UUID:
    """Resolve one edge-authenticated tenant; caller JSON can never select an RLS context.

    The local header adapter is deliberately an injected test seam only.  Production composition
    refuses that adapter and must supply the signed OIDC BFF/session implementation (FR-1.1/1.3).
    """
    container: Container = request.app.state.container
    try:
        return await container.auth_context.resolve_tenant_id(request.headers)
    except AuthenticationFailedError as error:
        raise HTTPException(status_code=401, detail="authentication required") from error


type AuthenticatedTenant = Annotated[UUID, Depends(_authenticated_tenant)]


async def _record_feed_feedback(
    request: Request, body: FeedFeedbackBody, tenant_id: AuthenticatedTenant
) -> FeedFeedbackAccepted:
    """Record a tenant-local, derived preference signal for a later feed re-score.

    The API intentionally accepts only a canonical event id, a fixed signal kind, and a
    caller-minted idempotency key. It never accepts a body tenant, raw event text, dwell duration,
    or client-selected score. Until a signed served-item capability is introduced, these are
    self-reported feedback signals rather than cryptographically proven impressions (FR-1.1/1.3,
    FR-2.1, FR-4.3, NFR-8).
    """
    container: Container = request.app.state.container
    try:
        status = await container.ranking_feedback.record(
            tenant_id,
            RankingFeedbackSignal(
                signal_id=body.signal_id,
                canonical_event_id=body.canonical_event_id,
                kind=body.kind,
            ),
        )
    except UnknownFeedbackEventError as error:
        raise HTTPException(status_code=404, detail="canonical event not found") from error
    except RankingFeedbackConflictError as error:
        raise HTTPException(status_code=409, detail="feedback signal id conflicts") from error
    return FeedFeedbackAccepted(status=status.value)


async def _configure_temporal(app: FastAPI, settings: Settings, container: Container) -> None:
    """Install optional engine boundaries without making read-only API paths depend on Temporal."""
    app.state.temporal = None
    app.state.request_starter = None
    app.state.lifecycle_signaler = None
    # Configuration is a deployment invariant and fails startup even when the engine is down.
    # Reachability is different: the database outbox is the durable outage boundary, so a valid
    # deployment keeps accepting durable intake while the request-start relay waits for recovery.
    validate_temporal_settings(settings)
    try:
        from ..workflows.start import (
            TemporalRegistrationLifecycleSignaler,
            TemporalRequestWorkflowStarter,
        )

        app.state.temporal = await connect_temporal(
            settings,
            container.object_store,
            lazy=True,
        )
        app.state.request_starter = TemporalRequestWorkflowStarter(app.state.temporal, settings)
        app.state.lifecycle_signaler = TemporalRegistrationLifecycleSignaler(app.state.temporal)
    except Exception as exc:
        _log.warning(
            "temporal unavailable; request starts remain durable in the start outbox",
            error=str(exc),
        )


async def _database_is_ready() -> bool:
    """Probe the application-role database path without opening a tenant data scope."""
    try:
        async with asyncio.timeout(_READINESS_TIMEOUT_SECONDS):
            async with system_session_scope() as session:
                await session.execute(text("SELECT 1"))
    except Exception as exc:
        _log.warning("database_readiness_failed", error=type(exc).__name__)
        return False
    return True


async def _temporal_is_reachable(app: FastAPI) -> bool:
    """Report engine degradation without making durable request intake unavailable."""
    client = getattr(app.state, "temporal", None)
    if client is None:
        return False
    try:
        return bool(
            await client.service_client.check_health(
                retry=False,
                timeout=timedelta(seconds=_READINESS_TIMEOUT_SECONDS),
            )
        )
    except Exception as exc:
        _log.warning("temporal_readiness_failed", error=type(exc).__name__)
        return False


async def _mark_handoff_done(request: Request, token: str) -> HandoffCompletionAccepted:
    """Capability-authenticate and durably signal one retained handoff workflow.

    The path token is independent of the deterministic task id. PostgreSQL stores only its digest
    and returns an opaque routing target through a narrow privileged function; no caller-selected
    tenant/workflow/event identity is accepted. Temporal signal acknowledgement is the durability
    boundary. The workflow then performs FR-16 source read-back and a fresh freeBusy check before
    any lifecycle/calendar advance (FR-1.3, FR-6.3, FR-8.8).
    """
    container: Container = request.app.state.container
    target = await _resolve_handoff_completion_target(container, token)
    if target.status == "used":
        return HandoffCompletionAccepted(status="already_accepted")
    if target.status in {"expired", "inactive"}:
        raise HTTPException(status_code=410, detail="handoff task is no longer active")
    if target.status != "active":
        raise HTTPException(status_code=404, detail="handoff task not found")

    signaler: RegistrationLifecycleSignaler | None = request.app.state.lifecycle_signaler
    if signaler is None:
        raise HTTPException(
            status_code=503,
            detail="lifecycle engine unavailable; retry command",
        )
    completion_id = f"{target.workflow_id}:handoff-completion:{target.task_id}:1"
    try:
        await signaler.signal_handoff_completed(
            target.workflow_id,
            target.task_id,
            completion_id,
        )
    except Exception as exc:
        _log.warning(
            "handoff_completion_signal_failed",
            workflow_id=target.workflow_id,
            task_id=target.task_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=503,
            detail="handoff completion was not accepted; retry",
        ) from exc
    return HandoffCompletionAccepted(status="accepted")


async def _show_handoff_done(request: Request, token: str) -> HTMLResponse:
    """Show a confirmation form; GET itself never consumes a token or signals Temporal."""
    container: Container = request.app.state.container
    target = await _resolve_handoff_completion_target(container, token)
    if target.status == "used":
        return _completion_page("Completion already submitted", show_form=False)
    if target.status in {"expired", "inactive"}:
        raise HTTPException(status_code=410, detail="handoff task is no longer active")
    if target.status != "active":
        raise HTTPException(status_code=404, detail="handoff task not found")
    return _completion_page("Confirm completed registration", show_form=True)


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        app.state.container = build_container(settings)
        try:
            await _configure_temporal(app, settings, app.state.container)
            yield
        finally:
            await dispose_engine()

    app = FastAPI(title="Events Concierge", version="0.1.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Expose dependency readiness while treating a Temporal outage as durable degradation."""
        database_ready = await _database_is_ready()
        temporal_ready = await _temporal_is_reachable(app)
        return JSONResponse(
            status_code=200 if database_ready else 503,
            content={
                "status": "ready" if database_ready else "not_ready",
                "components": {
                    "database": "ready" if database_ready else "unavailable",
                    "temporal": "ready" if temporal_ready else "degraded",
                },
            },
        )

    @app.post("/v1/onboard", status_code=201)
    async def onboard(body: OnboardBody) -> dict[str, str]:
        if not settings.mock_cloud:
            # Production identity provisioning belongs to the signed OIDC/BFF bootstrap. Leaving
            # this local fixture route reachable would create unauthenticated tenant identities.
            raise HTTPException(status_code=404, detail="not found")
        container: Container = app.state.container
        tenant = Tenant(
            tenant_id=uuid4(),
            oidc_subject=f"oidc|{uuid4()}",
            notify_email=body.notify_email,
            relay_inbox=f"{uuid4().hex[:12]}@u.concierge.test",
        )
        await container.tenant_repo.add(tenant)
        return {"tenant_id": str(tenant.tenant_id), "relay_inbox": tenant.relay_inbox}

    @app.post("/v1/requests", response_model=RequestAccepted)
    async def create_request(body: RequestBody, tenant_id: AuthenticatedTenant) -> RequestAccepted:
        container: Container = app.state.container
        intake = RequestIntakeService(container.request_repo, container.parser)
        request = await intake.accept(tenant_id, body.text)
        feed = await container.feed.build_feed(request)

        workflow_id = request_workflow_id(tenant_id, request.request_id)
        started = False
        starter = app.state.request_starter
        if starter is not None:
            relay = RequestStartRelay(
                container.request_repo,
                starter,
                lease_seconds=settings.request_start_lease_seconds,
            )
            started = await relay.relay_request(tenant_id, request.request_id)
        return RequestAccepted(
            request_id=request.request_id,
            workflow_id=workflow_id,
            workflow_started=started,
            feed=_feed_out(feed),
        )

    @app.post("/v1/feed", response_model=FeedOut)
    async def feed_page(body: RequestBody, tenant_id: AuthenticatedTenant) -> FeedOut:
        container: Container = app.state.container
        request, _ = await _parse(container, tenant_id, body.text)
        feed = await container.feed.build_feed(request, cursor=body.cursor)
        return _feed_out(feed)

    app.post("/v1/feed-feedback", status_code=202, response_model=FeedFeedbackAccepted)(
        _record_feed_feedback
    )

    @app.post("/v1/unrsvp", status_code=202, response_model=UnrsvpAccepted)
    async def unrsvp(body: UnrsvpBody, tenant_id: AuthenticatedTenant) -> UnrsvpAccepted:
        """Signal the RLS-visible active lifecycle; never claim acceptance without Temporal durability.

        The workflow id comes from the lifecycle row rather than reconstructing a best-effort name,
        so a retry/attempt representation remains encapsulated at the durable boundary (FR-6.7/8.8,
        ADR-003/007).
        """
        container: Container = app.state.container
        lifecycle = await container.lifecycle_repo.find_active(tenant_id, body.canonical_event_id)
        if lifecycle is None:
            raise HTTPException(status_code=404, detail="no active lifecycle for this event")
        # ``registered`` is the source-confirmed but not-yet-calendar-scheduled window.  Accepting
        # a withdrawal there would let a queued signal race the remainder of the registration saga.
        # The caller can retry once the durable calendar stage has reached a post-booking state;
        # direct Temporal signals are independently gated by the child workflow (FR-8.8, ADR-003).
        if lifecycle.state not in {
            LifecycleState.SCHEDULED,
            LifecycleState.RECONCILED,
            LifecycleState.WITHDRAWING,
        }:
            raise HTTPException(
                status_code=409,
                detail="registration is not yet ready for withdrawal; retry shortly",
            )
        signaler: RegistrationLifecycleSignaler | None = app.state.lifecycle_signaler
        if signaler is None:
            raise HTTPException(
                status_code=503, detail="lifecycle engine unavailable; retry command"
            )
        try:
            await signaler.signal_unrsvp(lifecycle.workflow_id, str(body.request_id))
        except Exception as exc:
            _log.warning(
                "unrsvp_signal_failed",
                workflow_id=lifecycle.workflow_id,
                error=str(exc),
            )
            raise HTTPException(
                status_code=503, detail="lifecycle command was not accepted; retry"
            ) from exc
        return UnrsvpAccepted(
            status="accepted",
            workflow_id=lifecycle.workflow_id,
            request_id=body.request_id,
        )

    app.get(
        "/v1/tasks/{token}/done",
        response_class=HTMLResponse,
        include_in_schema=False,
    )(_show_handoff_done)
    app.post(
        "/v1/tasks/{token}/done",
        status_code=202,
        response_model=HandoffCompletionAccepted,
    )(_mark_handoff_done)

    return app


app = create_app()
