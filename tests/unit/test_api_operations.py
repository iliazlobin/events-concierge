"""API liveness/readiness and production-edge safety contracts."""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

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
        "components": {"database": "ready", "temporal": "degraded"},
    }


async def test_readiness_fails_when_the_durable_database_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "_database_is_ready", _async_result(False))
    monkeypatch.setattr(app_module, "_temporal_is_reachable", _async_result(True))

    status, body = await _get(app_module.create_app(), "/readyz")

    assert status == 503
    assert body == {
        "status": "not_ready",
        "components": {"database": "unavailable", "temporal": "ready"},
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
            json={"notify_email": "user@example.test"},
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "not found"}


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
