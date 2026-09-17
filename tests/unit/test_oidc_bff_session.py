"""Production-shaped OIDC BFF session and cookie-bound CSRF coverage."""

from __future__ import annotations

import hashlib
import importlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from jwt import PyJWK
from pydantic import ValidationError

from events_concierge.adapters.oidc.auth import OidcJwtAuthContext
from events_concierge.adapters.oidc.session import OidcBffSessionAdapter
from events_concierge.application.tenant_effects import DirectTenantEffectAuthority
from events_concierge.composition import _build_identity_boundaries
from events_concierge.config import Settings
from events_concierge.domain.account_erasure import AccountErasureSnapshot, AccountErasureStatus
from events_concierge.domain.credentials import Tenant
from events_concierge.ports.auth import (
    AuthenticationFailedError,
    BrowserIdentity,
    BrowserLoginCompletion,
    BrowserLoginStart,
    BrowserSessionCredentials,
    BrowserSessionUnavailableError,
    CsrfVerificationFailedError,
    RecentAuthenticationRequiredError,
)

app_module = importlib.import_module("events_concierge.api.app")
session_module = importlib.import_module("events_concierge.adapters.oidc.session")

_ISSUER = "https://identity.example.test"
_CLIENT_ID = "events-concierge-web"
_CLIENT_SECRET = "fixture-client-secret"
_TENANT_CLAIM = "https://events.example.test/tenant_id"
_ORIGIN = "https://events.example.test"
_KEY_ID = "bff-fixture-key"
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65_537, key_size=2048)
_JWK_PAYLOAD = jwt.algorithms.RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key(), as_dict=True)
assert isinstance(_JWK_PAYLOAD, dict)
_JWK_PAYLOAD["kid"] = _KEY_ID
_PUBLIC_JWK = PyJWK.from_dict(_JWK_PAYLOAD)
_MAX_TENANT_SESSIONS = 32


class _MemorySessionStore:
    def __init__(self) -> None:
        self.logins: dict[str, str] = {}
        self.sessions: dict[str, str] = {}
        self.tenant_sessions: dict[UUID, set[str]] = {}
        self.fenced_tenants: set[UUID] = set()
        self.closed = False

    async def create_login(self, token: str, payload: str, ttl_seconds: int) -> bool:
        assert ttl_seconds == 600
        if token in self.logins:
            return False
        self.logins[token] = payload
        return True

    async def consume_login(self, token: str) -> str | None:
        return self.logins.pop(token, None)

    async def create_session(
        self,
        token: str,
        payload: str,
        ttl_seconds: int,
        tenant_id: UUID,
    ) -> bool:
        assert ttl_seconds == 28_800
        if (
            token in self.sessions
            or tenant_id in self.fenced_tenants
            or len(self.tenant_sessions.get(tenant_id, ())) >= _MAX_TENANT_SESSIONS
        ):
            return False
        self.sessions[token] = payload
        self.tenant_sessions.setdefault(tenant_id, set()).add(token)
        return True

    async def get_session(self, token: str) -> str | None:
        return self.sessions.get(token)

    async def delete_session(self, token: str) -> None:
        raw = self.sessions.pop(token, None)
        if raw is None:
            return
        tenant_id = UUID(json.loads(raw)["tenant_id"])
        sessions = self.tenant_sessions.get(tenant_id)
        if sessions is not None:
            sessions.discard(token)

    async def mark_recent_auth(
        self,
        token: str,
        tenant_id: UUID,
        subject: str,
        authenticated_at: int,
    ) -> bool:
        raw = self.sessions.get(token)
        if raw is None:
            return False
        record = json.loads(raw)
        if record.get("tenant_id") != str(tenant_id) or record.get("subject") != subject:
            return False
        record["recent_auth_at"] = authenticated_at
        self.sessions[token] = json.dumps(record, separators=(",", ":"), sort_keys=True)
        return True

    async def revoke_tenant_sessions(self, tenant_id: UUID) -> None:
        self.fenced_tenants.add(tenant_id)
        for token in self.tenant_sessions.pop(tenant_id, set()):
            self.sessions.pop(token, None)

    async def is_ready(self) -> bool:
        return True

    async def aclose(self) -> None:
        self.closed = True


