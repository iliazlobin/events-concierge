"""Signed operator identity, capability separation and durable acceptance without provider I/O."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from time import time
from typing import cast
from uuid import UUID

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from jwt import PyJWK
from pydantic import ValidationError

from events_concierge.api.admin import IngestionSourceConfigurationBody
from events_concierge.api.operator import create_operator_app
from events_concierge.api.operator_auth import IAP_ISSUER, IapOperatorIdentityVerifier, OperatorRole
from events_concierge.config import Settings

_AUDIENCE = "/projects/123/global/backendServices/456"
_ORIGIN = "https://ops.example.test"
_SUBJECT = "accounts.google.com:123456"
_COMMAND_ID = "a1111111-1111-4111-8111-111111111111"
_PRIVATE = ec.generate_private_key(ec.SECP256R1())
_JWK = PyJWK.from_dict(
    {
        **jwt.algorithms.ECAlgorithm.to_jwk(_PRIVATE.public_key(), as_dict=True),
        "kid": "iap-key",
        "alg": "ES256",
    }
)


async def _key(token: str) -> PyJWK:
    if jwt.get_unverified_header(token).get("kid") != "iap-key":
        raise jwt.PyJWKClientError("unknown key")
    return _JWK


def _token(**overrides: object) -> str:
    now = int(time())
    claims: dict[str, object] = {
        "iss": IAP_ISSUER,
        "aud": _AUDIENCE,
        "sub": _SUBJECT,
        "email": "operator@example.test",
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    return jwt.encode(claims, _PRIVATE, algorithm="ES256", headers={"kid": "iap-key"})


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "env": "test",
        "mock_cloud": False,
        "operator_api_enabled": True,
        "operator_iap_audience": _AUDIENCE,
        "operator_public_origin": _ORIGIN,
        "operator_subject_roles": {_SUBJECT: "reviewer"},
        "operator_allowed_email": "operator@example.test",
        "operator_database_url": "postgresql+psycopg://operator_login:secret@db.example.test/ec?sslmode=verify-full",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


class _Commands:
    def __init__(self) -> None:
        self.commands: dict[UUID, dict[str, object]] = {}
        self.actors: list[str] = []

    async def list_commands(self, limit: int) -> list[dict[str, object]]:
        return list(self.commands.values())[:limit]

    async def enqueue_command(
        self, *, command_id: UUID, action: str, source_key: str | None, requested_by: str
    ) -> dict[str, object]:
        self.actors.append(requested_by)
        if command_id not in self.commands:
            self.commands[command_id] = {
                "command_id": command_id,
                "action": action,
                "source_key": source_key,
                "status": "queued",
                "requested_at": datetime.now(UTC),
                "started_at": None,
                "completed_at": None,
                "result": None,
                "error_code": None,
            }
        return self.commands[command_id]


def _app(role: str = "reviewer") -> tuple[FastAPI, _Commands]:
    app = create_operator_app(_settings())
    app.state.operator_identity_verifier = IapOperatorIdentityVerifier(
        audience=_AUDIENCE,
        subject_roles={_SUBJECT: cast("OperatorRole", role)},
        key_resolver=_key,
    )
    service = _Commands()
    app.state.ingestion_admin = service
    return app, service


def _headers(token: str | None = None) -> dict[str, str]:
    return {"x-goog-iap-jwt-assertion": token or _token(), "origin": _ORIGIN}


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": f"Bearer {_token()}"},
        {
            "x-goog-authenticated-user-id": _SUBJECT,
            "x-goog-authenticated-user-email": "operator@example.test",
        },
    ],
)
async def test_unsigned_headers_and_consumer_bearer_never_grant_operator_authority(
    headers: dict[str, str],
) -> None:
    app, _ = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        response = await client.get("/admin/v1/operator/session", headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize("email", [None, "other@example.test", "another@example.test"])
async def test_owner_only_operator_rejects_every_other_verified_email(email: str | None) -> None:
    app, _ = _app()
    app.state.operator_identity_verifier = IapOperatorIdentityVerifier(
        audience=_AUDIENCE,
        subject_roles={_SUBJECT: "reviewer"},
        allowed_email="operator@example.test",
        key_resolver=_key,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        headers = _headers(_token(email=email)) | {
            "x-goog-authenticated-user-email": "accounts.google.com:operator@example.test",
        }
        assert (await client.get("/admin/v1/operator/session", headers=headers)).status_code == 403
        assert (
            await client.get(
                "/admin/v1/operator/session",
                headers=_headers(_token(email="operator@example.test")),
            )
        ).status_code == 200
        assert (
            await client.get(
                "/admin/v1/operator/session",
                headers=_headers(_token(email="operator@example.test", sub="unassigned-subject")),
            )
        ).status_code == 403


@pytest.mark.parametrize(
    "claims",
    [
        {"iss": "https://accounts.google.com"},
        {"aud": "consumer-client"},
        {"aud": [_AUDIENCE]},
        {"exp": 1},
        {"iat": int(time()) + 600},
        {"sub": "unassigned-subject"},
        {"sub": "bad\nsubject"},
        {"exp": int(time()) + 7200},
    ],
)
async def test_iap_claims_are_verified_before_read_access(claims: dict[str, object]) -> None:
    app, _ = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        response = await client.get(
            "/admin/v1/operator/session", headers=_headers(_token(**claims))
        )
    assert response.status_code in {401, 403}


async def test_wrong_signing_key_and_duplicate_assertions_are_rejected() -> None:
    app, _ = _app()
    claims = jwt.decode(_token(), options={"verify_signature": False})
    wrong = jwt.encode(
        claims,
        ec.generate_private_key(ec.SECP256R1()),
        algorithm="ES256",
        headers={"kid": "iap-key"},
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        assert (
            await client.get("/admin/v1/operator/session", headers=_headers(wrong))
        ).status_code == 401
        assert (
            await client.get(
                "/admin/v1/operator/session",
                headers=[
                    ("x-goog-iap-jwt-assertion", _token()),
                    ("x-goog-iap-jwt-assertion", _token()),
                ],
            )
        ).status_code == 401


async def test_viewer_can_read_but_cannot_enqueue() -> None:
    app, service = _app("viewer")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        session = await client.get("/admin/v1/operator/session", headers=_headers())
        response = await client.post(
            "/admin/v1/ingestion/commands",
            headers=_headers(),
            json={
                "command_id": _COMMAND_ID,
                "action": "refresh_source",
                "source_key": "city-events",
            },
        )
    assert session.json()["capabilities"] == ["ingestion.read"]
    assert response.status_code == 403
    assert service.actors == []


@pytest.mark.parametrize("origin", [None, "https://consumer.example.test", "null", _ORIGIN + "/"])
async def test_mutations_require_exact_configured_origin(origin: str | None) -> None:
    app, service = _app("operator")
    headers = _headers()
    if origin is None:
        headers.pop("origin")
    else:
        headers["origin"] = origin
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        response = await client.post(
            "/admin/v1/ingestion/commands",
            headers=headers,
            json={
                "command_id": _COMMAND_ID,
                "action": "refresh_due",
            },
        )
    assert response.status_code == 403
    assert service.actors == []


async def test_operator_receipt_replay_uses_stable_verified_actor_after_token_rotation() -> None:
    app, service = _app("operator")
    body = {"command_id": _COMMAND_ID, "action": "refresh_due"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        first = await client.post("/admin/v1/ingestion/commands", json=body, headers=_headers())
        replay = await client.post(
            "/admin/v1/ingestion/commands",
            json=body,
            headers=_headers(_token(iat=int(time()) - 10)),
        )
        forged = await client.post(
            "/admin/v1/ingestion/commands",
            json={**body, "requested_by": "admin"},
            headers=_headers(),
        )
    assert first.status_code == replay.status_code == 202
    assert first.json() == replay.json()
    assert forged.status_code == 422
    assert service.actors == [f"iap:{_SUBJECT}", f"iap:{_SUBJECT}"]
    assert len(service.commands) == 1


async def test_request_body_is_bounded_and_no_provider_graph_is_needed() -> None:
    app, service = _app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        response = await client.post(
            "/admin/v1/ingestion/commands", headers=_headers(), content=b"x" * 20000
        )
        absent = await client.get("/v1/me", headers=_headers())
    assert response.status_code == 413
    assert absent.status_code == 404
    assert service.actors == []


@pytest.mark.parametrize(
    "override",
    [
        {"mock_cloud": True},
        {"operator_subject_roles": {}},
        {"operator_public_origin": "http://ops.example.test"},
        {"operator_database_url": None},
        {"operator_iap_audience": None},
    ],
)
def test_partial_hosted_operator_profile_is_rejected(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        _settings(**override)


@pytest.mark.parametrize("role, expected", [("viewer", 403), ("operator", 403), ("reviewer", 200)])
async def test_source_configuration_requires_reviewer_capability(role: str, expected: int) -> None:
    app, service = _app(role)
    updates: list[dict[str, object]] = []

    async def update(source_key: str, **values: object) -> dict[str, object]:
        updates.append(values)
        now = datetime.now(UTC)
        return {
            "source_key": source_key,
            "source_revision": 2,
            "reviewed_at": now,
            "updated_at": now,
        }

    service.update_source_configuration = update  # type: ignore[attr-defined]
    body = {
        "expected_revision": 1,
        "seed_url": "https://events.example.test/list",
        "approved_origins": ["https://events.example.test"],
        "mode": "public_jsonld",
        "enabled": True,
        "handoff_only": True,
        "review_expires_at": None,
        "refresh_interval_minutes": 60,
        "min_interval_ms": 1500,
        "page_limit": 1,
        "review_acknowledged": True,
        "collection_horizon_days": 30,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        response = await client.patch(
            "/admin/v1/ingestion/sources/city-events", headers=_headers(), json=body
        )
    assert response.status_code == expected
    assert len(updates) == (1 if role == "reviewer" else 0)
    if updates:
        assert updates[0]["requested_by"] == f"iap:{_SUBJECT}"
        assert updates[0]["collection_horizon_days"] == 30


@pytest.mark.parametrize("days", [0, 91, -1, True, "30", 1.5])
def test_collection_horizon_input_is_a_bounded_integer(days: object) -> None:
    with pytest.raises(ValidationError):
        IngestionSourceConfigurationBody.model_validate(
            {
                "expected_revision": 1,
                "seed_url": "https://events.example.test/list",
                "approved_origins": ["https://events.example.test"],
                "mode": "public_jsonld",
                "enabled": True,
                "handoff_only": True,
                "review_expires_at": None,
                "refresh_interval_minutes": 1440,
                "min_interval_ms": 1500,
                "page_limit": 1,
                "review_acknowledged": True,
                "collection_horizon_days": days,
            }
        )


async def test_operator_cannot_bypass_same_origin_with_misleading_fetch_metadata() -> None:
    app, service = _app("operator")
    async with AsyncClient(transport=ASGITransport(app=app), base_url=_ORIGIN) as client:
        response = await client.post(
            "/admin/v1/ingestion/commands",
            headers={
                **_headers(),
                "Sec-Fetch-Site": "cross-site",
            },
            json={"command_id": _COMMAND_ID, "action": "refresh_due"},
        )
    assert response.status_code == 403
    assert service.actors == []


def test_file_resolved_operator_settings_are_not_resolved_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret_path = tmp_path / "operator-dsn"
    secret_path.write_text(
        "postgresql+psycopg://operator_login:secret@db.example.test/ec?sslmode=verify-full"
    )
    monkeypatch.setenv("EC_OPERATOR_DATABASE_URL_FILE", str(secret_path))
    settings = Settings(
        _env_file=None,
        env="test",
        mock_cloud=False,
        operator_api_enabled=True,
        operator_iap_audience=_AUDIENCE,
        operator_public_origin=_ORIGIN,
        operator_subject_roles={_SUBJECT: "reviewer"},
        operator_allowed_email="operator@example.test",
    )
    app = create_operator_app(settings)
    assert app.state.settings.operator_database_url == settings.operator_database_url
