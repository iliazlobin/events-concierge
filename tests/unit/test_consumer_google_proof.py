"""Google contact email requires two independently signed, cross-bound identity proofs."""

from __future__ import annotations

from time import time
from typing import Any
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient
from jwt import PyJWK
from jwt.exceptions import PyJWKClientConnectionError
from tests.unit.test_consumer_identity import (
    _GOOGLE_CLIENT,
    _ORIGIN,
    _PROJECT,
    _PROVIDERS,
    _api,
    _claims,
    _deferred,
    _settings,
    identity_module,
)

from events_concierge.adapters.identity_platform import IdentityPlatformVerifier
from events_concierge.ports.auth import (
    BrowserSessionUnavailableError,
    ConsumerSignInRejectedError,
)
from events_concierge.ports.auth import (
    ConsumerSignInFailureReason as Reason,
)


@pytest.fixture(scope="module")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _google_payload(**changes: Any) -> dict[str, Any]:
    now = int(time())
    return {
        "iss": "https://accounts.google.com",
        "aud": _GOOGLE_CLIENT,
        "azp": _GOOGLE_CLIENT,
        "sub": "google-123",
        "iat": now,
        "exp": now + 3600,
        "email": "consumer@example.test",
        "email_verified": True,
        **changes,
    }


def _token(key: rsa.RSAPrivateKey, **changes: Any) -> str:
    return jwt.encode(_google_payload(**changes), key, algorithm="RS256", headers={"kid": "test"})


def _verifier(
    monkeypatch: pytest.MonkeyPatch, key: rsa.RSAPrivateKey, **firebase_changes: Any
) -> IdentityPlatformVerifier:
    verifier = IdentityPlatformVerifier(_PROJECT, tuple(_PROVIDERS), _GOOGLE_CLIENT)
    verified = _claims(email=None, email_verified=False) | firebase_changes
    monkeypatch.setattr(identity_module.auth, "verify_id_token", lambda *a, **kw: verified)
    jwk = PyJWK.from_dict(
        jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key(), as_dict=True) | {"kid": "test"}
    )
    monkeypatch.setattr(verifier._google_keys._client, "get_signing_keys", lambda **kw: [jwk])
    return verifier


