"""Google identity admits only existing exact subjects after complete token verification."""

import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from pydantic import ValidationError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from tests.unit.test_oidc_bff_session import (
    _CLIENT_ID,
    _CLIENT_SECRET,
    _KEY_ID,
    _ORIGIN,
    _PRIVATE_KEY,
    _PUBLIC_JWK,
    _AccountErasureRepository,
    _MemorySessionStore,
    _settings,
    _TenantRepository,
    app_module,
)

import events_concierge.adapters.postgres.tenant_repos as tenant_repo_module
from events_concierge.adapters.oidc.auth import OidcJwtAuthContext
from events_concierge.adapters.oidc.session import OidcBffSessionAdapter
from events_concierge.adapters.postgres.tenant_repos import PostgresTenantRepository
from events_concierge.application.tenant_effects import DirectTenantEffectAuthority
from events_concierge.composition import _build_oidc_bff_session
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.oidc import (
    GOOGLE_AUTHORIZATION_URL,
    GOOGLE_ISSUER,
    GOOGLE_JWKS_URL,
    GOOGLE_TOKEN_URL,
    google_subject_binding,
)
from events_concierge.ports.auth import (
    AuthenticationFailedError,
    BrowserIdentity,
    BrowserSessionUnavailableError,
    BrowserStepUpUnavailableError,
)
from events_concierge.ports.tenant_effects import TenantEffectFencedError

_SUBJECT = "10769150350006150715113082367"


def _google_settings(**overrides: Any) -> Any:
    return _settings(
        **{
            "oidc_provider": "google",
            "oidc_issuer": GOOGLE_ISSUER,
            "oidc_authorization_url": GOOGLE_AUTHORIZATION_URL,
            "oidc_token_url": GOOGLE_TOKEN_URL,
            "oidc_jwks_url": GOOGLE_JWKS_URL,
            "oidc_tenant_claim": None,
            **overrides,
        }
    )


def _token(nonce: str = "transaction-nonce", **overrides: Any) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "iss": GOOGLE_ISSUER,
            "sub": _SUBJECT,
            "aud": _CLIENT_ID,
            "azp": _CLIENT_ID,
            "iat": now,
            "exp": now + timedelta(minutes=5),
            "nonce": nonce,
            # Deliberately unverified and equal across identities: email never selects an account.
            "email": "same@example.test",
            "email_verified": False,
            **overrides,
        },
        _PRIVATE_KEY,
        algorithm="RS256",
        headers={"kid": _KEY_ID},
    )


class _Lookup:
    def __init__(self) -> None:
        self.tenant_id = uuid4()
        self.calls: list[str] = []

    async def __call__(self, binding: str) -> UUID | None:
        self.calls.append(binding)
        return self.tenant_id if binding == google_subject_binding(_SUBJECT) else None


def _verifier(lookup: _Lookup) -> OidcJwtAuthContext:
    return OidcJwtAuthContext(
        issuer=GOOGLE_ISSUER,
        audience=_CLIENT_ID,
        jwks_url=GOOGLE_JWKS_URL,
        provider="google",
        google_tenant_lookup=lookup,
        signing_key_resolver=lambda _: _PUBLIC_JWK,
    )


