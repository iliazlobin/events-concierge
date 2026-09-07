"""API liveness/readiness and production-edge safety contracts."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from events_concierge.adapters.mock.object_store import MockFilesystemObjectStore
from events_concierge.composition import Container
from events_concierge.config import Settings

app_module = importlib.import_module("events_concierge.api.app")


class _BlockedSystemScope:
    async def __aenter__(self) -> object:
        await asyncio.Event().wait()
        return object()

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback


def _async_result(value: bool) -> Callable[..., Awaitable[bool]]:
    async def result(*args: object, **kwargs: object) -> bool:
        del args, kwargs
        return value

    return result


async def _get(app: FastAPI, path: str) -> tuple[int, dict[str, object]]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(path)
    return response.status_code, cast("dict[str, object]", response.json())


async def test_liveness_is_shallow_and_readiness_exposes_temporal_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "_database_is_ready", _async_result(True))
    monkeypatch.setattr(app_module, "_temporal_is_reachable", _async_result(False))
    app = app_module.create_app()

    health_status, health = await _get(app, "/healthz")
    ready_status, ready = await _get(app, "/readyz")

    assert health_status == 200
    assert health == {"status": "ok"}
    assert ready_status == 200
    assert ready == {
        "status": "ready",
        "components": {
            "database": "ready",
            "temporal": "degraded",
            "identity": "not_configured",
        },
    }


async def test_release_identity_and_metrics_are_bounded_operational_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision = "0123456789abcdef0123456789abcdef01234567"
    digest = "sha256:" + "a" * 64
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(release_revision=revision, image_digest=digest),
    )
    monkeypatch.setattr(app_module, "_database_is_ready", _async_result(True))
    monkeypatch.setattr(app_module, "_temporal_is_reachable", _async_result(False))
    app = app_module.create_app()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        await client.get("/readyz")
        version = await client.get("/versionz")
        metrics = await client.get("/metrics")

    assert version.status_code == 200
    assert version.json() == {"release_revision": revision, "image_digest": digest}
    assert version.headers["cache-control"] == "no-store, max-age=0"
    assert metrics.status_code == 200
    assert metrics.headers["content-type"] == "text/plain; version=0.0.4; charset=utf-8"
    assert f'release_revision="{revision}"' in metrics.text
    assert 'dependency="database"} 1' in metrics.text
    assert 'dependency="temporal"} 0' in metrics.text
    assert 'dependency="identity"} 1' in metrics.text
    assert 'route="/readyz"' in metrics.text


async def test_readiness_fails_when_the_durable_database_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "_database_is_ready", _async_result(False))
    monkeypatch.setattr(app_module, "_temporal_is_reachable", _async_result(True))

    status, body = await _get(app_module.create_app(), "/readyz")

    assert status == 503
    assert body == {
        "status": "not_ready",
        "components": {
            "database": "unavailable",
            "temporal": "ready",
            "identity": "not_configured",
        },
    }


async def test_database_readiness_probe_has_a_bounded_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "_READINESS_TIMEOUT_SECONDS", 0.001)
    monkeypatch.setattr(app_module, "system_session_scope", _BlockedSystemScope)

    assert await app_module._database_is_ready() is False


async def test_local_fixture_onboarding_is_not_exposed_by_production_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(mock_cloud=False),
    )
    app = app_module.create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/onboard",
            content=b"{",
            headers={"Content-Type": "application/json"},
        )

    assert "/v1/onboard" not in app.openapi()["paths"]
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


async def test_request_body_limit_rejects_declared_and_chunked_oversize_payloads() -> None:
    app = app_module.create_app()
    oversized = b"x" * (app_module._MAX_REQUEST_BODY_BYTES + 1)

    async def chunks() -> AsyncIterator[bytes]:
        midpoint = len(oversized) // 2
        yield oversized[:midpoint]
        yield oversized[midpoint:]

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        declared = await client.post("/v1/feed", content=oversized)
        chunked = await client.post(
            "/v1/feed",
            content=chunks(),
            headers={"Content-Type": "application/json"},
        )

    expected = {"detail": "request body too large"}
    assert declared.status_code == 413
    assert declared.json() == expected
    assert chunked.status_code == 413
    assert chunked.json() == expected


async def test_request_body_limit_rejects_malformed_or_inconsistent_content_length() -> None:
    app = app_module.create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        malformed = await client.post(
            "/v1/feed",
            content=b"{}",
            headers={"Content-Length": "not-a-decimal"},
        )
        inconsistent = await client.post(
            "/v1/feed",
            content=b"{}",
            headers={"Content-Length": "1"},
        )
        enormous = await client.post(
            "/v1/feed",
            content=b"",
            headers={"Content-Length": "9" * 5000},
        )

    assert malformed.status_code == 400
    assert malformed.json() == {"detail": "invalid Content-Length"}
    assert inconsistent.status_code == 400
    assert inconsistent.json() == {"detail": "Content-Length does not match request body"}
    assert enormous.status_code == 413
    assert enormous.json() == {"detail": "request body too large"}


async def test_request_body_read_deadline_rejects_a_stalled_chunked_caller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(request_body_timeout_seconds=0.1)
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    app = app_module.create_app()

    async def stalled_body() -> AsyncIterator[bytes]:
        yield b'{"text":"never finishes'
        await asyncio.Event().wait()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/feed",
            content=stalled_body(),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 408
    assert response.json() == {"detail": "request body timed out"}


def test_request_body_read_deadline_has_a_validated_operational_bound() -> None:
    assert Settings().request_body_timeout_seconds == 10.0
    assert Settings(request_body_timeout_seconds=0.1).request_body_timeout_seconds == 0.1
    assert Settings(request_body_timeout_seconds=60.0).request_body_timeout_seconds == 60.0

    for invalid in (0.0, 0.099, 60.01, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            Settings(request_body_timeout_seconds=invalid)


async def test_production_temporal_outage_keeps_durable_intake_available(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def unavailable(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise OSError("fixture engine outage")

    monkeypatch.setattr(app_module, "connect_temporal", unavailable)
    app = FastAPI()
    container = cast(
        "Container",
        SimpleNamespace(object_store=MockFilesystemObjectStore(tmp_path)),
    )

    await app_module._configure_temporal(
        app,
        Settings(
            mock_cloud=False,
            temporal_tls_enabled=True,
            temporal_tls_domain="temporal.example.test",
            temporal_api_key="fixture-api-key",
        ),
        container,
    )

    assert app.state.temporal is None
    assert app.state.request_starter is None
    assert app.state.lifecycle_signaler is None


async def test_invalid_production_temporal_credentials_fail_startup(tmp_path: Path) -> None:
    app = FastAPI()
    container = cast(
        "Container",
        SimpleNamespace(object_store=MockFilesystemObjectStore(tmp_path)),
    )

    with pytest.raises(ValueError, match="requires TLS"):
        await app_module._configure_temporal(
            app,
            Settings(
                mock_cloud=False,
                temporal_tls_enabled=False,
                temporal_api_key="fixture-api-key",
            ),
            container,
        )
