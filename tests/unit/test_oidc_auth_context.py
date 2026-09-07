"""Offline signed-token authentication coverage (FR-1.1/1.3, AC-1/AC-2)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt import PyJWK

import events_concierge.adapters.oidc.auth as oidc_auth
from events_concierge.adapters.oidc import OidcJwtAuthContext
from events_concierge.ports.auth import AuthenticationFailedError

_ISSUER = "https://identity.example.test"
_AUDIENCE = "events-concierge-api"
_TENANT_CLAIM = "https://events.example.test/tenant_id"
_KEY_ID = "fixture-signing-key"
_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _public_jwk(key_id: str) -> PyJWK:
    payload = jwt.algorithms.RSAAlgorithm.to_jwk(_PRIVATE_KEY.public_key(), as_dict=True)
    assert isinstance(payload, dict)
    payload["kid"] = key_id
    return PyJWK.from_dict(payload)


_PUBLIC_JWK = _public_jwk(_KEY_ID)


class _Clock:
    def __init__(self) -> None:
        self._now = 1_000.0

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _FakeJwksClient:
    def __init__(self, keys: list[PyJWK]) -> None:
        self.keys = keys
        self.fetch_calls = 0

    def get_signing_keys(self, *, refresh: bool = False) -> list[PyJWK]:
        assert refresh is True
        self.fetch_calls += 1
        return list(self.keys)


def _adapter() -> OidcJwtAuthContext:
    return OidcJwtAuthContext(
        issuer=_ISSUER,
        audience=_AUDIENCE,
        jwks_url=f"{_ISSUER}/.well-known/jwks.json",
        tenant_claim=_TENANT_CLAIM,
        signing_key_resolver=lambda _token: _PUBLIC_JWK,
    )


def _token(
    tenant_id: UUID | str,
    *,
    issuer: str = _ISSUER,
    audience: str | list[str] = _AUDIENCE,
    authorized_party: str | None = None,
    expires_delta: timedelta = timedelta(minutes=5),
    algorithm: str = "RS256",
    key_id: str = _KEY_ID,
    subject: object = "oidc|fixture-user",
    nonce: str | None = None,
    auth_time: object | None = None,
) -> str:
    now = datetime.now(UTC)
    claims: dict[str, object] = {
        "iss": issuer,
        "sub": subject,
        "aud": audience,
        "iat": now,
        "exp": now + expires_delta,
        _TENANT_CLAIM: str(tenant_id),
    }
    if authorized_party is not None:
        claims["azp"] = authorized_party
    if nonce is not None:
        claims["nonce"] = nonce
    if auth_time is not None:
        claims["auth_time"] = auth_time
    key: object = _PRIVATE_KEY
    if algorithm == "HS256":
        key = "fixture-symmetric-key-that-must-never-be-trusted"
    return jwt.encode(claims, key, algorithm=algorithm, headers={"kid": key_id})


async def test_oidc_auth_context_resolves_only_the_signed_tenant_claim() -> None:
    tenant_id = uuid4()

    resolved = await _adapter().resolve_tenant_id(
        {
            "Authorization": f"Bearer {_token(tenant_id)}",
            "X-EC-Tenant-ID": str(uuid4()),
        }
    )

    assert resolved == tenant_id


async def test_oidc_identity_verification_binds_subject_and_exact_transaction_nonce() -> None:
    tenant_id = uuid4()
    token = _token(tenant_id, nonce="fixture-nonce")

    identity = await _adapter().verify_identity_token(
        token,
        expected_nonce="fixture-nonce",
    )

    assert identity.tenant_id == tenant_id
    assert identity.subject == "oidc|fixture-user"
    with pytest.raises(AuthenticationFailedError):
        await _adapter().verify_identity_token(token, expected_nonce="other-nonce")
    with pytest.raises(AuthenticationFailedError):
        await _adapter().verify_identity_token(
            _token(tenant_id),
            expected_nonce="fixture-nonce",
        )
    with pytest.raises(AuthenticationFailedError):
        await _adapter().verify_identity_token(
            _token(tenant_id, subject="bad\nsubject"),
        )


async def test_oidc_step_up_requires_a_numeric_provider_auth_time() -> None:
    tenant_id = uuid4()
    authenticated_at = int(datetime.now(UTC).timestamp())

    identity = await _adapter().verify_identity_token(
        _token(tenant_id, nonce="step-up", auth_time=authenticated_at),
        expected_nonce="step-up",
        require_auth_time=True,
    )

    assert identity.authenticated_at == authenticated_at
    for invalid_auth_time in (None, True, "123"):
        with pytest.raises(AuthenticationFailedError):
            await _adapter().verify_identity_token(
                _token(tenant_id, nonce="step-up", auth_time=invalid_auth_time),
                expected_nonce="step-up",
                require_auth_time=True,
            )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer  token"},
        {"Authorization": "Bearer token, Bearer other"},
        {"Authorization": "bearer token"},
    ],
)
async def test_oidc_auth_context_rejects_missing_or_ambiguous_authorization(
    headers: dict[str, str],
) -> None:
    with pytest.raises(AuthenticationFailedError, match="valid tenant authentication is required"):
        await _adapter().resolve_tenant_id(headers)


@pytest.mark.parametrize(
    "token",
    [
        _token(uuid4(), issuer="https://attacker.example.test"),
        _token(uuid4(), audience="another-api"),
        _token(uuid4(), expires_delta=timedelta(minutes=-5)),
        _token(str(uuid4()).upper()),
        _token(uuid4(), audience=[_AUDIENCE, "another-api"]),
        _token(
            uuid4(),
            audience=[_AUDIENCE, "another-api"],
            authorized_party="another-api",
        ),
        _token(uuid4(), authorized_party="another-api"),
        _token(uuid4(), algorithm="HS256"),
    ],
)
async def test_oidc_auth_context_rejects_untrusted_claims_or_algorithm(token: str) -> None:
    with pytest.raises(AuthenticationFailedError, match="valid tenant authentication is required"):
        await _adapter().resolve_tenant_id({"Authorization": f"Bearer {token}"})


async def test_oidc_auth_context_accepts_bound_multi_audience_token() -> None:
    tenant_id = uuid4()
    token = _token(
        tenant_id,
        audience=[_AUDIENCE, "identity-userinfo"],
        authorized_party=_AUDIENCE,
    )

    resolved = await _adapter().resolve_tenant_id({"Authorization": f"Bearer {token}"})

    assert resolved == tenant_id


async def test_oidc_auth_context_single_flights_and_rate_limits_unknown_key_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unique attacker-selected key IDs cannot produce one HTTPS fetch apiece."""
    clock = _Clock()
    jwks_client = _FakeJwksClient([_PUBLIC_JWK])
    monkeypatch.setattr(oidc_auth, "monotonic", clock)
    monkeypatch.setattr(
        oidc_auth,
        "PyJWKClient",
        lambda *_args, **_kwargs: jwks_client,
    )
    adapter = OidcJwtAuthContext(
        issuer=_ISSUER,
        audience=_AUDIENCE,
        jwks_url=f"{_ISSUER}/.well-known/jwks.json",
        tenant_claim=_TENANT_CLAIM,
    )

    tenant_id = uuid4()
    concurrent_initial = await asyncio.gather(
        *[
            adapter.resolve_tenant_id({"Authorization": f"Bearer {_token(tenant_id)}"})
            for _ in range(16)
        ]
    )
    assert concurrent_initial == [tenant_id] * 16
    assert jwks_client.fetch_calls == 1

    clock.advance(31)
    attempted = await asyncio.gather(
        *[
            adapter.resolve_tenant_id(
                {"Authorization": (f"Bearer {_token(tenant_id, key_id=f'attacker-key-{index}')}")}
            )
            for index in range(64)
        ],
        return_exceptions=True,
    )

    assert all(isinstance(result, AuthenticationFailedError) for result in attempted)
    assert jwks_client.fetch_calls == 2