async def test_google_without_primary_email_uses_signed_provider_email_and_firebase_uid(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    verifier = _verifier(monkeypatch, signing_key)
    identity = await verifier.verify("firebase-proof", google_id_token=_token(signing_key))
    assert identity.uid == "consumer-123" and identity.email == "consumer@example.test"
    assert identity.subject == f"identity-platform:v1:{_PROJECT}:consumer-123"
    # Google email is metadata; the same signed Google ID bound to another Firebase UID
    # still produces a different application account.
    other = _verifier(monkeypatch, signing_key, sub="other-uid")
    assert (
        await other.verify("firebase-proof", google_id_token=_token(signing_key))
    ).tenant_id != identity.tenant_id


@pytest.mark.parametrize(
    "changes",
    [
        {"iss": "https://attacker.test"},
        {"aud": "other-client"},
        {"aud": [_GOOGLE_CLIENT]},
        {"azp": "other-presenter"},
        {"azp": None},
        {"azp": [_GOOGLE_CLIENT]},
        {"sub": "different-google-user"},
        {"sub": ""},
        {"sub": "x" * 256},
        {"sub": "private value"},
        {"sub": "é"},
        {"email_verified": False},
        {"email_verified": "true"},
        {"email_verified": 1},
        {"email": None},
        {"email": "one@two@three"},
        {"email": "private\n@example.test"},
        {"iat": True},
        {"exp": True},
        {"iat": "123"},
        {"exp": "123"},
        {"iat": int(time()) - 360},
        {"iat": int(time()) + 120},
        {"exp": int(time()) - 60},
        {"exp": int(time())},
    ],
)
async def test_signed_google_proof_rejects_wrong_authority_identity_email_or_time(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey, changes: Any
) -> None:
    verifier = _verifier(monkeypatch, signing_key)
    with pytest.raises(ConsumerSignInRejectedError) as caught:
        await verifier.verify("firebase-proof", google_id_token=_token(signing_key, **changes))
    assert str(caught.value) == "sign-in could not be verified"


@pytest.mark.parametrize(
    "identities",
    [
        None,
        {},
        {"google.com": []},
        {"google.com": "google-123"},
        {"google.com": ["google-123", "google-123"]},
        {"google.com": ["different-google-user"]},
        {"google.com": [123]},
        {"apple.com": ["google-123"]},
    ],
)
async def test_google_provider_binding_requires_one_exact_signed_firebase_identity(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey, identities: Any
) -> None:
    verifier = _verifier(
        monkeypatch,
        signing_key,
        firebase={"sign_in_provider": "google.com", "identities": identities},
    )
    with pytest.raises(ConsumerSignInRejectedError) as caught:
        await verifier.verify("firebase-proof", google_id_token=_token(signing_key))
    assert caught.value.reason is Reason.CLAIM_UID


@pytest.mark.parametrize("proof", [None, "", "malformed", "é", "\ud800", "x" * (16 * 1024 + 1)])
async def test_google_never_falls_back_to_primary_email_or_unsigned_profile(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey, proof: Any
) -> None:
    verifier = _verifier(
        monkeypatch, signing_key, email="consumer@example.test", email_verified=True
    )
    with pytest.raises(ConsumerSignInRejectedError) as caught:
        await verifier.verify("firebase-proof", google_id_token=proof)
    assert caught.value.reason is Reason.TOKEN_VERIFICATION


async def test_bad_signature_and_token_algorithm_cannot_supply_email(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    verifier = _verifier(monkeypatch, signing_key)
    foreign = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for proof in (
        _token(foreign),
        jwt.encode(
            _google_payload(),
            "synthetic-secret-with-at-least-32-bytes",
            algorithm="HS256",
            headers={"kid": "test"},
        ),
    ):
        with pytest.raises(ConsumerSignInRejectedError) as caught:
            await verifier.verify("firebase-proof", google_id_token=proof)
        assert caught.value.reason is Reason.TOKEN_VERIFICATION


async def test_google_key_outage_is_503_not_bad_credentials(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    verifier = _verifier(monkeypatch, signing_key)

    def unavailable(**kwargs: Any) -> Any:
        raise PyJWKClientConnectionError("private network detail")

    monkeypatch.setattr(verifier._google_keys._client, "get_signing_keys", unavailable)
    with pytest.raises(BrowserSessionUnavailableError) as caught:
        await verifier.verify("firebase-proof", google_id_token=_token(signing_key))
    assert str(caught.value) == "Google identity service unavailable"


@pytest.mark.parametrize(
    "firebase_changes",
    [
        {"email": "different@example.test"},
        {"email": "bad-value"},
        {"email_verified": "true"},
    ],
)
async def test_present_firebase_primary_email_cannot_conflict_with_google_proof(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey, firebase_changes: Any
) -> None:
    verifier = _verifier(monkeypatch, signing_key, **firebase_changes)
    with pytest.raises(ConsumerSignInRejectedError) as caught:
        await verifier.verify("firebase-proof", google_id_token=_token(signing_key))
    assert caught.value.reason is Reason.CLAIM_EMAIL


async def test_google_issuer_alias_without_azp_is_valid(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    verifier = _verifier(monkeypatch, signing_key)
    claims = _google_payload(iss="accounts.google.com")
    del claims["azp"]
    proof = jwt.encode(claims, signing_key, algorithm="RS256", headers={"kid": "test"})
    assert (await verifier.verify("firebase-proof", google_id_token=proof)).email


async def test_apple_remains_strict_and_rejects_extra_google_token_before_key_fetch(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey
) -> None:
    verifier = _verifier(
        monkeypatch,
        signing_key,
        email="private@privaterelay.appleid.com",
        email_verified=True,
        firebase={"sign_in_provider": "apple.com"},
    )
    lookup = AsyncMock(wraps=verifier._verify_google)
    monkeypatch.setattr(verifier, "_verify_google", lookup)
    assert (await verifier.verify("firebase-proof")).provider == "apple.com"
    with pytest.raises(ConsumerSignInRejectedError) as caught:
        await verifier.verify("firebase-proof", google_id_token=_token(signing_key))
    assert caught.value.reason is Reason.CLAIM_PROVIDER
    lookup.assert_not_awaited()
    monkeypatch.setattr(
        identity_module.auth,
        "verify_id_token",
        lambda *a, **kw: _claims(email_verified=False, firebase={"sign_in_provider": "apple.com"}),
    )
    with pytest.raises(ConsumerSignInRejectedError) as caught:
        await verifier.verify("firebase-proof")
    assert caught.value.reason is Reason.CLAIM_EMAIL
    assert _settings(
        identity_platform_providers=("apple.com",), identity_platform_google_client_id=None
    )


@pytest.mark.parametrize("missing", [False, True])
async def test_real_api_exchange_forwards_both_proofs_and_redacts_them(
    monkeypatch: pytest.MonkeyPatch, signing_key: rsa.RSAPrivateKey, missing: bool
) -> None:
    app, store, accounts = _api(monkeypatch, **_deferred())
    app.state.container.browser_session.verifier = _verifier(monkeypatch, signing_key)
    proof = _token(signing_key)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        challenge = await client.get("/auth/identity/start")
        response = await client.post(
            "/auth/identity/session",
            headers={"Origin": _ORIGIN},
            json={
                "state": challenge.json()["state"],
                "id_token": "firebase-proof",
                **({} if missing else {"google_id_token": proof}),
            },
        )
    assert response.status_code == (401 if missing else 200)
    assert bool(accounts.bootstrapped) is (not missing) and bool(store.sessions) is (not missing)
    assert proof not in response.text and "firebase-proof" not in response.text
    assert "no-store" in response.headers["Cache-Control"]


async def test_invalid_google_body_is_redacted_without_verification_or_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, store, _ = _api(monkeypatch, **_deferred())
    verify = AsyncMock()
    app.state.container.browser_session.complete_identity_login = verify
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        response = await client.post(
            "/auth/identity/session",
            headers={"Origin": _ORIGIN},
            json={
                "state": "s" * 43,
                "id_token": "private-firebase-proof",
                "google_id_token": "private-google-proof" * 1024,
            },
        )
    assert response.status_code == 422 and response.json() == {"detail": "invalid sign-in request"}
    assert "private" not in response.text and not store.sessions
    verify.assert_not_awaited()


@pytest.mark.parametrize(
    "client_id",
    [
        None,
        "",
        "other-client",
        "123-a.apps.googleusercontent.com\n",
        "123-" + "a" * 260 + ".apps.googleusercontent.com",
    ],
)
def test_google_verifier_cannot_start_without_exact_deployment_client(client_id: Any) -> None:
    with pytest.raises(ValueError, match="web OAuth client ID"):
        IdentityPlatformVerifier(_PROJECT, ("google.com",), client_id)


async def test_mixed_google_proof_cannot_be_retried_on_the_consumed_challenge(
    monkeypatch: pytest.MonkeyPatch,
    signing_key: rsa.RSAPrivateKey,
) -> None:
    app, store, accounts = _api(monkeypatch, **_deferred())
    app.state.container.browser_session.verifier = _verifier(monkeypatch, signing_key)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        challenge = await client.get("/auth/identity/start")
        body = {"state": challenge.json()["state"], "id_token": "firebase-proof"}
        mixed = await client.post(
            "/auth/identity/session",
            headers={"Origin": _ORIGIN},
            json=body | {"google_id_token": _token(signing_key, sub="another-google-user")},
        )
        replay = await client.post(
            "/auth/identity/session",
            headers={"Origin": _ORIGIN},
            json=body | {"google_id_token": _token(signing_key)},
        )
    assert mixed.status_code == replay.status_code == 401
    assert not store.sessions and not accounts.bootstrapped
