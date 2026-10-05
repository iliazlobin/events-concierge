"""Separate production operator ASGI app; no consumer/provider graph is instantiated.

Run uvicorn events_concierge.api.operator:create_operator_app --factory. The public web service
forwards the original signed IAP assertion; this API validates it on every request.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..adapters.agent_runtime.openrouter.key_usage import OpenRouterKeyUsage
from ..adapters.postgres.catalog import PostgresCatalogRepository
from ..adapters.postgres.command_investigation import CommandInvestigationStore
from ..adapters.postgres.ingestion_admin import PostgresIngestionAdminRepository
from ..adapters.postgres.model_usage import PostgresModelUsageStore
from ..adapters.postgres.operator_operations import PostgresOperatorOperationsRepository
from ..adapters.ranking.embedding import DeterministicEmbedding
from ..application.catalog_execution_descriptors import CatalogExecutionDescriptorRegistry
from ..application.ingestion_admin import IngestionAdminService
from ..config import Settings, get_settings
from ..deployment.startup import preflight_operator_runtime
from ..infra.operator_database import OperatorDatabase
from .admin import _local_ingestion_admin, install_ingestion_admin_routes
from .command_investigation import install_command_investigation_routes
from .model_usage import install_model_usage_routes
from .operator_auth import IapOperatorIdentityVerifier, OperatorPrincipal
from .operator_operations import install_operator_operations_routes

_MAX_BODY_BYTES = 16_384


class OperatorBodyLimitMiddleware:
    """Bound decoded admin bodies and the entire read interval, including chunked input."""

    def __init__(self, app: ASGIApp, *, timeout_seconds: float = 10) -> None:
        self._app = app
        self._timeout_seconds = timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] in {"GET", "HEAD"}:
            await self._app(scope, receive, send)
            return
        body = bytearray()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        return
                    body.extend(message.get("body", b""))
                    if len(body) > _MAX_BODY_BYTES:
                        raise HTTPException(413, "operator request body too large")
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            response = JSONResponse({"detail": "operator request body timed out"}, status_code=408)
            await response(scope, receive, send)
            return
        except HTTPException as error:
            response = JSONResponse({"detail": error.detail}, status_code=error.status_code)
            await response(scope, receive, send)
            return
        consumed = False

        async def replay() -> Message:
            nonlocal consumed
            if consumed:
                return await receive()
            consumed = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self._app(scope, replay, send)


def install_operator_session_routes(app: FastAPI) -> None:
    """Let the UI render actual server-authorized capabilities, including explicit local mode."""

    @app.get("/admin/v1/operator/session")
    async def operator_session(
        request: Request,
        response: Response,
        authority: Annotated[object, Depends(_local_ingestion_admin)],
    ) -> dict[str, object]:
        del authority
        response.headers["Cache-Control"] = "no-store, max-age=0"
        principal = getattr(request.state, "operator_principal", None)
        if isinstance(principal, OperatorPrincipal):
            return {
                "subject": principal.subject,
                "role": principal.role,
                "capabilities": sorted(principal.capabilities),
                "environment": request.app.state.settings.env,
                "authentication": "iap",
            }
        return {
            "subject": "local-admin",
            "role": "reviewer",
            "capabilities": [
                "ingestion.read",
                "ingestion.refresh",
                "ingestion.sources.enable",
                "ingestion.sources.configure",
                "models.budget.configure",
            ],
            "environment": request.app.state.settings.env,
            "authentication": "local",
        }


def build_operator_services(app: FastAPI, settings: Settings) -> OperatorDatabase:
    """Use one isolated operator pool for both aggregate and public-catalog projections."""
    database = OperatorDatabase(settings)
    repository = PostgresIngestionAdminRepository(
        CatalogExecutionDescriptorRegistry(settings.temporal_catalog_queue),
        session_scope=database.session_scope,
    )
    catalog = PostgresCatalogRepository(
        DeterministicEmbedding(),
        session_scope=database.session_scope,
    )
    app.state.ingestion_admin = IngestionAdminService(
        repository,
        catalog=catalog,
        release_revision=settings.release_revision,
        image_digest=settings.image_digest,
    )
    app.state.operator_operations = PostgresOperatorOperationsRepository(database.session_scope)
    app.state.operator_database = database
    app.state.model_usage = PostgresModelUsageStore(session_scope=database.session_scope)
    app.state.openrouter_key_usage = OpenRouterKeyUsage(
        os.environ.get("EC_OPENROUTER_API_KEY", "").strip()
    )
    app.state.command_investigation = CommandInvestigationStore(
        session_scope=database.session_scope,
        task_queue=settings.temporal_catalog_queue,
    )
    return database


def create_operator_app(settings: Settings | None = None) -> FastAPI:
    """Build only the explicitly enabled hosted operator process, without contacting a provider."""
    settings = settings or get_settings()
    # Revalidate injected model_copy snapshots as well as ordinary settings construction.
    settings = Settings.model_validate(
        {
            name: (
                None
                if name.endswith("_file") and name != "migration_url_file"
                else getattr(settings, name)
            )
            for name in Settings.model_fields
        }
    )
    if not settings.operator_api_enabled or settings.mock_cloud or settings.admin_ingestion_enabled:
        raise ValueError("standalone operator API requires the explicit non-mock operator profile")
    assert settings.operator_iap_audience is not None
    verifier = IapOperatorIdentityVerifier(
        audience=settings.operator_iap_audience,
        subject_roles=settings.operator_subject_roles,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        preflight_operator_runtime(settings)
        database = build_operator_services(app, settings)
        try:
            yield
        finally:
            await database.aclose()

    app = FastAPI(
        title="Events Concierge Operations",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.operator_boundary = True
    app.state.operator_identity_verifier = verifier
    app.state.ingestion_admin = None
    app.add_middleware(
        OperatorBodyLimitMiddleware, timeout_seconds=settings.request_body_timeout_seconds
    )
    install_ingestion_admin_routes(app)
    install_command_investigation_routes(app)
    install_operator_operations_routes(app)
    install_operator_session_routes(app)
    install_model_usage_routes(app)

    @app.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready() -> dict[str, str]:
        database = getattr(app.state, "operator_database", None)
        if database is None or not await database.ready():
            raise HTTPException(503, "operator database unavailable")
        return {"status": "ok"}

    return app
