"""The first-release HTTP boundary rejects deferred actions before any side effect."""

from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from events_concierge.api import app as app_module
from events_concierge.api.release_profile import apply_release_profile
from events_concierge.config import Settings


@pytest.mark.parametrize("mock_cloud", [True, False])
async def test_discovery_rejects_deferred_routes_before_authentication_or_io(
    monkeypatch: pytest.MonkeyPatch, mock_cloud: bool,
) -> None:
    monkeypatch.setattr(
        app_module, "get_settings",
        lambda: Settings(release_profile="discovery", mock_cloud=mock_cloud, agent_enabled=True),
    )
    app = app_module.create_app()
    # No container/lifespan is installed. Reaching a handler or dependency would
    # fail this test, as well as exposing deferred capabilities to the caller.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for method, path in [
            ("POST", "/v1/chat"),
            ("POST", "/v1/requests"),
            ("GET", "/v1/requests"),
            ("GET", "/v1/registrations"),
            ("GET", "/v1/tasks"),
            ("POST", "/v1/feed"),
            ("POST", "/v1/unrsvp"),
            ("POST", "/v1/me/tasks/test/done"),
            ("POST", "/v1/tasks/test/done"),
            ("GET", "/v1/tasks/test/done"),
            ("GET", "/v1/me/api-keys"),
            ("POST", "/v1/me/api-keys"),
            ("DELETE", f"/v1/me/api-keys/{uuid4()}"),
            ("POST", f"/v1/catalog/entities/{uuid4()}/refresh"),
            ("GET", "/app"),
        ]:
            response = await client.request(method, path)
            assert response.status_code == 404, (method, path, response.text)
        config = await client.get("/v1/ui-config")
        assert config.json()["release_profile"] == "discovery"
        assert (await client.get("/healthz")).status_code == 200
    schema = app.openapi()["paths"]
    assert "/v1/chat" not in schema
    assert "/v1/requests" not in schema
    assert "/v1/me/api-keys" not in schema
    assert "/v1/me/api-keys/{key_id}" not in schema
    assert "/v1/catalog/events" in schema
    assert "/v1/catalog/events/summary" in schema
    assert "/v1/me/erasure-requests" in schema
    assert "/v1/me/saved-filters" in schema


async def test_future_consumer_route_requires_explicit_discovery_admission() -> None:
    app = FastAPI()

    @app.post("/v1/future-purchase")
    async def future_purchase() -> None:
        raise AssertionError("unreviewed action executed")

    apply_release_profile(app, "discovery")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.post("/v1/future-purchase")).status_code == 404


def test_full_profile_retains_deferred_workflows_for_development(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_module, "get_settings", lambda: Settings(release_profile="full"))
    schema = app_module.create_app().openapi()["paths"]
    assert "/v1/requests" in schema
    assert "/v1/unrsvp" in schema
    assert set(schema["/v1/me/api-keys"]) == {"get", "post"}
    assert "delete" in schema["/v1/me/api-keys/{key_id}"]