async def test_oidc_auth_context_accepts_a_rotated_key_after_bounded_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    jwks_client = _FakeJwksClient([_PUBLIC_JWK])
    monkeypatch.setattr(oidc_auth, "monotonic", clock)
    monkeypatch.setattr(
        oidc_auth,
        "PyJWKClient",
        lambda *_args, **_kwargs: jwks_client,
    )
    adapter = OidcJwtAuthContext(
        issuer=_ISSUER,
        audience=_AUDIENCE,
        jwks_url=f"{_ISSUER}/.well-known/jwks.json",
        tenant_claim=_TENANT_CLAIM,
    )
    tenant_id = uuid4()
    await adapter.resolve_tenant_id({"Authorization": f"Bearer {_token(tenant_id)}"})

    rotated_key_id = "rotated-signing-key"
    jwks_client.keys = [_public_jwk(rotated_key_id)]
    rotated_token = _token(tenant_id, key_id=rotated_key_id)
    with pytest.raises(AuthenticationFailedError):
        await adapter.resolve_tenant_id({"Authorization": f"Bearer {rotated_token}"})
    assert jwks_client.fetch_calls == 1

    clock.advance(31)
    resolved = await adapter.resolve_tenant_id({"Authorization": f"Bearer {rotated_token}"})

    assert resolved == tenant_id
    assert jwks_client.fetch_calls == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("issuer", "http://identity.example.test"),
        ("issuer", "https://identity.example.test:0"),
        ("issuer", "https://identity.example.test:65536"),
        ("issuer", "https://identity.example.test:not-a-port"),
        ("issuer", "https://identity.example.test:"),
        ("issuer", "https://identity.example.test/issuer?tenant=fixture"),
        ("issuer", "https://identity.example.test/issuer\ninjected"),
        ("issuer", "https://[::1"),
        ("jwks_url", "file:///etc/passwd"),
        ("jwks_url", "https://identity.example.test:65536/jwks"),
        ("jwks_url", "https://identity.example.test/jw ks"),
        ("jwks_url", "https://identity.example.test/jwks#"),
        ("algorithms", ("HS256",)),
    ],
)
def test_oidc_auth_context_rejects_unsafe_deployment_configuration(
    field: str, value: object
) -> None:
    kwargs: dict[str, object] = {
        "issuer": _ISSUER,
        "audience": _AUDIENCE,
        "jwks_url": f"{_ISSUER}/.well-known/jwks.json",
        "tenant_claim": _TENANT_CLAIM,
    }
    kwargs[field] = value

    with pytest.raises(ValueError):
        OidcJwtAuthContext(**kwargs)  # type: ignore[arg-type]


def test_oidc_auth_context_allows_a_fixed_provider_owned_jwks_query() -> None:
    """Some providers use a configured version/routing query; JWT input cannot alter this URL."""
    OidcJwtAuthContext(
        issuer=_ISSUER,
        audience=_AUDIENCE,
        jwks_url=f"{_ISSUER}/.well-known/jwks.json?version=2026-07-18",
        tenant_claim=_TENANT_CLAIM,
        signing_key_resolver=lambda _token: _PUBLIC_JWK,
    )
