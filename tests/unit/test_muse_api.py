"""Muse bearer access cannot inherit browser, admin or account-setting authority."""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from tests.unit.test_muse import published_event, service_for

from events_concierge.api import app as app_module
from events_concierge.config import Settings
from events_concierge.ports.auth import AuthenticationFailedError, CsrfVerificationFailedError

ORIGIN = "https://events.example.test"


@pytest.mark.parametrize("profile", ["full", "discovery"])
@pytest.mark.parametrize("explicitly_disabled", [False, True])
async def test_disabled_muse_omits_routes_before_authentication_or_io(
    monkeypatch, profile, explicitly_disabled,
):
    monkeypatch.delenv("EC_MUSE_ENABLED", raising=False)
    settings = Settings(
        _env_file=None, release_profile=profile,
        **({"muse_enabled": False} if explicitly_disabled else {}),
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    app = app_module.create_app()
    # A disabled release must not need a container, authentication or Muse tables.
    batch_id, event_id = uuid4(), uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        for method, path in [
            ("GET", "/v1/me/muse/connection"),
            ("POST", "/v1/me/muse/connection"),
            ("DELETE", "/v1/me/muse/connection"),
            ("GET", "/v1/me/muse/batches"),
            ("POST", "/v1/me/muse/batches"),
            ("GET", "/v1/muse/openapi.json"),
            ("GET", "/v1/muse/connector/batches"),
            ("GET", f"/v1/muse/connector/batches/{batch_id}"),
            ("POST", f"/v1/muse/connector/batches/{batch_id}/items/{event_id}/claim"),
            ("PUT", f"/v1/muse/connector/batches/{batch_id}/items/{event_id}/outcome"),
        ]:
            response = await client.request(method, path)
            assert response.status_code == 404, (method, path, response.text)
        assert (await client.get("/v1/ui-config")).json()["muse_enabled"] is False
    assert not any("/muse/" in path for path in app.openapi()["paths"])


def muse_app(monkeypatch):
    tenant = uuid4()
    settings = Settings(
        _env_file=None, mock_cloud=True, release_profile="discovery", public_base_url=ORIGIN,
        muse_enabled=True,
    )
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    app = app_module.create_app()
    service, repository, catalog = service_for(published_event())
    hashes = {}

    async def connect(owner, digest, expires):
        hashes[owner] = digest

    async def authenticate(owner, digest):
        return hashes.get(owner) == digest

    async def revoke(owner):
        hashes.pop(owner, None)

    async def resolve(headers):
        if headers.get("cookie") != "fixture-session=owner":
            raise AuthenticationFailedError("no browser session")
        return tenant

    async def csrf(owner, headers):
        if headers.get("x-fixture-csrf") != "verified" or headers.get("origin") != ORIGIN:
            raise CsrfVerificationFailedError("no browser CSRF evidence")
        assert owner == tenant

    repository.connect.side_effect = connect
    repository.authenticate.side_effect = authenticate
    repository.revoke.side_effect = revoke
    repository.batches.return_value = []
    repository.batch.return_value = None
    app.state.settings = settings
    app.state.container = SimpleNamespace(
        muse=service,
        auth_context=SimpleNamespace(resolve_tenant_id=resolve),
        csrf_protection=SimpleNamespace(verify_state_change=csrf),
        tenant_repo=SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(tenant_id=tenant))),
        account_erasure_repo=None,
        catalog=catalog,
    )
    return app, repository, tenant


async def test_owner_key_creation_requires_browser_identity_and_csrf(monkeypatch):
    app, repository, tenant = muse_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        assert (await client.get("/v1/ui-config")).json()["muse_enabled"] is True
        assert (await client.post("/v1/me/muse/connection")).status_code == 401
        assert (
            await client.post("/v1/me/muse/connection", headers={"Cookie": "fixture-session=owner"})
        ).status_code == 403
        repository.connect.assert_not_awaited()
        issued = await client.post(
            "/v1/me/muse/connection",
            headers={
                "Cookie": "fixture-session=owner",
                "Origin": ORIGIN,
                "X-Fixture-Csrf": "verified",
            },
        )
        assert issued.status_code == 201
        token = issued.json()["token"]
        assert issued.headers["cache-control"] == "no-store, max-age=0"
        assert repository.connect.await_args.args[:2] == (
            tenant,
            hashlib.sha256(token.encode()).hexdigest(),
        )
        # A connector key never authenticates the account or grants key-issuance authority.
        for method, path in [
            ("GET", "/v1/me"),
            ("POST", "/v1/me/muse/connection"),
            ("GET", "/v1/me/muse/batches"),
        ]:
            assert (
                await client.request(method, path, headers={"Authorization": "Bearer " + token})
            ).status_code == 401