class _UnavailableSessionStore(_MemorySessionStore):
    async def get_session(self, token: str) -> str | None:
        del token
        raise BrowserSessionUnavailableError("fixture unavailable")

    async def is_ready(self) -> bool:
        return False


def _identity_token(
    tenant_id: UUID,
    nonce: str,
    *,
    subject: str = "oidc|fixture-user",
    authenticated_at: datetime | None = None,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": _ISSUER,
        "sub": subject,
        "aud": _CLIENT_ID,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "nonce": nonce,
        _TENANT_CLAIM: str(tenant_id),
    }
    if authenticated_at is not None:
        claims["auth_time"] = int(authenticated_at.timestamp())
    return jwt.encode(
        claims,
        _PRIVATE_KEY,
        algorithm="RS256",
        headers={"kid": _KEY_ID},
    )


def _verifier() -> OidcJwtAuthContext:
    return OidcJwtAuthContext(
        issuer=_ISSUER,
        audience=_CLIENT_ID,
        jwks_url=f"{_ISSUER}/jwks.json",
        tenant_claim=_TENANT_CLAIM,
        signing_key_resolver=lambda _token: _PUBLIC_JWK,
    )


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "mock_cloud": False,
        "public_base_url": _ORIGIN,
        "oidc_bff_enabled": True,
        "oidc_issuer": _ISSUER,
        "oidc_authorization_url": f"{_ISSUER}/authorize",
        "oidc_token_url": f"{_ISSUER}/oauth/token",
        "oidc_jwks_url": f"{_ISSUER}/jwks.json",
        "oidc_client_id": _CLIENT_ID,
        "oidc_client_secret": _CLIENT_SECRET,
        "oidc_tenant_claim": _TENANT_CLAIM,
    }
    values.update(overrides)
    return Settings(**values)