@pytest.mark.parametrize("issuer", [GOOGLE_ISSUER, "accounts.google.com"])
async def test_google_aliases_resolve_one_explicit_binding_without_email_linking(
    issuer: str,
) -> None:
    lookup = _Lookup()
    verifier = _verifier(lookup)
    identity = await verifier.verify_identity_token(
        _token(iss=issuer), expected_nonce="transaction-nonce"
    )
    assert identity.tenant_id == lookup.tenant_id
    assert identity.subject == google_subject_binding(_SUBJECT)
    assert identity.authenticated_at is None
    with pytest.raises(AuthenticationFailedError):
        await verifier.verify_identity_token(
            _token(sub="other-google-account"), expected_nonce="transaction-nonce"
        )
    assert lookup.calls == [
        google_subject_binding(_SUBJECT),
        google_subject_binding("other-google-account"),
    ]


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "https://accounts.google.com/"},
        {"iss": "https://accounts.google.com.attacker.example"},
        {"aud": "other-client"},
        {"azp": "other-client"},
        {"aud": [_CLIENT_ID, "other-client"], "azp": None},
        {"exp": 1},
        {"iat": 9_999_999_999},
        {"nonce": "wrong-transaction"},
        {"sub": ""},
        {"sub": "a" * 256},
        {"sub": "hidden\nsubject"},
        {"sub": "ümlaut"},
    ],
)
async def test_google_rejected_claims_never_reach_tenant_lookup(overrides: dict[str, Any]) -> None:
    lookup = _Lookup()
    with pytest.raises(AuthenticationFailedError):
        await _verifier(lookup).verify_identity_token(
            _token(**overrides), expected_nonce="transaction-nonce"
        )
    assert lookup.calls == []


