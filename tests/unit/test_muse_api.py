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
from events_concierge.domain.muse import (
    MuseConflictError,
    MuseNotFoundError,
    SignupRegistration,
    SignupRegistrations,
)
from events_concierge.ports.auth import AuthenticationFailedError, CsrfVerificationFailedError

ORIGIN = "https://events.example.test"


@pytest.mark.parametrize("profile", ["full", "discovery"])
@pytest.mark.parametrize("explicitly_disabled", [False, True])
async def test_disabled_muse_omits_routes_before_authentication_or_io(
    monkeypatch,
    profile,
    explicitly_disabled,
):
    monkeypatch.delenv("EC_MUSE_ENABLED", raising=False)
    settings = Settings(
        _env_file=None,
        release_profile=profile,
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
            ("GET", "/v1/me/muse/registrations"),
            ("POST", "/v1/me/muse/registrations/seen"),
            ("POST", "/v1/me/muse/batches"),
            ("GET", "/v1/me/muse/registrations"),
            ("POST", "/v1/me/muse/registrations"),
            ("POST", "/v1/me/muse/registrations/seen"),
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
        _env_file=None,
        mock_cloud=True,
        release_profile="discovery",
        public_base_url=ORIGIN,
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
        repository.batches.assert_awaited_once_with(tenant, 50, None)
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


async def test_registration_reads_require_owner_and_validate_paging(monkeypatch):
    app, repository, tenant = muse_app(monkeypatch)
    repository.registrations.return_value = SignupRegistrations(items=[], total=0, unread_count=0)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        path = "/v1/me/muse/registrations"
        assert (await client.get(path)).status_code == 401
        owner = {"Cookie": "fixture-session=owner"}
        result = await client.get(path, headers=owner)
        assert result.status_code == 200 and result.json()["next_cursor"] is None
        assert result.headers["cache-control"] == "no-store, max-age=0"
        repository.registrations.assert_awaited_once_with(tenant, 50, None)
        for query in ("limit=0", "limit=101", "cursor=malformed"):
            assert (await client.get(path + "?" + query, headers=owner)).status_code == 422
        repository.registrations.side_effect = MuseNotFoundError(
            "signup registration cursor not found"
        )
        assert (
            await client.get(path + "?cursor=" + str(uuid4()), headers=owner)
        ).status_code == 404


async def test_queue_response_and_ack_errors_use_the_owner_registration_contract(monkeypatch):
    app, repository, tenant = muse_app(monkeypatch)
    event_id = (
        app.state.container.catalog.get_browse_event.return_value.canonical_event.canonical_event_id
    )
    batch = await app.state.container.muse.prepare(tenant, uuid4(), [event_id])
    registration = SignupRegistration(
        **batch.items[0].model_dump(),
        batch_id=batch.batch_id,
        created_at=batch.created_at,
        version=1,
        unread=False,
    )
    repository.queue_registration.side_effect = [MuseNotFoundError(), registration]
    headers = {"Cookie": "fixture-session=owner", "Origin": ORIGIN, "X-Fixture-Csrf": "verified"}
    payload = {"request_id": str(uuid4()), "event_id": str(event_id)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        path = "/v1/me/muse/registrations"
        result = await client.post(path, json=payload, headers=headers)
        assert result.status_code == 201 and result.json() == registration.model_dump(mode="json")
        assert result.headers["cache-control"] == "no-store, max-age=0"
        repository.queue_registration.side_effect = MuseConflictError("different selection")
        assert (await client.post(path, json=payload, headers=headers)).status_code == 409
        for error, code in (
            (ValueError("future version"), 422),
            (MuseNotFoundError("unknown task"), 404),
        ):
            repository.see_registrations.side_effect = error
            result = await client.post(
                path + "/seen",
                json={"items": [{"event_id": str(event_id), "version": 2}]},
                headers=headers,
            )
            assert result.status_code == code


async def test_registration_writes_require_owner_csrf_and_reject_injected_facts(monkeypatch):
    app, repository, _ = muse_app(monkeypatch)
    payload = {"request_id": str(uuid4()), "event_id": str(uuid4())}
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        path = "/v1/me/muse/registrations"
        assert (await client.post(path, json=payload)).status_code == 401
        assert (
            await client.post(path, json=payload, headers={"Cookie": "fixture-session=owner"})
        ).status_code == 403
        headers = {
            "Cookie": "fixture-session=owner",
            "Origin": ORIGIN,
            "X-Fixture-Csrf": "verified",
        }
        assert (
            await client.post(
                path, json={**payload, "registration_url": "https://evil.test"}, headers=headers
            )
        ).status_code == 422
        seen = path + "/seen"
        assert (await client.post(seen, json={"items": []})).status_code == 401
        assert (
            await client.post(seen, json={"items": []}, headers={"Cookie": "fixture-session=owner"})
        ).status_code == 403
        assert (
            await client.post(
                seen, json={"items": [{"event_id": str(uuid4()), "version": 0}]}, headers=headers
            )
        ).status_code == 422
        assert (await client.post(seen, json={"items": []}, headers=headers)).status_code == 204
    repository.queue_registration.assert_not_awaited()
    repository.see_registrations.assert_awaited_once()


async def test_connector_pagination_preserves_the_four_operation_contract(monkeypatch):
    app, repository, tenant = muse_app(monkeypatch)
    token, _ = await app.state.container.muse.connect(tenant)
    cursor = uuid4()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        headers = {"Authorization": "Bearer " + token}
        path = "/v1/muse/connector/batches"
        result = await client.get(path + f"?limit=100&cursor={cursor}", headers=headers)
        assert result.status_code == 200 and result.json() == []
        repository.batches.assert_awaited_once_with(tenant, 100, cursor)
        assert (await client.get(path + "?limit=101", headers=headers)).status_code == 422
        repository.batches.side_effect = MuseNotFoundError("signup batch cursor not found")
        assert (await client.get(path + f"?cursor={uuid4()}", headers=headers)).status_code == 404


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
        repository.batches.assert_awaited_once_with(tenant, 50, None)
    else:
        repository.batches.assert_not_awaited()