async def test_oidc_bff_login_session_csrf_and_revocation_are_one_coherent_boundary() -> None:
    tenant_id = uuid4()
    store = _MemorySessionStore()
    nonce: dict[str, str] = {}
    token_requests: list[httpx.Request] = []

    def exchange(request: httpx.Request) -> httpx.Response:
        token_requests.append(request)
        return httpx.Response(200, json={"id_token": _identity_token(tenant_id, nonce["value"])})

    client = httpx.AsyncClient(transport=httpx.MockTransport(exchange))
    adapter = OidcBffSessionAdapter(
        issuer=_ISSUER,
        authorization_url=f"{_ISSUER}/authorize",
        token_url=f"{_ISSUER}/oauth/token",
        jwks_url=f"{_ISSUER}/jwks.json",
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        tenant_claim=_TENANT_CLAIM,
        redirect_uri=f"{_ORIGIN}/auth/callback",
        trusted_origin=_ORIGIN,
        redis_url="redis://unused.test/0",
        store=store,
        http_client=client,
        identity_verifier=_verifier(),
    )

    login = await adapter.start_login("/app#/plans")
    authorization = urlsplit(login.authorization_url)
    query = parse_qs(authorization.query)
    nonce["value"] = query["nonce"][0]
    transaction = json.loads(store.logins[login.transaction_token])
    expected_challenge = jwt.utils.base64url_encode(
        hashlib.sha256(transaction["verifier"].encode("ascii")).digest()
    ).decode("ascii")

    completion = await adapter.complete_login(
        {"Cookie": f"{adapter.login_cookie_name}={login.transaction_token}"},
        code="fixture-authorization-code",
        state=query["state"][0],
    )
    credentials = await adapter.issue_session(completion.identity)
    cookie = (
        f"{adapter.session_cookie_name}={credentials.session_token}; "
        f"{adapter.csrf_cookie_name}={credentials.csrf_token}"
    )

    assert authorization.scheme == "https"
    assert authorization.netloc == "identity.example.test"
    assert query["response_type"] == ["code"]
    assert query["scope"] == ["openid"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [expected_challenge]
    assert completion == BrowserLoginCompletion(
        identity=BrowserIdentity(tenant_id, "oidc|fixture-user"),
        return_to="/app#/plans",
    )
    assert await adapter.resolve_tenant_id({"Cookie": cookie}) == tenant_id
    await adapter.verify_state_change(
        tenant_id,
        {
            "Cookie": cookie,
            "Origin": _ORIGIN,
            adapter.csrf_header_name: credentials.csrf_token,
        },
    )
    assert len(token_requests) == 1
    request = token_requests[0]
    form = parse_qs(request.content.decode("ascii"))
    assert form["code"] == ["fixture-authorization-code"]
    assert form["code_verifier"] == [transaction["verifier"]]
    assert form["redirect_uri"] == [f"{_ORIGIN}/auth/callback"]
    assert "client_id" not in form
    assert "client_secret" not in form
    assert request.headers["authorization"].startswith("Basic ")

    for invalid_headers in (
        {
            "Cookie": cookie,
            "Origin": "https://attacker.example",
            adapter.csrf_header_name: credentials.csrf_token,
        },
        {"Cookie": cookie, "Origin": _ORIGIN, adapter.csrf_header_name: "x" * 43},
        {
            "Cookie": f"{adapter.session_cookie_name}={credentials.session_token}",
            "Origin": _ORIGIN,
            adapter.csrf_header_name: credentials.csrf_token,
        },
    ):
        with pytest.raises(CsrfVerificationFailedError):
            await adapter.verify_state_change(tenant_id, invalid_headers)

    with pytest.raises(AuthenticationFailedError):
        await adapter.complete_login(
            {"Cookie": f"{adapter.login_cookie_name}={login.transaction_token}"},
            code="fixture-authorization-code",
            state=query["state"][0],
        )

    await adapter.revoke_session({"Cookie": cookie})
    with pytest.raises(AuthenticationFailedError):
        await adapter.resolve_tenant_id({"Cookie": cookie})
    await adapter.aclose()
    await client.aclose()
    assert store.closed is True


async def test_oidc_bff_reauthentication_is_purpose_session_and_identity_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    store = _MemorySessionStore()
    token_context: dict[str, str] = {}
    now = datetime.now(UTC).replace(microsecond=0)
    monkeypatch.setattr(session_module, "wall_time", now.timestamp)

    def exchange(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id_token": _identity_token(
                    tenant_id,
                    token_context["nonce"],
                    authenticated_at=now,
                )
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(exchange))
    adapter = OidcBffSessionAdapter(
        issuer=_ISSUER,
        authorization_url=f"{_ISSUER}/authorize",
        token_url=f"{_ISSUER}/oauth/token",
        jwks_url=f"{_ISSUER}/jwks.json",
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        tenant_claim=_TENANT_CLAIM,
        redirect_uri=f"{_ORIGIN}/auth/callback",
        trusted_origin=_ORIGIN,
        redis_url="redis://unused.test/0",
        store=store,
        http_client=client,
        identity_verifier=_verifier(),
    )
    credentials = await adapter.issue_session(BrowserIdentity(tenant_id, "oidc|fixture-user"))
    cookie = (
        f"{adapter.session_cookie_name}={credentials.session_token}; "
        f"{adapter.csrf_cookie_name}={credentials.csrf_token}"
    )
    authority = {
        "Cookie": cookie,
        "Origin": _ORIGIN,
        adapter.csrf_header_name: credentials.csrf_token,
    }

    with pytest.raises(RecentAuthenticationRequiredError):
        await adapter.verify_recent_auth(tenant_id, authority, max_age_seconds=300)

    reauthentication = await adapter.start_reauthentication(
        tenant_id,
        authority,
        return_to="/app#/settings",
    )
    query = parse_qs(urlsplit(reauthentication.authorization_url).query)
    token_context["nonce"] = query["nonce"][0]
    assert query["prompt"] == ["login"]
    assert query["max_age"] == ["0"]
    transaction = json.loads(store.logins[reauthentication.transaction_token])
    assert transaction["purpose"] == "account_erasure"
    assert transaction["tenant_id"] == str(tenant_id)
    assert transaction["subject"] == "oidc|fixture-user"
    assert str(tenant_id) not in transaction["bound_session_hash"]

    completion = await adapter.complete_login(
        {
            **authority,
            "Cookie": f"{cookie}; {adapter.login_cookie_name}={reauthentication.transaction_token}",
        },
        code="fixture-step-up-code",
        state=query["state"][0],
    )
    assert completion.reauthenticated is True
    assert completion.return_to == "/app#/settings"
    assert await adapter.resolve_tenant_id(authority) == tenant_id
    await adapter.verify_recent_auth(tenant_id, authority, max_age_seconds=300)

    monkeypatch.setattr(session_module, "wall_time", lambda: now.timestamp() + 361)
    with pytest.raises(RecentAuthenticationRequiredError):
        await adapter.verify_recent_auth(tenant_id, authority, max_age_seconds=300)
    with pytest.raises(AuthenticationFailedError):
        await adapter.complete_login(
            {
                **authority,
                "Cookie": (
                    f"{cookie}; {adapter.login_cookie_name}="
                    f"{reauthentication.transaction_token}"
                ),
            },
            code="fixture-step-up-code",
            state=query["state"][0],
        )
    await adapter.aclose()
    await client.aclose()