async def test_google_wrong_signature_never_reaches_tenant_lookup() -> None:
    lookup = _Lookup()
    token = _token()
    header, payload, signature = token.split(".")
    signature = ("A" if signature[0] != "A" else "B") + signature[1:]
    with pytest.raises(AuthenticationFailedError):
        await _verifier(lookup).verify_identity_token(
            f"{header}.{payload}.{signature}", expected_nonce="transaction-nonce"
        )
    assert lookup.calls == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"oidc_issuer": "accounts.google.com"},
        {"oidc_jwks_url": "https://attacker.example/keys"},
        {"oidc_authorization_url": GOOGLE_AUTHORIZATION_URL + "?prompt=none"},
        {"oidc_token_url": "https://attacker.example/token"},
        {"oidc_algorithms": "RS256,RS384"},
        {"oidc_tenant_claim": "sub"},
        {"mock_cloud": True},
    ],
)
def test_google_configuration_is_explicit_and_pinned(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _google_settings(**overrides)


async def test_google_composition_uses_repository_lookup() -> None:
    session = _build_oidc_bff_session(_google_settings())
    assert session._identity_verifier._google_tenant_lookup is not None
    await session.aclose()


@pytest.mark.parametrize("fenced", [False, True])
async def test_google_api_login_csrf_logout_and_erasure_fence(
    monkeypatch: pytest.MonkeyPatch, fenced: bool
) -> None:
    lookup = _Lookup()
    store = _MemorySessionStore()
    token_calls: list[dict[str, list[str]]] = []

    async def exchange(request: httpx.Request) -> httpx.Response:
        token_calls.append(parse_qs(request.content.decode()))
        transaction = (
            json.loads(next(iter(store.logins.values()))) if store.logins else saved_transaction
        )
        return httpx.Response(200, json={"id_token": _token(transaction["nonce"])})

    async with httpx.AsyncClient(transport=httpx.MockTransport(exchange)) as token_client:
        session = OidcBffSessionAdapter(
            issuer=GOOGLE_ISSUER,
            authorization_url=GOOGLE_AUTHORIZATION_URL,
            token_url=GOOGLE_TOKEN_URL,
            jwks_url=GOOGLE_JWKS_URL,
            client_id=_CLIENT_ID,
            client_secret=_CLIENT_SECRET,
            provider="google",
            google_tenant_lookup=lookup,
            redirect_uri=f"{_ORIGIN}/auth/callback",
            trusted_origin=_ORIGIN,
            redis_url="redis://unused",
            store=store,
            http_client=token_client,
            identity_verifier=_verifier(lookup),
        )
        tenant = Tenant(
            lookup.tenant_id,
            google_subject_binding(_SUBJECT),
            "existing@example.test",
            "existing@relay.test",
        )

        class _FencedAuthority:
            async def run(self, request: Any, effect: Any) -> None:
                raise TenantEffectFencedError("fixture erasure won the race")

        monkeypatch.setattr(app_module, "get_settings", _google_settings)
        app = app_module.create_app()
        app.state.container = SimpleNamespace(
            browser_session=session,
            auth_context=session,
            csrf_protection=session,
            tenant_repo=_TenantRepository(tenant),
            account_erasure_repo=_AccountErasureRepository(tenant.tenant_id),
            tenant_effect_authority=_FencedAuthority() if fenced else DirectTenantEffectAuthority(),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=_ORIGIN
        ) as client:
            ui_config = (await client.get("/v1/ui-config")).json()
            assert ui_config["auth_provider"] == "google"
            assert ui_config["reauth_url"] is None
            assert ui_config["logout_url"] == "/auth/logout"
            login = await client.get("/auth/login")
            query = parse_qs(urlsplit(login.headers["location"]).query)
            saved_transaction = json.loads(next(iter(store.logins.values())))
            assert query["scope"] == ["openid email"]
            assert query["prompt"] == ["select_account"]
            assert query["code_challenge_method"] == ["S256"]
            callback = await client.get(
                "/auth/callback", params={"code": "real-fixture-code", "state": query["state"][0]}
            )
            assert callback.status_code == 303
            assert callback.headers["location"] == (
                "/sign-in?reason=not_authorized" if fenced else "/app"
            )
            assert callback.headers["referrer-policy"] == "no-referrer"
            assert token_calls[0]["code_verifier"] == [saved_transaction["verifier"]]
            assert lookup.calls == [tenant.oidc_subject]
            assert len(store.sessions) == (0 if fenced else 1)
            replay = await client.get(
                "/auth/callback", params={"code": "real-fixture-code", "state": query["state"][0]}
            )
            assert replay.status_code == 303
            assert replay.headers["location"] == "/sign-in?reason=not_authorized"
            assert len(token_calls) == 1
            if not fenced:
                csrf = client.cookies.get(session.csrf_cookie_name)
                denied = await client.post("/auth/logout")
                assert denied.status_code == 403
                authority = {"Origin": _ORIGIN, session.csrf_header_name: csrf}
                reauth = await client.post(
                    "/auth/reauth", json={"return_to": "/app#/settings"}, headers=authority
                )
                assert reauth.status_code == 503
                assert "supported step-up" in reauth.json()["detail"]
                assert store.logins == {}
                with pytest.raises(BrowserStepUpUnavailableError):
                    await session.verify_recent_auth(
                        tenant.tenant_id, authority, max_age_seconds=300
                    )
                logout = await client.post("/auth/logout", headers=authority)
                assert logout.status_code == 204
                assert store.sessions == {}
        await session.aclose()


@pytest.mark.parametrize(
    "code,wrong_state", [(None, False), ("must-not-exchange", False), (None, True)]
)
async def test_provider_cancellation_is_state_bound_consumed_and_does_not_exchange(
    monkeypatch: pytest.MonkeyPatch, code: str | None, wrong_state: bool
) -> None:
    lookup = _Lookup()
    store = _MemorySessionStore()
    session = OidcBffSessionAdapter(
        issuer=GOOGLE_ISSUER,
        authorization_url=GOOGLE_AUTHORIZATION_URL,
        token_url=GOOGLE_TOKEN_URL,
        jwks_url=GOOGLE_JWKS_URL,
        client_id=_CLIENT_ID,
        client_secret=_CLIENT_SECRET,
        provider="google",
        google_tenant_lookup=lookup,
        redirect_uri=f"{_ORIGIN}/auth/callback",
        trusted_origin=_ORIGIN,
        redis_url="redis://unused",
        store=store,
        identity_verifier=_verifier(lookup),
    )

    async def unexpected_exchange(*args: Any) -> str:
        raise AssertionError("provider cancellation must not exchange a code")

    monkeypatch.setattr(session, "_exchange_code", unexpected_exchange)
    monkeypatch.setattr(app_module, "get_settings", _google_settings)
    app = app_module.create_app()
    app.state.container = SimpleNamespace(browser_session=session)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=_ORIGIN
    ) as client:
        login = await client.get("/auth/login")
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        callback_params = {
            "state": "A" * 43 if wrong_state else state,
            "error": "provider-private-details",
        }
        if code is not None:
            callback_params["code"] = code
        response = await client.get("/auth/callback", params=callback_params)
        assert response.status_code == 303
        assert response.headers["location"] == (
            "/sign-in?reason=not_authorized" if wrong_state else "/sign-in?reason=cancelled"
        )
        assert "provider-private-details" not in str(response.headers)
        assert response.headers["cache-control"] == "no-store, max-age=0"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert store.logins == {}
        assert store.sessions == {}
        replay = await client.get(
            "/auth/callback", params={"code": "cannot-replay", "state": state}
        )
        assert replay.headers["location"] == "/sign-in?reason=not_authorized"
    assert lookup.calls == []
    await session.aclose()


