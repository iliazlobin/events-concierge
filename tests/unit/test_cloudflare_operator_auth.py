"""Offline signed Access tokens never turn consumer or email headers into operator authority."""

from __future__ import annotations

from time import time
from unittest.mock import patch

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from jwt import PyJWK
from pydantic import ValidationError

from events_concierge.api.operator import create_operator_app
from events_concierge.api.operator_auth import CloudflareAccessOperatorIdentityVerifier
from events_concierge.config import Settings

TEAM = "https://validation-team.cloudflareaccess.com"
AUD = "a" * 64
SUBJECT = "7335d417-61da-459d-899c-0a01c76a2f94"
ORIGIN = "https://admin-events.iliazlobin.com"
PRIVATE = rsa.generate_private_key(public_exponent=65537, key_size=2048)
JWK = PyJWK.from_dict(
    {
        **jwt.algorithms.RSAAlgorithm.to_jwk(PRIVATE.public_key(), as_dict=True),
        "kid": "cf-key",
        "alg": "RS256",
        "use": "sig",
    }
)


def claims(**overrides: object) -> dict[str, object]:
    now = int(time())
    return {
        "iss": TEAM,
        "aud": [AUD],
        "iat": now,
        "exp": now + 600,
        "nbf": now,
        "sub": SUBJECT,
        "email": "iliazlobin91@gmail.com",
        "type": "app",
        **overrides,
    }


def token(**overrides: object) -> str:
    return jwt.encode(claims(**overrides), PRIVATE, algorithm="RS256", headers={"kid": "cf-key"})


async def key(assertion: str) -> PyJWK:
    if jwt.get_unverified_header(assertion).get("kid") != "cf-key":
        raise jwt.PyJWKClientError("unknown key")
    return JWK


def verifier() -> CloudflareAccessOperatorIdentityVerifier:
    return CloudflareAccessOperatorIdentityVerifier(
        team_domain=TEAM, audience=AUD, subject_roles={SUBJECT: "reviewer"}, key_resolver=key
    )


