"""Consumer identity, browser binding, legal acceptance and erasure fail closed."""

from __future__ import annotations

import importlib
from time import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from tests.unit.test_oidc_bff_session import _MemorySessionStore

from events_concierge.adapters.identity_platform import (
    IdentityPlatformBrowserSessionAdapter,
    IdentityPlatformVerifier,
    verified_identity,
)
from events_concierge.application.tenant_effects import DirectTenantEffectAuthority
from events_concierge.config import Settings
from events_concierge.domain.consumer_identity import LegalPolicy, VerifiedConsumerIdentity
from events_concierge.domain.credentials import Tenant
from events_concierge.ports.auth import (
    AuthenticationFailedError,
    BrowserIdentity,
    BrowserSessionUnavailableError,
    ConsumerSignInFailureReason,
    ConsumerSignInRejectedError,
    RecentAuthenticationRequiredError,
)

identity_module = importlib.import_module("events_concierge.adapters.identity_platform")
app_module = importlib.import_module("events_concierge.api.app")
_PROJECT = "events-identity-test"
_ORIGIN = "https://events.example.test"
_PROVIDERS = frozenset({"google.com", "apple.com"})
_VALIDATION_NOW = 1_700_000_000
_GOOGLE_CLIENT = "123456789-consumer-web.apps.googleusercontent.com"


def _claims(**changes: Any) -> dict[str, Any]:
    return {
        "iss": f"https://securetoken.google.com/{_PROJECT}",
        "aud": _PROJECT,
        "sub": "consumer-123",
        "email": "consumer@example.test",
        "email_verified": True,
        "auth_time": int(time()),
        "firebase": {
            "sign_in_provider": "google.com",
            "identities": {"google.com": ["google-123"]},
        },
        **changes,
    }


def _google_claims(**changes: Any) -> dict[str, Any]:
    return {
        "sub": "google-123",
        "email": "consumer@example.test",
        "email_verified": True,
        **changes,
    }