async def test_google_lookup_pool_exhaustion_is_an_availability_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def exhausted_pool() -> Any:
        raise SqlAlchemyTimeoutError("fixture pool exhausted")
        yield  # pragma: no cover -- async context manager shape

    monkeypatch.setattr(tenant_repo_module, "system_session_scope", exhausted_pool)
    with pytest.raises(BrowserSessionUnavailableError, match="lookup unavailable"):
        await PostgresTenantRepository().resolve_google_tenant(google_subject_binding(_SUBJECT))
    assert not await PostgresTenantRepository().google_identity_is_ready()


async def test_sessions_cannot_cross_google_provider_or_client_authority() -> None:
    store = _MemorySessionStore()
    lookup = _Lookup()

    def make_session(*, google: bool, client_id: str = _CLIENT_ID) -> OidcBffSessionAdapter:
        settings = _google_settings() if google else _settings()
        return OidcBffSessionAdapter(
            issuer=settings.oidc_issuer,
            authorization_url=settings.oidc_authorization_url,
            token_url=settings.oidc_token_url,
            jwks_url=settings.oidc_jwks_url,
            client_id=client_id,
            client_secret=_CLIENT_SECRET,
            tenant_claim=settings.oidc_tenant_claim,
            provider=settings.oidc_provider,
            google_tenant_lookup=lookup if google else None,
            redirect_uri=f"{_ORIGIN}/auth/callback",
            trusted_origin=_ORIGIN,
            redis_url="redis://unused",
            store=store,
        )

    google, custom, other_client = (
        make_session(google=True),
        make_session(google=False),
        make_session(google=True, client_id="other-google-client"),
    )
    identity = BrowserIdentity(lookup.tenant_id, google_subject_binding(_SUBJECT))
    try:
        assert not await google.is_ready()  # Google cannot advertise only Redis readiness.

        async def metadata_ready() -> bool:
            return True

        google._google_identity_ready = metadata_ready
        assert await google.is_ready()
        issued = await google.issue_session(identity)
        headers = {"Cookie": f"{google.session_cookie_name}={issued.session_token}"}
        assert await google.resolve_tenant_id(headers) == identity.tenant_id
        for wrong_authority in (custom, other_client):
            with pytest.raises(AuthenticationFailedError):
                await wrong_authority.resolve_tenant_id(headers)
        # Even a custom token with the same subject namespace lacks Google session authority.
        old = await custom.issue_session(identity)
        old_headers = {"Cookie": f"{custom.session_cookie_name}={old.session_token}"}
        assert await custom.resolve_tenant_id(old_headers) == identity.tenant_id
        with pytest.raises(AuthenticationFailedError):
            await google.resolve_tenant_id(old_headers)
        assert lookup.calls == []  # session checks never resolve an unverified token/identity
    finally:
        await google.aclose()
        await custom.aclose()
        await other_client.aclose()
