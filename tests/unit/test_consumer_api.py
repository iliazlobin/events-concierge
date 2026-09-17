"""Consumer-shell configuration and request-boundary validation."""

from __future__ import annotations

import importlib

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from events_concierge.config import Settings

app_module = importlib.import_module("events_concierge.api.app")


async def test_consumer_shell_is_same_origin_hardened_and_assets_are_allowlisted() -> None:
    app = app_module.create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        shell = await client.get("/")
        app_alias = await client.get("/app")
        stylesheet = await client.get("/assets/app.css")
        script = await client.get("/assets/app.js")
        map_stylesheet = await client.get("/assets/leaflet.css")
        map_script = await client.get("/assets/leaflet.js")
        mark = await client.get("/assets/mark.svg")
        manifest = await client.get("/manifest.webmanifest")
        unknown_asset = await client.get("/assets/index.html")

    assert shell.status_code == 200
    assert app_alias.status_code == 200
    assert "Events Concierge" in shell.text
    assert shell.text == app_alias.text
    assert shell.headers["cache-control"] == "no-cache, max-age=0"
    assert shell.headers["x-frame-options"] == "DENY"
    assert shell.headers["x-content-type-options"] == "nosniff"
    assert shell.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "connect-src 'self'" in shell.headers["content-security-policy"]
    assert "object-src 'none'" in shell.headers["content-security-policy"]
    assert "img-src 'self' data: https://tile.openstreetmap.org" in shell.headers[
        "content-security-policy"
    ]
    assert stylesheet.status_code == 200
    assert stylesheet.headers["x-content-type-options"] == "nosniff"
    assert stylesheet.headers["content-type"].startswith("text/css")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert map_stylesheet.status_code == 200
    assert map_stylesheet.headers["content-type"].startswith("text/css")
    assert map_script.status_code == 200
    assert map_script.headers["content-type"].startswith("text/javascript")
    assert mark.status_code == 200
    assert mark.headers["content-type"].startswith("image/svg+xml")
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert unknown_asset.status_code == 404
    assert unknown_asset.json() == {"detail": "asset not found"}


async def test_ui_config_distinguishes_local_demo_from_deployment_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(mock_cloud=True, ui_auth_start_url=None),
    )
    local_app = app_module.create_app()
    async with AsyncClient(
        transport=ASGITransport(app=local_app),
        base_url="http://test",
    ) as client:
        local = await client.get("/v1/ui-config")

    monkeypatch.setattr(
        app_module,
        "get_settings",
        lambda: Settings(mock_cloud=False, ui_auth_start_url="/auth/login"),
    )
    deployed_app = app_module.create_app()
    async with AsyncClient(
        transport=ASGITransport(app=deployed_app),
        base_url="http://test",
    ) as client:
        deployed = await client.get("/v1/ui-config")

    assert local.status_code == 200
    assert local.json() == {
        "product_name": "Events Concierge",
        "release_profile": "full",
        "local_demo": True,
        "auth_mode": "local_demo",
        "auth_provider": None,
        "auth_start_url": None,
        "reauth_url": None,
        "logout_url": None,
        "csrf_cookie_name": None,
        "csrf_header_name": None,
    }
    assert deployed.status_code == 200
    assert deployed.json() == {
        "product_name": "Events Concierge",
        "release_profile": "full",
        "local_demo": False,
        "auth_mode": "deployment_session",
        "auth_provider": None,
        "auth_start_url": "/auth/login",
        "reauth_url": None,
        "logout_url": None,
        "csrf_cookie_name": None,
        "csrf_header_name": None,
    }
    assert "/v1/onboard" not in deployed_app.openapi()["paths"]


@pytest.mark.parametrize(
    "value",
    (
        "http://identity.example.test/login",
        "//identity.example.test/login",
        "/\\identity.example.test/login",
        "https://user:password@identity.example.test/login",
        "https://identity.example.test/login#fragment",
        "/login\nredirect",
        "",
    ),
)
def test_ui_auth_start_url_rejects_unsafe_redirect_targets(value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(ui_auth_start_url=value)


def test_ui_auth_start_url_accepts_relative_or_https_deployment_targets() -> None:
    assert Settings(ui_auth_start_url="/auth/login?return=%2Fapp").ui_auth_start_url == (
        "/auth/login?return=%2Fapp"
    )
    assert (
        Settings(
            ui_auth_start_url="https://identity.example.test/login?client=concierge"
        ).ui_auth_start_url
        == "https://identity.example.test/login?client=concierge"
    )


def test_preference_input_normalizes_bounded_unique_interest_labels() -> None:
    body = app_module.PreferencesBody(
        interests=["  Jazz  ", "Live    Music", "COMMUNITY"],
        revision=2,
    )
    assert body.interests == ["jazz", "live music", "community"]

    with pytest.raises(ValidationError, match="unique"):
        app_module.PreferencesBody(interests=["Jazz", " jazz "], revision=3)
    with pytest.raises(ValidationError, match="bounded, printable"):
        app_module.PreferencesBody(interests=["x" * 65], revision=3)
    with pytest.raises(ValidationError):
        app_module.PreferencesBody(interests=["jazz"], revision=0)