@pytest.mark.parametrize(
    "return_to",
    (
        "https://attacker.example/app",
        "//attacker.example/app",
        "/auth/callback",
        "/app\\redirect",
        "/app\nredirect",
    ),
)
async def test_oidc_bff_rejects_open_or_non_application_return_paths(return_to: str) -> None:
    store = _MemorySessionStore()
    adapter = OidcBffSessionAdapter(
        issuer=_ISSUER,
        authorization_url=f"{_ISSUER}/authorize",
        token_url=f"{_ISSUER}/oauth/token",
        jwks_url=f"{_ISSUER}/jwks.json",
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        tenant_claim=_TENANT_CLAIM,
        redirect_uri=f"{_ORIGIN}/auth/callback",
        trusted_origin=_ORIGIN,
        redis_url="redis://unused.test/0",
        store=store,
        identity_verifier=_verifier(),
    )

    with pytest.raises(ValueError, match="return path"):
        await adapter.start_login(return_to)

    assert store.logins == {}
    await adapter.aclose()


async def test_oidc_bff_preserves_session_store_outage_as_availability_failure() -> None:
    adapter = OidcBffSessionAdapter(
        issuer=_ISSUER,
        authorization_url=f"{_ISSUER}/authorize",
        token_url=f"{_ISSUER}/oauth/token",
        jwks_url=f"{_ISSUER}/jwks.json",
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        tenant_claim=_TENANT_CLAIM,
        redirect_uri=f"{_ORIGIN}/auth/callback",
        trusted_origin=_ORIGIN,
        redis_url="redis://unused.test/0",
        store=_UnavailableSessionStore(),
        identity_verifier=_verifier(),
    )
    headers = {"Cookie": f"{adapter.session_cookie_name}={'s' * 43}"}

    assert await adapter.is_ready() is False
    with pytest.raises(BrowserSessionUnavailableError):
        await adapter.resolve_tenant_id(headers)
    with pytest.raises(BrowserSessionUnavailableError):
        await adapter.verify_state_change(uuid4(), headers)
    await adapter.aclose()


async def test_oidc_bff_settings_and_composition_fail_closed_for_partial_or_mixed_identity() -> (
    None
):
    with pytest.raises(ValidationError, match="cannot be enabled in mock-cloud mode"):
        _settings(mock_cloud=True)
    with pytest.raises(ValidationError, match="oidc_token_url"):
        _settings(oidc_token_url=None)
    with pytest.raises(ValidationError, match="oidc_client_secret"):
        _settings(oidc_client_secret=" ")
    with pytest.raises(ValidationError, match="same-origin /auth/login"):
        _settings(ui_auth_start_url="https://identity.example.test/authorize")

    settings = _settings()
    auth, csrf, lifecycle = _build_identity_boundaries(settings, None, None, None)
    assert lifecycle is not None
    assert auth is lifecycle
    assert csrf is lifecycle

    with pytest.raises(ValueError, match="cannot be combined"):
        _build_identity_boundaries(settings, auth, None, None)
    await lifecycle.aclose()