def _settings(**changes: Any) -> Settings:
    return Settings(
        _env_file=None,
        **{
            "mock_cloud": False,
            "env": "staging",
            "release_profile": "discovery",
            "identity_platform_enabled": True,
            "identity_platform_project_id": _PROJECT,
            "identity_platform_google_client_id": _GOOGLE_CLIENT,
            "identity_platform_api_key": "restricted-browser-key-fixture",
            "identity_platform_auth_domain": f"{_PROJECT}.firebaseapp.com",
            "identity_platform_providers": ("google.com", "apple.com"),
            "public_base_url": _ORIGIN,
            "signup_terms_version": "2026-10-05",
            "signup_terms_url": f"{_ORIGIN}/terms",
            "signup_privacy_version": "2026-10-05",
            "signup_privacy_url": f"{_ORIGIN}/privacy",
            **changes,
        },
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://securetoken.google.com/another-project"},
        {"aud": "another-project"},
        {"aud": [_PROJECT]},
        {"firebase": {"sign_in_provider": "password"}},
        {"firebase": {"sign_in_provider": "anonymous"}},
        {"firebase": {"sign_in_provider": "google.com", "tenant": "other-tenant"}},
        {"firebase": {"sign_in_provider": []}},
        {"email_verified": False},
        {"email_verified": "true"},
        {"email": "private\n@example.test"},
        {"email": "not-an-email"},
        {"sub": "consumer 123"},
        {"sub": ""},
        {"sub": "x" * 129},
        {"auth_time": True},
        {"auth_time": _VALIDATION_NOW - 301},
        {"auth_time": _VALIDATION_NOW + 120},
    ],
)
def test_unverified_stale_or_different_authority_cannot_select_an_account(
    changes: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A delayed full-suite run must not turn the future-token fixture into a valid token.
    monkeypatch.setattr(identity_module, "time", lambda: _VALIDATION_NOW)
    claims = _claims(auth_time=_VALIDATION_NOW) | changes
    with pytest.raises(AuthenticationFailedError):
        verified_identity(
            claims,
            _PROJECT,
            _PROVIDERS,
            google_claims=_google_claims(email_verified=claims.get("email_verified")),
        )


def test_apple_verified_relay_email_is_supported_without_email_account_linking() -> None:
    google = verified_identity(_claims(), _PROJECT, _PROVIDERS, google_claims=_google_claims())
    changed_email = verified_identity(
        _claims(email="another@example.test"),
        _PROJECT,
        _PROVIDERS,
        google_claims=_google_claims(email="another@example.test"),
    )
    apple = verified_identity(
        _claims(
            sub="apple-123",
            email="private@privaterelay.appleid.com",
            firebase={"sign_in_provider": "apple.com"},
        ),
        _PROJECT,
        _PROVIDERS,
    )
    same_email = verified_identity(
        _claims(sub="other-user"), _PROJECT, _PROVIDERS, google_claims=_google_claims()
    )
    assert google.tenant_id == changed_email.tenant_id
    assert google.tenant_id != same_email.tenant_id != apple.tenant_id
    assert google.tenant_id.version == 8
    assert apple.provider == "apple.com"


@pytest.mark.parametrize(
    "changes",
    [
        {"mock_cloud": True},
        {"oidc_bff_enabled": True},
        {"identity_platform_project_id": None},
        {"identity_platform_api_key": None},
        {"identity_platform_google_client_id": None},
        {"identity_platform_google_client_id": "another-client"},
        {"identity_platform_auth_domain": "attacker.firebaseapp.com"},
        {"identity_platform_providers": ()},
        {"identity_platform_providers": ("google.com", "google.com")},
        {"public_base_url": "http://events.example.test"},
        {"public_base_url": f"{_ORIGIN}/path"},
        {"signup_terms_version": None},
        {"signup_privacy_url": "http://events.example.test/privacy"},
        {"signup_terms_url": "https://secret@events.example.test/terms"},
    ],
)
def test_incomplete_or_unsafe_production_signup_configuration_cannot_start(changes: Any) -> None:
    with pytest.raises(ValidationError):
        _settings(**changes)


async def test_official_sdk_is_fixed_to_project_and_always_checks_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = IdentityPlatformVerifier(_PROJECT, tuple(_PROVIDERS), _GOOGLE_CLIENT)
    calls = []

    def verify(token: str, *, app: Any, check_revoked: bool) -> Any:
        calls.append((token, app.project_id, check_revoked))
        return _claims()

    monkeypatch.setattr(identity_module.auth, "verify_id_token", verify)
    monkeypatch.setattr(verifier, "_verify_google", AsyncMock(return_value=_google_claims()))
    assert (await verifier.verify("opaque-id-token", google_id_token="google-proof")).tenant_id
    assert calls == [("opaque-id-token", _PROJECT, True)]
    monkeypatch.setenv("FIREBASE_AUTH_EMULATOR_HOST", "127.0.0.1:9099")
    with pytest.raises(ValueError, match="emulator"):
        IdentityPlatformVerifier(_PROJECT, tuple(_PROVIDERS), _GOOGLE_CLIENT)


@pytest.mark.parametrize(
    "failure", ["InvalidIdTokenError", "RevokedIdTokenError", "UserDisabledError"]
)
async def test_invalid_or_revoked_sdk_results_never_issue_authority(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    verifier = IdentityPlatformVerifier(_PROJECT, tuple(_PROVIDERS), _GOOGLE_CLIENT)

    def rejected(*args: Any, **kwargs: Any) -> Any:
        raise getattr(identity_module.auth, failure)("fixture rejection")

    monkeypatch.setattr(identity_module.auth, "verify_id_token", rejected)
    with pytest.raises(AuthenticationFailedError):
        await verifier.verify("rejected-token")


async def test_active_cookie_checks_disabled_user_and_revocation_without_stale_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = IdentityPlatformVerifier(_PROJECT, tuple(_PROVIDERS), _GOOGLE_CLIENT)
    now = int(time())
    user = SimpleNamespace(disabled=False, tokens_valid_after_timestamp=0)
    calls = []

    def get_user(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return user

    monkeypatch.setattr(identity_module.auth, "get_user", get_user)
    await verifier.check_session("consumer-123", now)
    await verifier.check_session("consumer-123", now)
    assert len(calls) == 1
    verifier._revocation_cache.clear()
    user.disabled = True
    with pytest.raises(AuthenticationFailedError):
        await verifier.check_session("consumer-123", now)
    verifier._revocation_cache.clear()
    user.disabled = False
    user.tokens_valid_after_timestamp = (now + 1) * 1000
    with pytest.raises(AuthenticationFailedError):
        await verifier.check_session("consumer-123", now)
    verifier._revocation_cache.clear()

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise OSError("fixture service unavailable")

    monkeypatch.setattr(identity_module.auth, "get_user", unavailable)
    with pytest.raises(BrowserSessionUnavailableError):
        await verifier.check_session("consumer-123", now)


async def test_missing_cloud_credentials_fail_closed_as_service_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = IdentityPlatformVerifier(_PROJECT, tuple(_PROVIDERS), _GOOGLE_CLIENT)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise identity_module.GoogleAuthError("fixture credentials unavailable")

    monkeypatch.setattr(identity_module.auth, "verify_id_token", unavailable)
    with pytest.raises(BrowserSessionUnavailableError):
        await verifier.verify("fixture-token")


async def test_managed_erasure_retries_missing_users_but_preserves_outages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = IdentityPlatformVerifier(_PROJECT, tuple(_PROVIDERS), _GOOGLE_CLIENT)
    calls = []

    def deleted(uid: str, *, app: Any) -> None:
        calls.append((uid, app.project_id))
        raise identity_module.auth.UserNotFoundError("fixture missing user")

    monkeypatch.setattr(identity_module.auth, "delete_user", deleted)
    verifier._revocation_cache["consumer-123"] = (0, False, 0)
    await verifier.delete_account("consumer-123")
    await verifier.delete_account("consumer-123")
    assert calls == [("consumer-123", _PROJECT)] * 2
    assert "consumer-123" not in verifier._revocation_cache

    def unavailable(*args: Any, **kwargs: Any) -> None:
        raise OSError("fixture identity outage")

    monkeypatch.setattr(identity_module.auth, "delete_user", unavailable)
    with pytest.raises(BrowserSessionUnavailableError):
        await verifier.delete_account("consumer-123")


class _Verifier:
    project_id = _PROJECT

    def __init__(self) -> None:
        self.identity = verified_identity(
            _claims(), _PROJECT, _PROVIDERS, google_claims=_google_claims()
        )
        self.checked: list[tuple[str, int]] = []
        self.deleted: list[str] = []

    async def verify(
        self, token: str, *, google_id_token: str | None = None
    ) -> VerifiedConsumerIdentity:
        if token != "verified-fixture-token":
            raise AuthenticationFailedError("fixture token rejected")
        return self.identity

    async def check_session(self, uid: str, authenticated_at: int) -> None:
        self.checked.append((uid, authenticated_at))

    async def delete_account(self, uid: str) -> None:
        self.deleted.append(uid)


def _adapter() -> tuple[IdentityPlatformBrowserSessionAdapter, _MemorySessionStore, _Verifier]:
    store, verifier = _MemorySessionStore(), _Verifier()

    async def get_tenant(tenant_id: UUID) -> Tenant:
        assert tenant_id == verifier.identity.tenant_id
        return Tenant(
            tenant_id,
            verifier.identity.subject,
            verifier.identity.email,
            "inactive@accounts.invalid",
        )

    return (
        IdentityPlatformBrowserSessionAdapter(
            verifier=cast("IdentityPlatformVerifier", verifier),
            tenant_lookup=get_tenant,
            trusted_origin=_ORIGIN,
            redis_url="redis://unused",
            store=store,
        ),
        store,
        verifier,
    )


async def test_signin_challenge_is_one_shot_same_origin_and_bound_to_browser_cookie() -> None:
    adapter, _, verifier = _adapter()
    challenge = await adapter.begin_login("/?view=calendar")
    headers = {
        "Cookie": f"{adapter.login_cookie_name}={challenge.transaction_token}",
        "Origin": _ORIGIN,
    }
    for changed in ({"Origin": "https://attacker.test"}, {"Cookie": ""}):
        with pytest.raises(AuthenticationFailedError):
            await adapter.complete_identity_login(
                headers | changed, token="verified-fixture-token", state=challenge.state
            )
    completion = await adapter.complete_identity_login(
        headers, token="verified-fixture-token", state=challenge.state
    )
    assert completion.identity == verifier.identity
    assert completion.return_to == "/?view=calendar"
    with pytest.raises(AuthenticationFailedError):
        await adapter.complete_identity_login(
            headers, token="verified-fixture-token", state=challenge.state
        )


async def test_cookie_session_rotation_csrf_step_up_and_erasure_remain_account_bound() -> None:
    adapter, store, verifier = _adapter()
    identity = verifier.identity
    session = await adapter.issue_session(
        BrowserIdentity(identity.tenant_id, identity.subject, identity.authenticated_at)
    )
    headers = {
        "Cookie": f"{adapter.session_cookie_name}={session.session_token}; {adapter.csrf_cookie_name}={session.csrf_token}",
        "Origin": _ORIGIN,
        adapter.csrf_header_name: session.csrf_token,
    }
    assert await adapter.resolve_tenant_id(headers) == identity.tenant_id
    assert verifier.checked == [("consumer-123", identity.authenticated_at)]
    with pytest.raises(RecentAuthenticationRequiredError):
        await adapter.verify_recent_auth(identity.tenant_id, headers, max_age_seconds=300)
    start = await adapter.start_reauthentication(
        identity.tenant_id, headers, return_to="/settings/account"
    )
    state = start.authorization_url.split("state=")[1]
    completion = await adapter.complete_identity_login(
        headers
        | {
            "Cookie": headers["Cookie"] + f"; {adapter.login_cookie_name}={start.transaction_token}"
        },
        token="verified-fixture-token",
        state=state,
    )
    assert completion.reauthenticated
    await adapter.verify_recent_auth(identity.tenant_id, headers, max_age_seconds=300)
    await adapter.revoke_tenant_sessions(identity.tenant_id)
    assert verifier.deleted == ["consumer-123"]
    assert identity.tenant_id in store.fenced_tenants
    with pytest.raises(AuthenticationFailedError):
        await adapter.resolve_tenant_id(headers)
    with pytest.raises(BrowserSessionUnavailableError):
        await adapter.issue_session(
            BrowserIdentity(identity.tenant_id, identity.subject, identity.authenticated_at)
        )


async def test_step_up_cannot_authorize_another_account() -> None:
    adapter, _, verifier = _adapter()
    identity = verifier.identity
    session = await adapter.issue_session(
        BrowserIdentity(identity.tenant_id, identity.subject, identity.authenticated_at)
    )
    headers = {
        "Cookie": f"{adapter.session_cookie_name}={session.session_token}; {adapter.csrf_cookie_name}={session.csrf_token}",
        "Origin": _ORIGIN,
        adapter.csrf_header_name: session.csrf_token,
    }
    start = await adapter.start_reauthentication(
        identity.tenant_id, headers, return_to="/settings/account"
    )
    verifier.identity = verified_identity(
        _claims(sub="another-consumer"), _PROJECT, _PROVIDERS, google_claims=_google_claims()
    )
    with pytest.raises(ConsumerSignInRejectedError) as rejected:
        await adapter.complete_identity_login(
            headers
            | {
                "Cookie": headers["Cookie"]
                + f"; {adapter.login_cookie_name}={start.transaction_token}"
            },
            token="verified-fixture-token",
            state=start.authorization_url.split("state=")[1],
        )
    assert rejected.value.reason is ConsumerSignInFailureReason.REAUTHENTICATION
    with pytest.raises(RecentAuthenticationRequiredError):
        await adapter.verify_recent_auth(identity.tenant_id, headers, max_age_seconds=300)


class _Accounts:
    def __init__(self) -> None:
        self.accepted: list[tuple[VerifiedConsumerIdentity, LegalPolicy]] = []
        self.bootstrapped: list[VerifiedConsumerIdentity] = []

    async def bootstrap(self, identity: VerifiedConsumerIdentity) -> UUID:
        self.bootstrapped.append(identity)
        return identity.tenant_id

    async def accept(self, identity: VerifiedConsumerIdentity, policy: LegalPolicy) -> UUID:
        self.accepted.append((identity, policy))
        return identity.tenant_id

    async def has_accepted(self, tenant_id: UUID, policy: LegalPolicy) -> bool:
        return any(
            identity.tenant_id == tenant_id and accepted == policy
            for identity, accepted in self.accepted
        )

    async def is_ready(self, *, legal_required: bool = True) -> bool:
        return True


def _api(
    monkeypatch: pytest.MonkeyPatch, **changes: Any
) -> tuple[Any, _MemorySessionStore, _Accounts]:
    monkeypatch.setattr(app_module, "get_settings", lambda: _settings(**changes))
    app = app_module.create_app()
    adapter, store, verifier = _adapter()
    accounts = _Accounts()
    app.state.container = SimpleNamespace(
        browser_session=adapter,
        auth_context=adapter,
        csrf_protection=adapter,
        consumer_accounts=accounts,
        tenant_effect_authority=DirectTenantEffectAuthority(),
        tenant_repo=SimpleNamespace(
            get=AsyncMock(
                return_value=Tenant(
                    verifier.identity.tenant_id,
                    verifier.identity.subject,
                    verifier.identity.email,
                    "inactive@accounts.invalid",
                )
            )
        ),
        account_erasure_repo=SimpleNamespace(get=AsyncMock(return_value=None)),
    )
    return app, store, accounts


@pytest.mark.parametrize(
    "changes,status",
    [
        ({"accepted_terms": False}, 428),
        ({"accepted_terms": "true"}, 422),
        ({"terms_version": "old"}, 409),
        ({"privacy_version": "old"}, 409),
        ({"id_token": "rejected-private-token"}, 401),
        ({"state": "bad"}, 422),
        ({"admin": True}, 422),
    ],
)
async def test_api_rejects_bad_consent_and_redacts_tokens_without_issuing_session(
    monkeypatch: pytest.MonkeyPatch,
    changes: Any,
    status: int,
) -> None:
    app, store, accounts = _api(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        challenge = await client.get("/auth/identity/start")
        body = {
            "id_token": "verified-fixture-token",
            "state": challenge.json()["state"],
            "accepted_terms": True,
            "terms_version": "2026-10-05",
            "privacy_version": "2026-10-05",
        } | changes
        response = await client.post(
            "/auth/identity/session", json=body, headers={"Origin": _ORIGIN}
        )
    assert response.status_code == status, response.text
    assert "no-store" in response.headers["Cache-Control"]
    assert body["id_token"] not in response.text
    assert not accounts.accepted and not store.sessions


async def test_api_acceptance_issues_only_secure_opaque_cookies_and_protected_routes_reject_guests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, store, accounts = _api(monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        for path in ("/v1/me", "/v1/me/saved-filters"):
            assert (await client.get(path)).status_code == 401
        assert (await client.put("/v1/preferences", json={})).status_code == 401
        config = (await client.get("/v1/ui-config")).json()
        assert config["anonymous_browsing"] and config["auth_provider"] == "identity_platform"
        assert config["identity_platform"]["providers"] == ["google.com", "apple.com"]
        challenge = await client.get("/auth/identity/start?return_to=%2F%3Fview%3Dcalendar")
        response = await client.post(
            "/auth/identity/session",
            headers={"Origin": _ORIGIN},
            json={
                "id_token": "verified-fixture-token",
                "state": challenge.json()["state"],
                "accepted_terms": True,
                "terms_version": "2026-10-05",
                "privacy_version": "2026-10-05",
            },
        )
    assert response.status_code == 200 and response.json() == {"return_to": "/?view=calendar"}
    assert len(accounts.accepted) == len(store.sessions) == 1
    assert "verified-fixture-token" not in response.text
    session_cookie = next(
        cookie
        for cookie in response.headers.get_list("set-cookie")
        if cookie.startswith("__Host-ec_session=") and "Max-Age=28800" in cookie
    )
    assert all(flag in session_cookie for flag in ("HttpOnly", "Secure", "SameSite=lax", "Path=/"))


def _deferred() -> dict[str, Any]:
    return {
        "consumer_legal_mode": "deferred",
        "signup_terms_version": None,
        "signup_terms_url": None,
        "signup_privacy_version": None,
        "signup_privacy_url": None,
    }


def test_legal_deferral_is_explicit_and_cannot_claim_approved_documents() -> None:
    assert _settings().consumer_legal_mode == "required"
    assert _settings(**_deferred()).consumer_legal_policy is None
    blank = {key: "" if key.startswith("signup_") else value for key, value in _deferred().items()}
    assert _settings(**blank).consumer_legal_policy is None
    with pytest.raises(ValidationError):
        _settings(**(blank | {"consumer_legal_mode": "required"}))
    for change in (
        {"consumer_legal_mode": "ignored"},
        {"identity_platform_enabled": False},
        {"signup_terms_version": "invented"},
        {"signup_terms_url": f"{_ORIGIN}/terms"},
    ):
        with pytest.raises(ValidationError):
            _settings(**(_deferred() | change))


async def test_deferred_signup_binds_account_without_receipt_and_preserves_csrf_logout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, store, accounts = _api(monkeypatch, **_deferred())
    filters = AsyncMock(return_value=[])
    app.state.container.saved_catalog_filters = SimpleNamespace(list_filters=filters)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        assert (await client.get("/v1/me/saved-filters")).status_code == 401
        config = (await client.get("/v1/ui-config")).json()
        assert config["consumer_legal_mode"] == "deferred" and config["legal_policy"] is None
        assert config["logout_url"] == "/auth/logout"
        challenge = await client.get("/auth/identity/start?return_to=%2Fsettings")
        response = await client.post(
            "/auth/identity/session",
            headers={"Origin": _ORIGIN},
            json={"id_token": "verified-fixture-token", "state": challenge.json()["state"]},
        )
        assert response.status_code == 200 and response.json() == {"return_to": "/settings"}
        assert len(accounts.bootstrapped) == len(store.sessions) == 1 and not accounts.accepted
        assert "verified-fixture-token" not in response.text
        assert (await client.get("/v1/me/saved-filters")).json() == []
        filters.assert_awaited_once_with(accounts.bootstrapped[0].tenant_id)
        assert (await client.post("/auth/logout", headers={"Origin": _ORIGIN})).status_code == 403
        assert len(store.sessions) == 1
        csrf = client.cookies.get("__Host-ec_csrf")
        assert (
            await client.post("/auth/logout", headers={"Origin": _ORIGIN, "X-EC-CSRF": csrf})
        ).status_code == 204
        assert not store.sessions
        assert (await client.get("/v1/me/saved-filters")).status_code == 401


@pytest.mark.parametrize(
    "body_change,status",
    [
        ({"accepted_terms": True}, 400),
        ({"terms_version": "invented"}, 400),
        ({"privacy_version": "invented"}, 400),
        ({"consumer_legal_mode": "deferred"}, 422),
        ({"id_token": "bad-token"}, 401),
    ],
)
async def test_deferred_signup_rejects_fabricated_acceptance_or_unverified_identity(
    monkeypatch: pytest.MonkeyPatch,
    body_change: Any,
    status: int,
) -> None:
    app, store, accounts = _api(monkeypatch, **_deferred())
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        challenge = await client.get("/auth/identity/start")
        response = await client.post(
            "/auth/identity/session",
            headers={"Origin": _ORIGIN},
            json={"id_token": "verified-fixture-token", "state": challenge.json()["state"]}
            | body_change,
        )
    assert response.status_code == status
    assert not accounts.bootstrapped and not accounts.accepted and not store.sessions


async def test_return_to_required_legal_gates_personal_data_until_real_acceptance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, store, accounts = _api(monkeypatch, **_deferred())
    filters = AsyncMock(return_value=[])
    app.state.container.saved_catalog_filters = SimpleNamespace(list_filters=filters)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        challenge = await client.get("/auth/identity/start")
        assert (
            await client.post(
                "/auth/identity/session",
                headers={"Origin": _ORIGIN},
                json={"id_token": "verified-fixture-token", "state": challenge.json()["state"]},
            )
        ).status_code == 200
        cookies = client.cookies
    strict_app, _, _ = _api(monkeypatch)
    strict_app.state.container = app.state.container  # same durable account/session authorities
    async with AsyncClient(
        transport=ASGITransport(app=strict_app), base_url=_ORIGIN, cookies=cookies
    ) as client:
        assert (await client.get("/v1/me/saved-filters")).status_code == 428
        assert not accounts.accepted
        filters.assert_not_awaited()
        csrf = client.cookies.get("__Host-ec_csrf")
        assert (
            await client.post("/auth/logout", headers={"Origin": _ORIGIN, "X-EC-CSRF": csrf})
        ).status_code == 204
        assert not store.sessions
        challenge = await client.get("/auth/identity/start")
        assert (
            await client.post(
                "/auth/identity/session",
                headers={"Origin": _ORIGIN},
                json={"id_token": "verified-fixture-token", "state": challenge.json()["state"]},
            )
        ).status_code == 428
        assert not store.sessions and not accounts.accepted
        challenge = await client.get("/auth/identity/start")
        assert (
            await client.post(
                "/auth/identity/session",
                headers={"Origin": _ORIGIN},
                json={
                    "id_token": "verified-fixture-token",
                    "state": challenge.json()["state"],
                    "accepted_terms": True,
                    "terms_version": "2026-10-05",
                    "privacy_version": "2026-10-05",
                },
            )
        ).status_code == 200
        assert len(accounts.bootstrapped) == len(accounts.accepted) == 1
        assert (await client.get("/v1/me/saved-filters")).json() == []
        filters.assert_awaited_once_with(accounts.bootstrapped[0].tenant_id)


def test_published_catalog_routes_have_no_consumer_identity_dependency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, _ = _api(monkeypatch)
    public = [
        route for route in app.routes if getattr(route, "path", "").startswith("/v1/catalog/")
    ]
    assert len(public) == 10
    assert any(route.path == "/v1/catalog/name-suggestions" for route in public)
    for route in public:
        assert route.methods == {"GET"}
        assert not route.dependant.dependencies
