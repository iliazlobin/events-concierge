"""Sign-in diagnostics identify fixed failure branches without exposing identity data."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import DBAPIError
from tests.unit.test_consumer_identity import (
    _ORIGIN,
    _PROJECT,
    _PROVIDERS,
    _VALIDATION_NOW,
    _adapter,
    _api,
    _claims,
    _deferred,
    _settings,
    identity_module,
)

from events_concierge.adapters.identity_platform import IdentityPlatformVerifier, verified_identity
from events_concierge.adapters.postgres import consumer_accounts as accounts_module
from events_concierge.ports.auth import (
    AuthenticationFailedError,
    BrowserSessionUnavailableError,
    ConsumerSignInRejectedError,
)
from events_concierge.ports.auth import (
    ConsumerSignInFailureReason as Reason,
)
from events_concierge.ports.tenant_effects import TenantEffectFencedError

_COUNTER = "events_concierge_consumer_sign_in_rejections_total{"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"iss": "other"}, Reason.CLAIM_AUTHORITY),
        ({"aud": [_PROJECT]}, Reason.CLAIM_AUTHORITY),
        ({"firebase": None}, Reason.CLAIM_AUTHORITY),
        ({"firebase": {"tenant": "other"}}, Reason.CLAIM_AUTHORITY),
        ({"firebase": {}}, Reason.CLAIM_PROVIDER),
        ({"firebase": {"sign_in_provider": []}}, Reason.CLAIM_PROVIDER),
        ({"firebase": {"sign_in_provider": "password"}}, Reason.CLAIM_PROVIDER),
        ({"sub": None}, Reason.CLAIM_UID),
        ({"sub": ""}, Reason.CLAIM_UID),
        ({"sub": "x" * 129}, Reason.CLAIM_UID),
        ({"sub": "private secret"}, Reason.CLAIM_UID),
        ({"sub": "é"}, Reason.CLAIM_UID),
        ({"sub": "\x00"}, Reason.CLAIM_UID),
        ({"email_verified": "true"}, Reason.CLAIM_EMAIL),
        ({"email": None}, Reason.CLAIM_EMAIL),
        ({"email": "a"}, Reason.CLAIM_EMAIL),
        ({"email": "x" * 320 + "@"}, Reason.CLAIM_EMAIL),
        ({"email": "one@two@three"}, Reason.CLAIM_EMAIL),
        ({"email": "private\n@example.test"}, Reason.CLAIM_EMAIL),
        ({"auth_time": True}, Reason.CLAIM_FRESHNESS),
        ({"auth_time": _VALIDATION_NOW - 301}, Reason.CLAIM_FRESHNESS),
        ({"auth_time": _VALIDATION_NOW + 31}, Reason.CLAIM_FRESHNESS),
    ],
)
def test_claim_rejection_category_preserves_security_predicates_and_generic_message(
    monkeypatch: pytest.MonkeyPatch, changes: Any, reason: Reason
) -> None:
    monkeypatch.setattr(identity_module, "time", lambda: _VALIDATION_NOW)
    with pytest.raises(ConsumerSignInRejectedError) as caught:
        verified_identity(_claims(auth_time=_VALIDATION_NOW) | changes, _PROJECT, _PROVIDERS)
    assert caught.value.reason is reason
    assert str(caught.value) == "sign-in could not be verified"


@pytest.mark.parametrize("authenticated_at", [_VALIDATION_NOW - 300, _VALIDATION_NOW + 30])
def test_valid_claim_freshness_boundaries_remain_accepted(
    monkeypatch: pytest.MonkeyPatch, authenticated_at: int
) -> None:
    monkeypatch.setattr(identity_module, "time", lambda: _VALIDATION_NOW)
    assert verified_identity(_claims(auth_time=authenticated_at), _PROJECT, _PROVIDERS)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("origin", Reason.ORIGIN),
        ("missing_cookie", Reason.LOGIN_COOKIE),
        ("malformed_cookie", Reason.LOGIN_COOKIE),
        ("duplicate_cookie", Reason.LOGIN_COOKIE),
        ("invalid_state", Reason.CHALLENGE),
        ("mismatched_state", Reason.CHALLENGE),
        ("expired_challenge", Reason.CHALLENGE),
    ],
)
async def test_browser_binding_rejection_precedes_token_verification(
    case: str, reason: Reason
) -> None:
    adapter, store, verifier = _adapter()
    verify = AsyncMock(wraps=verifier.verify)
    verifier.verify = verify
    challenge = await adapter.begin_login("/")
    state = challenge.state
    headers = {
        "Origin": _ORIGIN,
        "Cookie": f"{adapter.login_cookie_name}={challenge.transaction_token}",
    }
    if case == "origin":
        headers["Origin"] = "https://attacker.test"
    elif case == "missing_cookie":
        del headers["Cookie"]
    elif case == "malformed_cookie":
        headers["Cookie"] = f"{adapter.login_cookie_name}=private-secret"
    elif case == "duplicate_cookie":
        headers["Cookie"] += "; " + headers["Cookie"]
    elif case == "invalid_state":
        state = "private-secret"
    elif case == "mismatched_state":
        state = (await adapter.begin_login("/")).state
    else:
        await store.consume_login(challenge.transaction_token)
    with pytest.raises(ConsumerSignInRejectedError) as caught:
        await adapter.complete_identity_login(headers, token="private-token", state=state)
    assert caught.value.reason is reason
    verify.assert_not_awaited()
    assert not store.sessions


async def test_replayed_challenge_has_its_own_category_and_never_reverifies_token() -> None:
    adapter, _, verifier = _adapter()
    verify = AsyncMock(wraps=verifier.verify)
    verifier.verify = verify
    challenge = await adapter.begin_login("/")
    headers = {
        "Origin": _ORIGIN,
        "Cookie": f"{adapter.login_cookie_name}={challenge.transaction_token}",
    }
    await adapter.complete_identity_login(
        headers, token="verified-fixture-token", state=challenge.state
    )
    with pytest.raises(ConsumerSignInRejectedError) as caught:
        await adapter.complete_identity_login(
            headers, token="verified-fixture-token", state=challenge.state
        )
    assert caught.value.reason is Reason.CHALLENGE
    assert verify.await_count == 1


@pytest.mark.parametrize(
    "failure",
    [
        "InvalidIdTokenError",
        "ExpiredIdTokenError",
        "RevokedIdTokenError",
        "UserDisabledError",
        "UserNotFoundError",
        "ValueError",
        "TypeError",
    ],
)
async def test_sdk_rejections_are_bounded_and_outages_remain_unavailable(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    verifier = IdentityPlatformVerifier(_PROJECT, tuple(_PROVIDERS))
    exception = {"ValueError": ValueError, "TypeError": TypeError}.get(failure)
    if exception is None:
        exception = getattr(identity_module.auth, failure)

    def rejected(*args: Any, **kwargs: Any) -> Any:
        raise exception("private-provider-message")

    monkeypatch.setattr(identity_module.auth, "verify_id_token", rejected)
    with pytest.raises(ConsumerSignInRejectedError) as caught:
        await verifier.verify("private-token")
    assert caught.value.reason is Reason.TOKEN_VERIFICATION
    assert "private" not in str(caught.value)

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise OSError("private-provider-message")

    monkeypatch.setattr(identity_module.auth, "verify_id_token", unavailable)
    with pytest.raises(BrowserSessionUnavailableError):
        await verifier.verify("private-token")


@pytest.mark.parametrize("method", ["bootstrap", "accept"])
@pytest.mark.parametrize("sqlstate", ["42501", "08006"])
async def test_account_denial_is_distinct_from_database_outage(
    monkeypatch: pytest.MonkeyPatch, method: str, sqlstate: str
) -> None:
    class DatabaseFailureError(Exception):
        def __init__(self) -> None:
            super().__init__("private-database-message")
            self.sqlstate = sqlstate

    original = DatabaseFailureError()
    failure = DBAPIError("private-sql", {}, original)
    execute = AsyncMock(side_effect=failure)

    @asynccontextmanager
    async def session_scope():
        yield SimpleNamespace(execute=execute)

    monkeypatch.setattr(accounts_module, "system_session_scope", session_scope)
    repository = accounts_module.PostgresConsumerAccountRepository()
    identity = verified_identity(_claims(), _PROJECT, _PROVIDERS)
    policy = _settings().consumer_legal_policy
    assert policy is not None
    operation = (
        repository.bootstrap(identity)
        if method == "bootstrap"
        else repository.accept(identity, policy)
    )
    if sqlstate == "42501":
        with pytest.raises(ConsumerSignInRejectedError) as caught:
            await operation
        assert caught.value.reason is Reason.ACCOUNT_UNAVAILABLE
    else:
        with pytest.raises(BrowserSessionUnavailableError):
            await operation
    execute.assert_awaited_once()


@pytest.mark.parametrize("reason", list(Reason))
async def test_one_counter_per_rejected_post_and_no_private_details_in_response_or_metrics(
    monkeypatch: pytest.MonkeyPatch, reason: Reason
) -> None:
    app, store, accounts = _api(monkeypatch, **_deferred())
    error = (
        AuthenticationFailedError("private-token email@example.test")
        if reason is Reason.UNKNOWN
        else ConsumerSignInRejectedError(reason)
    )
    app.state.container.browser_session.complete_identity_login = AsyncMock(side_effect=error)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        start = await client.get("/auth/identity/start")
        response = await client.post(
            "/auth/identity/session",
            headers={"Origin": _ORIGIN},
            json={"id_token": "private-token", "state": start.json()["state"]},
        )
    assert response.status_code == 401
    assert response.json() == {"detail": "sign-in could not be verified"}
    assert "no-store" in response.headers["cache-control"]
    rendered = app.state.metrics.render()
    assert [line for line in rendered.splitlines() if line.startswith(_COUNTER)] == [
        f'{_COUNTER}reason="{reason.value}"}} 1'
    ]
    assert not accounts.bootstrapped and not accounts.accepted and not store.sessions
    for private in ("private-token", "email@example.test", start.json()["state"]):
        assert private not in response.text and private not in rendered


@pytest.mark.parametrize(
    "case,reason",
    [("fenced", Reason.ACCOUNT_FENCED), ("old_cookie", Reason.EXISTING_SESSION_COOKIE)],
)
async def test_post_bootstrap_denial_never_issues_session_and_counts_once(
    monkeypatch: pytest.MonkeyPatch, case: str, reason: Reason
) -> None:
    app, store, accounts = _api(monkeypatch, **_deferred())
    if case == "fenced":
        app.state.container.tenant_effect_authority.run = AsyncMock(
            side_effect=TenantEffectFencedError("private-fence")
        )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        start = await client.get("/auth/identity/start")
        if case == "old_cookie":
            client.cookies.set("__Host-ec_session", "private-malformed-cookie")
        response = await client.post(
            "/auth/identity/session",
            headers={"Origin": _ORIGIN},
            json={"id_token": "verified-fixture-token", "state": start.json()["state"]},
        )
    assert response.status_code == 401 and not store.sessions
    assert len(accounts.bootstrapped) == 1
    assert [
        line for line in app.state.metrics.render().splitlines() if line.startswith(_COUNTER)
    ] == [f'{_COUNTER}reason="{reason.value}"}} 1']


async def test_service_outage_is_503_without_a_rejection_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, store, accounts = _api(monkeypatch, **_deferred())
    app.state.container.browser_session.verifier.verify = AsyncMock(
        side_effect=BrowserSessionUnavailableError("private-outage")
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        start = await client.get("/auth/identity/start")
        response = await client.post(
            "/auth/identity/session",
            headers={"Origin": _ORIGIN},
            json={"id_token": "private-token", "state": start.json()["state"]},
        )
    assert response.status_code == 503
    assert not any(line.startswith(_COUNTER) for line in app.state.metrics.render().splitlines())
    assert not store.sessions and not accounts.bootstrapped


async def test_success_has_no_rejection_counter(monkeypatch: pytest.MonkeyPatch) -> None:
    app, store, accounts = _api(monkeypatch, **_deferred())
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        start = await client.get("/auth/identity/start")
        response = await client.post(
            "/auth/identity/session",
            headers={"Origin": _ORIGIN},
            json={"id_token": "verified-fixture-token", "state": start.json()["state"]},
        )
    assert response.status_code == 200
    assert len(store.sessions) == len(accounts.bootstrapped) == 1
    assert not any(line.startswith(_COUNTER) for line in app.state.metrics.render().splitlines())