class _FakeBrowserSession:
    login_cookie_name = "__Host-ec_login"
    session_cookie_name = "__Host-ec_session"
    csrf_cookie_name = "__Host-ec_csrf"
    csrf_header_name = "X-EC-CSRF"
    login_ttl_seconds = 600
    session_ttl_seconds = 28_800

    def __init__(self, identity: BrowserIdentity) -> None:
        self.identity = identity
        self.ready = True
        self.revoke_calls = 0
        self.csrf_calls = 0
        self.issue_calls = 0
        self.recent = False

    async def start_login(self, return_to: str) -> BrowserLoginStart:
        assert return_to == "/app"
        return BrowserLoginStart(f"{_ISSUER}/authorize?fixture=1", "l" * 43)

    async def cancel_login(self, headers: Any, *, state: str) -> None:
        del headers, state

    async def start_reauthentication(
        self,
        tenant_id: UUID,
        headers: Any,
        *,
        return_to: str,
    ) -> BrowserLoginStart:
        assert tenant_id == self.identity.tenant_id
        assert self.session_cookie_name in headers["cookie"]
        assert return_to == "/app#/settings"
        return BrowserLoginStart(f"{_ISSUER}/authorize?prompt=login", "r" * 43)

    async def complete_login(
        self,
        headers: Any,
        *,
        code: str,
        state: str,
    ) -> BrowserLoginCompletion:
        assert self.login_cookie_name in headers["cookie"]
        assert (code, state) == ("code", "state")
        reauthenticated = f"{self.login_cookie_name}={'r' * 43}" in headers["cookie"]
        if reauthenticated:
            self.recent = True
        return BrowserLoginCompletion(
            self.identity,
            "/app#/settings" if reauthenticated else "/app#/plans",
            reauthenticated=reauthenticated,
        )

    async def issue_session(self, identity: BrowserIdentity) -> BrowserSessionCredentials:
        assert identity == self.identity
        self.issue_calls += 1
        return BrowserSessionCredentials("s" * 43, "c" * 43)

    async def resolve_tenant_id(self, headers: Any) -> UUID:
        assert self.session_cookie_name in headers["cookie"]
        return self.identity.tenant_id

    async def verify_state_change(self, tenant_id: UUID, headers: Any) -> None:
        assert tenant_id == self.identity.tenant_id
        assert headers["origin"] == _ORIGIN
        self.csrf_calls += 1

    async def revoke_session(self, headers: Any) -> None:
        del headers
        self.revoke_calls += 1

    async def verify_recent_auth(
        self,
        tenant_id: UUID,
        headers: Any,
        *,
        max_age_seconds: int,
    ) -> None:
        assert tenant_id == self.identity.tenant_id
        assert self.session_cookie_name in headers["cookie"]
        assert max_age_seconds == 300
        if not self.recent:
            raise RecentAuthenticationRequiredError("fixture recent auth required")

    async def revoke_tenant_sessions(self, tenant_id: UUID) -> None:
        assert tenant_id == self.identity.tenant_id
        self.revoke_calls += 1

    async def is_ready(self) -> bool:
        return self.ready

    async def aclose(self) -> None:
        return None


class _TenantRepository:
    def __init__(self, tenant: Tenant) -> None:
        self.tenant = tenant

    async def get(self, tenant_id: UUID) -> Tenant | None:
        return self.tenant if tenant_id == self.tenant.tenant_id else None


class _AccountErasureRepository:
    def __init__(self, tenant_id: UUID) -> None:
        self.tenant_id = tenant_id
        self.snapshot: AccountErasureSnapshot | None = None

    async def get(self, tenant_id: UUID) -> AccountErasureSnapshot | None:
        return self.snapshot if tenant_id == self.tenant_id else None

    async def begin(self, tenant_id: UUID, request_id: UUID) -> AccountErasureSnapshot:
        assert tenant_id == self.tenant_id
        self.snapshot = AccountErasureSnapshot(
            tenant_id=tenant_id,
            request_id=request_id,
            status=AccountErasureStatus.ERASING,
            workflow_ids=(),
            canonical_event_ids=(),
            workflow_target_count=0,
            calendar_target_count=0,
            calendar_binding_expected=False,
            external_effects_completed=False,
            workflows_completed=False,
            calendar_completed=False,
            browser_sessions_completed=False,
            credential_vault_completed=False,
            object_store_completed=False,
            retained_audit_rows=0,
        )
        return self.snapshot