def settings(**overrides: object) -> Settings:
    values = {
        "_env_file": None,
        "env": "staging",
        "mock_cloud": False,
        "operator_api_enabled": True,
        "operator_auth_provider": "cloudflare_access",
        "operator_cloudflare_team_domain": TEAM,
        "operator_cloudflare_audience": AUD,
        "operator_public_origin": ORIGIN,
        "operator_subject_roles": {SUBJECT: "reviewer"},
        "operator_database_url": "postgresql+psycopg://operator_login:secret@db.example.test/ec?sslmode=verify-full",
        **overrides,
    }
    return Settings(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("audience", [[AUD], AUD])
async def test_exact_application_audience_and_stable_signed_actor(audience: object) -> None:
    principal = await verifier().verify(token(aud=audience))
    assert principal.actor == f"cloudflare_access:{SUBJECT}"
    assert principal.role == "reviewer"
    assert "models.budget.configure" in principal.capabilities
    assert (await verifier().verify(token(iat=int(time()) - 10))).actor == principal.actor


@pytest.mark.parametrize(
    "override",
    [
        {"iss": "https://another-team.cloudflareaccess.com"},
        {"iss": TEAM + "/"},
        {"aud": "consumer-client"},
        {"aud": [AUD, "other-app"]},
        {"aud": [AUD, AUD]},
        {"aud": []},
        {"exp": 1},
        {"iat": int(time()) + 600},
        {"nbf": int(time()) + 600},
        {"exp": int(time()) + 7200},
        {"exp": True},
        {"iat": True},
        {"nbf": True},
        {"iat": str(int(time()))},
        {"type": "org"},
        {"type": None},
        {"sub": ""},
        {"sub": "bad\nsubject"},
        {"sub": "x" * 201},
        {"sub": 123},
    ],
)
async def test_malicious_claims_fail_closed(override: dict[str, object]) -> None:
    with pytest.raises(HTTPException) as failure:
        await verifier().verify(token(**override))
    assert failure.value.status_code == 401
    assert failure.value.headers == {"Cache-Control": "no-store, max-age=0"}


@pytest.mark.parametrize("missing", ["iss", "aud", "iat", "exp", "nbf", "sub", "email", "type"])
async def test_all_required_application_claims_are_signed(missing: str) -> None:
    payload = claims()
    del payload[missing]
    assertion = jwt.encode(payload, PRIVATE, algorithm="RS256", headers={"kid": "cf-key"})
    with pytest.raises(HTTPException) as failure:
        await verifier().verify(assertion)
    assert failure.value.status_code == 401


@pytest.mark.parametrize(
    "override",
    [
        {"email": "attacker@example.test"},
        {"email": "ILIAZLOBIN91@gmail.com"},
        {"sub": "another-verified-user"},
    ],
)
async def test_owner_email_and_configured_subject_are_both_required(
    override: dict[str, object],
) -> None:
    with pytest.raises(HTTPException) as failure:
        await verifier().verify(token(**override))
    assert failure.value.status_code == 403


async def test_forged_wrong_algorithm_unknown_key_and_oversized_assertions_are_rejected() -> None:
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    invalid = [
        jwt.encode(claims(), other, algorithm="RS256", headers={"kid": "cf-key"}),
        jwt.encode(claims(), "attacker-secret" * 3, algorithm="HS256", headers={"kid": "cf-key"}),
        jwt.encode(claims(), PRIVATE, algorithm="RS256", headers={"kid": "unknown"}),
        jwt.encode(
            claims(), PRIVATE, algorithm="RS256", headers={"kid": "cf-key", "crit": ["custom"]}
        ),
        "",
        "not.a.jwt",
        "x" * 8193,
    ]
    for assertion in invalid:
        with pytest.raises(HTTPException) as failure:
            await verifier().verify(assertion)
        assert failure.value.status_code == 401


async def test_key_endpoint_is_derived_only_from_the_configured_team() -> None:
    class Keys:
        def get_signing_keys(self, refresh: bool) -> list[PyJWK]:
            assert refresh
            return [JWK]

    with patch("events_concierge.api.operator_auth.PyJWKClient", return_value=Keys()) as client:
        identity = CloudflareAccessOperatorIdentityVerifier(
            team_domain=TEAM, audience=AUD, subject_roles={SUBJECT: "reviewer"}
        )
        assertion = jwt.encode(
            claims(),
            PRIVATE,
            algorithm="RS256",
            headers={
                "kid": "cf-key",
                "jku": "https://attacker.example.test/keys",
            },
        )
        assert (await identity.verify(assertion)).subject == SUBJECT
        client.assert_called_once_with(
            TEAM + "/cdn-cgi/access/certs", timeout=5, cache_jwk_set=True
        )


async def test_api_selects_only_the_configured_signed_assertion() -> None:
    app = create_operator_app(settings())
    assert isinstance(
        app.state.operator_identity_verifier, CloudflareAccessOperatorIdentityVerifier
    )
    app.state.operator_identity_verifier = verifier()
    app.state.ingestion_admin = object()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        for headers in [
            {},
            {"authorization": "Bearer " + token()},
            {"x-goog-iap-jwt-assertion": token()},
            {"cf-access-authenticated-user-email": "iliazlobin91@gmail.com"},
        ]:
            assert (
                await client.get("/admin/v1/operator/session", headers=headers)
            ).status_code == 401
        assert (
            await client.get(
                "/admin/v1/operator/session",
                headers=[
                    ("cf-access-jwt-assertion", token()),
                    ("cf-access-jwt-assertion", token()),
                ],
            )
        ).status_code == 401
        response = await client.get(
            "/admin/v1/operator/session",
            headers={
                "cf-access-jwt-assertion": token(),
                "cf-access-authenticated-user-email": "attacker@example.test",
            },
        )
    assert response.status_code == 200
    assert response.json()["authentication"] == "cloudflare_access"
    assert response.json()["subject"] == SUBJECT


@pytest.mark.parametrize(
    "override",
    [
        {"operator_auth_provider": "headers"},
        {"operator_cloudflare_team_domain": "http://team.cloudflareaccess.com"},
        {"operator_cloudflare_team_domain": TEAM + "/"},
        {"operator_cloudflare_team_domain": TEAM + ".attacker.test"},
        {"operator_cloudflare_team_domain": "https://user@validation-team.cloudflareaccess.com"},
        {"operator_cloudflare_team_domain": TEAM + ":443"},
        {"operator_cloudflare_audience": ""},
        {"operator_cloudflare_audience": "consumer-client"},
        {"operator_subject_roles": {}},
        {"operator_iap_audience": "/projects/123/global/backendServices/456"},
    ],
)
def test_incomplete_or_mixed_provider_settings_cannot_start(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        settings(**override)
