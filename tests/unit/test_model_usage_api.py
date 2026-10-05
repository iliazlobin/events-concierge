"""Model administration inherits existing identity, origin, privacy and OCC boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.unit.test_operator_api import _ORIGIN, _SUBJECT, _app, _headers

from events_concierge.api.model_usage import install_model_usage_routes


class Store:
    def __init__(self) -> None:
        self.actors: list[str] = []
        self.revision = 1
        self.calls = 0

    async def budget(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        return {
            "revision": self.revision,
            "mode": "enforce",
            "daily_limit_usd": None,
            "monthly_limit_usd": None,
            "daily_used_usd": "0",
            "monthly_used_usd": "0",
            "alert_percent": 80,
            "updated_at": now,
            "generated_at": now,
            "day_start": now,
            "month_start": now,
            "unknown_calls": 0,
            "pending_calls": 0,
            "private": "must-not-render",
        }

    async def update_budget(self, body: dict[str, Any], actor: str) -> dict[str, Any] | None:
        if body["expected_revision"] != self.revision:
            return None
        self.actors.append(actor)
        self.revision += 1
        return await self.budget()

    async def report(
        self, start: datetime, end: datetime, hours: int, model: str | None
    ) -> dict[str, Any]:
        self.calls += 1
        return {
            "generated_at": datetime.now(UTC),
            "start_at": start,
            "end_at": end,
            "bucket_hours": hours,
            "model": model,
            "tracked_since": None,
            "model_options": [],
            "totals": {
                "calls": 0,
                "failed": 0,
                "cost_usd": "0",
                "unknown_cost_calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "latency_ms": None,
                "pending": 0,
                "unknown_token_calls": 0,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
            },
            "series": [],
            "models": [],
            "recent": [],
            "api_key": "never-return",
        }


_UPDATE = {
    "expected_revision": 1,
    "mode": "enforce",
    "daily_limit_usd": "2.50",
    "monthly_limit_usd": "20",
    "alert_percent": 80,
}


@pytest.mark.parametrize(
    ("role", "expected"), [("viewer", 403), ("operator", 403), ("reviewer", 200)]
)
async def test_budget_writes_require_reviewer_and_actor_is_verified(
    role: str, expected: int
) -> None:
    app, _ = _app(role)
    store = Store()
    app.state.model_usage = store
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        read = await client.get("/admin/v1/models/budget", headers=_headers())
        response = await client.patch("/admin/v1/models/budget", json=_UPDATE, headers=_headers())
    assert read.status_code == 200
    assert response.status_code == expected
    assert "private" not in read.json()
    assert store.actors == ([f"iap:{_SUBJECT}"] if expected == 200 else [])


async def test_budget_edits_require_same_origin_and_revision_and_reject_forged_actor() -> None:
    app, _ = _app()
    store = Store()
    app.state.model_usage = store
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        assert (
            await client.patch(
                "/admin/v1/models/budget",
                json=_UPDATE,
                headers={**_headers(), "origin": "https://attacker.test"},
            )
        ).status_code == 403
        assert (
            await client.patch(
                "/admin/v1/models/budget", json={**_UPDATE, "actor": "owner"}, headers=_headers()
            )
        ).status_code == 422
        assert (
            await client.patch("/admin/v1/models/budget", json=_UPDATE, headers=_headers())
        ).status_code == 200
        conflict = await client.patch("/admin/v1/models/budget", json=_UPDATE, headers=_headers())
    assert conflict.status_code == 409 and len(store.actors) == 1
    assert conflict.headers["cache-control"].startswith("no-store")


async def test_bounded_aware_windows_and_missing_auth_are_rejected_before_store() -> None:
    app, _ = _app("viewer")
    store = Store()
    app.state.model_usage = store
    now = datetime.now(UTC)
    valid = {"start_at": (now - timedelta(days=7)).isoformat(), "end_at": now.isoformat()}
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        assert (await client.get("/admin/v1/models/usage", params=valid)).status_code == 401
        for params in [
            {**valid, "start_at": (now - timedelta(days=91)).isoformat()},
            {**valid, "bucket_hours": 1},
            {**valid, "start_at": "2026-09-01T00:00:00"},
            {**valid, "model": "bad\nmodel"},
        ]:
            assert (
                await client.get("/admin/v1/models/usage", params=params, headers=_headers())
            ).status_code == 422
        response = await client.get("/admin/v1/models/usage", params=valid, headers=_headers())
    assert store.calls == 1 and response.status_code == 200
    assert "api_key" not in response.json()
    assert response.headers["cache-control"].startswith("no-store")


@pytest.mark.parametrize(("bucket_hours", "days"), [("1", 1), ("24", 7)])
async def test_usage_accepts_explicit_http_bucket_values(bucket_hours: str, days: int) -> None:
    app, _ = _app("viewer")
    store = Store()
    app.state.model_usage = store
    now = datetime.now(UTC)
    params = {
        "start_at": (now - timedelta(days=days)).isoformat(),
        "end_at": now.isoformat(),
        "bucket_hours": bucket_hours,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        response = await client.get("/admin/v1/models/usage", params=params, headers=_headers())
    assert response.status_code == 200
    assert response.json()["bucket_hours"] == int(bucket_hours)
    assert store.calls == 1


@pytest.mark.parametrize("bucket_hours", ["0", "2", "25", "daily"])
async def test_usage_rejects_unsupported_http_bucket_values_before_store(bucket_hours: str) -> None:
    app, _ = _app("viewer")
    store = Store()
    app.state.model_usage = store
    now = datetime.now(UTC)
    params = {
        "start_at": (now - timedelta(days=1)).isoformat(),
        "end_at": now.isoformat(),
        "bucket_hours": bucket_hours,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        response = await client.get("/admin/v1/models/usage", params=params, headers=_headers())
    assert response.status_code == 422
    assert store.calls == 0


@pytest.mark.parametrize("host", ["attacker.test", "127.0.0.1:8000"])
async def test_local_boundary_requires_loopback(host: str) -> None:
    app = FastAPI()
    app.state.settings = SimpleNamespace(admin_ingestion_enabled=True, mock_cloud=True, env="local")
    app.state.ingestion_admin = object()
    app.state.model_usage = Store()
    install_model_usage_routes(app)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=f"http://{host}") as client:
        response = await client.get("/admin/v1/models/budget")
    assert response.status_code == (200 if host.startswith("127.") else 403)