async def test_bff_api_sets_secure_host_cookies_binds_subject_and_revokes_logout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = BrowserIdentity(uuid4(), "oidc|fixture-user")
    browser_session = _FakeBrowserSession(identity)
    tenant = Tenant(identity.tenant_id, identity.subject, "user@example.test", "relay@example.test")
    monkeypatch.setattr(app_module, "get_settings", _settings)
    app = app_module.create_app()
    app.state.container = SimpleNamespace(
        browser_session=browser_session,
        auth_context=browser_session,
        csrf_protection=browser_session,
        tenant_repo=_TenantRepository(tenant),
        tenant_effect_authority=DirectTenantEffectAuthority(),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=_ORIGIN,
        follow_redirects=False,
    ) as client:
        config = await client.get("/v1/ui-config")
        login = await client.get("/auth/login")
        callback = await client.get("/auth/callback?code=code&state=state")
        reauthentication = await client.post(
            "/auth/reauth",
            json={"return_to": "/app#/settings"},
            headers={"Origin": _ORIGIN, "X-EC-CSRF": "c" * 43},
        )
        logout = await client.post(
            "/auth/logout",
            headers={"Origin": _ORIGIN, "X-EC-CSRF": "c" * 43},
        )

    assert config.json()["auth_start_url"] == "/auth/login"
    assert config.json()["reauth_url"] == "/auth/reauth"
    assert config.json()["logout_url"] == "/auth/logout"
    assert config.json()["csrf_cookie_name"] == browser_session.csrf_cookie_name
    assert config.json()["csrf_header_name"] == browser_session.csrf_header_name
    assert login.status_code == 302
    assert login.headers["location"].startswith(f"{_ISSUER}/authorize")
    login_cookie = login.headers.get_list("set-cookie")[0]
    assert login_cookie.startswith(f"{browser_session.login_cookie_name}=")
    assert all(flag in login_cookie for flag in ("HttpOnly", "Path=/", "SameSite=lax", "Secure"))

    assert callback.status_code == 303
    assert callback.headers["location"] == "/app#/plans"
    callback_cookies = callback.headers.get_list("set-cookie")
    session_cookie = next(
        value for value in callback_cookies if value.startswith(browser_session.session_cookie_name)
    )
    csrf_cookie = next(
        value
        for value in callback_cookies
        if value.startswith(f"{browser_session.csrf_cookie_name}={'c' * 43}")
    )
    assert all(flag in session_cookie for flag in ("HttpOnly", "Path=/", "SameSite=lax", "Secure"))
    assert "HttpOnly" not in csrf_cookie
    assert all(flag in csrf_cookie for flag in ("Path=/", "SameSite=strict", "Secure"))
    assert callback.headers["cache-control"] == "no-store, max-age=0"
    assert callback.headers["referrer-policy"] == "no-referrer"

    assert reauthentication.status_code == 200
    assert reauthentication.json() == {
        "authorization_url": f"{_ISSUER}/authorize?prompt=login"
    }
    assert reauthentication.headers["cache-control"] == "no-store, max-age=0"
    reauthentication_cookie = reauthentication.headers.get_list("set-cookie")[0]
    assert reauthentication_cookie.startswith(
        f"{browser_session.login_cookie_name}={'r' * 43}"
    )

    assert logout.status_code == 204
    assert len(logout.headers.get_list("set-cookie")) == 3
    assert all("Max-Age=0" in value for value in logout.headers.get_list("set-cookie"))
    assert browser_session.issue_calls == 1
    assert browser_session.revoke_calls == 2
    assert browser_session.csrf_calls == 2