async def test_connector_requires_bearer_and_revocation_takes_effect(monkeypatch):
    app, repository, tenant = muse_app(monkeypatch)
    token, _ = await app.state.container.muse.connect(tenant)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        path = "/v1/muse/connector/batches"
        for headers in (
            {},
            {"Cookie": "fixture-session=owner"},
            {"X-EC-Tenant": str(tenant)},
            {"Authorization": "Basic " + token},
            {"Authorization": "Bearer guessed"},
        ):
            assert (await client.get(path, headers=headers)).status_code == 401
        repository.batches.assert_not_awaited()
        assert (
            await client.get(
                path, headers={"Authorization": "Bearer " + token, "X-EC-Tenant": str(uuid4())}
            )
        ).status_code == 200
        repository.batches.assert_awaited_once_with(tenant)
        missing = await client.get(
            path + "/" + str(uuid4()), headers={"Authorization": "Bearer " + token}
        )
        assert missing.status_code == 404
        await app.state.container.muse.repository.revoke(tenant)
        assert (
            await client.get(path, headers={"Authorization": "Bearer " + token})
        ).status_code == 401


async def test_connector_contract_exposes_only_selected_batch_operations(monkeypatch):
    app, _, _ = muse_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        result = await client.get("/v1/muse/openapi.json")
    assert result.status_code == 200
    schema = result.json()
    assert schema["servers"] == [{"url": ORIGIN}]
    assert sum(len(verbs) for verbs in schema["paths"].values()) == 4
    assert all(path.startswith("/v1/muse/connector/") for path in schema["paths"])
    assert schema["components"]["securitySchemes"]["MuseConnection"]["scheme"] == "bearer"
    for verbs in schema["paths"].values():
        for operation in verbs.values():
            assert operation["security"] == [{"MuseConnection": []}]
    assert "/v1/me/api-keys" not in app.openapi()["paths"]
    assert "/v1/me/muse/batches" in app.openapi()["paths"]


async def test_batch_body_cannot_set_tenant_or_provider_url(monkeypatch):
    app, repository, _ = muse_app(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        result = await client.post(
            "/v1/me/muse/batches",
            headers={
                "Cookie": "fixture-session=owner",
                "Origin": ORIGIN,
                "X-Fixture-Csrf": "verified",
            },
            json={
                "request_id": str(uuid4()),
                "event_ids": [str(uuid4())],
                "tenant_id": str(uuid4()),
                "registration_url": "https://evil.test/",
            },
        )
    assert result.status_code == 422
    repository.create.assert_not_awaited()


async def test_erasure_racing_a_connector_request_returns_auth_failure(monkeypatch):
    app, repository, tenant = muse_app(monkeypatch)
    token, _ = await app.state.container.muse.connect(tenant)
    repository.batches.side_effect = AuthenticationFailedError("private erasure detail")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        result = await client.get(
            "/v1/muse/connector/batches", headers={"Authorization": "Bearer " + token}
        )
    assert result.status_code == 401
    assert "private erasure detail" not in result.text


@pytest.mark.parametrize("accepted, expected_status", [(False, 428), (True, 200)])
async def test_connector_obeys_current_legal_acceptance(monkeypatch, accepted, expected_status):
    app, repository, tenant = muse_app(monkeypatch)
    app.state.settings = Settings(
        _env_file=None,
        mock_cloud=False,
        release_profile="discovery",
        muse_enabled=True,
        public_base_url=ORIGIN,
        identity_platform_enabled=True,
        identity_platform_project_id="events-fixture",
        identity_platform_api_key="restricted-browser-key-fixture",
        identity_platform_auth_domain="events-fixture.firebaseapp.com",
        identity_platform_google_client_id="123456789-fixture.apps.googleusercontent.com",
        identity_platform_providers=("google.com",),
        signup_terms_version="2026-10-05",
        signup_terms_url=ORIGIN + "/terms",
        signup_privacy_version="2026-10-05",
        signup_privacy_url=ORIGIN + "/privacy",
    )
    accounts = SimpleNamespace(has_accepted=AsyncMock(return_value=accepted))
    app.state.container.consumer_accounts = accounts
    token, _ = await app.state.container.muse.connect(tenant)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        result = await client.get(
            "/v1/muse/connector/batches", headers={"Authorization": "Bearer " + token}
        )
    assert result.status_code == expected_status
    assert result.headers["cache-control"] == "no-store, max-age=0"
    accounts.has_accepted.assert_awaited_once_with(tenant, app.state.settings.consumer_legal_policy)
    if accepted:
        repository.batches.assert_awaited_once_with(tenant)
    else:
        repository.batches.assert_not_awaited()