async def test_account_erasure_requires_typed_confirmation_recent_auth_and_clears_cookies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = BrowserIdentity(uuid4(), "oidc|fixture-user")
    browser_session = _FakeBrowserSession(identity)
    tenant = Tenant(identity.tenant_id, identity.subject, "user@example.test", "relay@example.test")
    erasure_repository = _AccountErasureRepository(identity.tenant_id)
    monkeypatch.setattr(app_module, "get_settings", _settings)
    app = app_module.create_app()
    app.state.container = SimpleNamespace(
        browser_session=browser_session,
        auth_context=browser_session,
        csrf_protection=browser_session,
        tenant_repo=_TenantRepository(tenant),
        tenant_effect_authority=DirectTenantEffectAuthority(),
        account_erasure_repo=erasure_repository,
    )
    authority = {"Origin": _ORIGIN, "X-EC-CSRF": "c" * 43}
    request_id = uuid4()

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=_ORIGIN,
        follow_redirects=False,
    ) as client:
        await client.get("/auth/login")
        await client.get("/auth/callback?code=code&state=state")
        missing_before_reauthentication = await client.post(
            "/v1/me/erasure-requests",
            json={"request_id": str(request_id)},
            headers=authority,
        )
        stale = await client.post(
            "/v1/me/erasure-requests",
            json={
                "request_id": str(request_id),
                "confirmation": "DELETE MY ACCOUNT",
            },
            headers=authority,
        )
        reauthentication = await client.post(
            "/auth/reauth",
            json={"return_to": "/app#/settings"},
            headers=authority,
        )
        assert reauthentication.status_code == 200
        callback = await client.get("/auth/callback?code=code&state=state")
        missing_confirmation = await client.post(
            "/v1/me/erasure-requests",
            json={"request_id": str(request_id)},
            headers=authority,
        )
        accepted = await client.post(
            "/v1/me/erasure-requests",
            json={
                "request_id": str(request_id),
                "confirmation": "DELETE MY ACCOUNT",
            },
            headers=authority,
        )

    assert missing_before_reauthentication.status_code == 428
    assert missing_confirmation.status_code == 422
    assert stale.status_code == 428
    assert stale.json() == {"detail": "recent sign-in required before account erasure"}
    assert callback.status_code == 303
    assert callback.headers["location"] == "/app#/settings"
    callback_cookies = callback.headers.get_list("set-cookie")
    assert len(callback_cookies) == 1
    assert callback_cookies[0].startswith(browser_session.login_cookie_name)
    assert "Max-Age=0" in callback_cookies[0]
    assert accepted.status_code == 202
    assert accepted.json()["request_id"] == str(request_id)
    assert accepted.json()["status"] == "erasing"
    cleared = accepted.headers.get_list("set-cookie")
    assert len(cleared) == 3
    assert all("Max-Age=0" in value for value in cleared)
    assert erasure_repository.snapshot is not None


async def test_bff_callback_rejects_a_signed_tenant_with_the_wrong_subject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = BrowserIdentity(uuid4(), "oidc|unexpected-user")
    browser_session = _FakeBrowserSession(identity)
    tenant = Tenant(
        identity.tenant_id, "oidc|bound-user", "user@example.test", "relay@example.test"
    )
    monkeypatch.setattr(app_module, "get_settings", _settings)
    app = app_module.create_app()
    app.state.container = SimpleNamespace(
        browser_session=browser_session,
        tenant_repo=_TenantRepository(tenant),
        tenant_effect_authority=DirectTenantEffectAuthority(),
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url=_ORIGIN,
        follow_redirects=False,
    ) as client:
        await client.get("/auth/login")
        callback = await client.get("/auth/callback?code=code&state=state")

    assert callback.status_code == 401
    assert callback.json() == {"detail": "login could not be verified"}
    assert browser_session.issue_calls == 0
    assert browser_session.revoke_calls == 0


async def test_bff_readiness_requires_the_shared_session_control_plane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = BrowserIdentity(uuid4(), "oidc|fixture-user")
    browser_session = _FakeBrowserSession(identity)

    async def ready(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(app_module, "get_settings", _settings)
    monkeypatch.setattr(app_module, "_database_is_ready", ready)
    monkeypatch.setattr(app_module, "_temporal_is_reachable", ready)
    app = app_module.create_app()
    app.state.container = SimpleNamespace(browser_session=browser_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        available = await client.get("/readyz")
        browser_session.ready = False
        unavailable = await client.get("/readyz")

    assert available.status_code == 200
    assert available.json()["components"]["identity"] == "ready"
    assert unavailable.status_code == 503
    assert unavailable.json()["status"] == "not_ready"
    assert unavailable.json()["components"]["identity"] == "unavailable"
