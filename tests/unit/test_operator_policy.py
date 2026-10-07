"""Offline policy validation, cloud response bounds and fail-closed refresh/revocation."""

from __future__ import annotations

import asyncio
import base64
import json
from threading import Event
from unittest.mock import Mock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from tests.unit.test_operator_api import _AUDIENCE, _key, _settings, _token

from events_concierge.adapters.operator_policy import (
    OperatorPolicyUnavailableError,
    ParameterManagerOperatorPolicy,
    parse_operator_policy,
)
from events_concierge.api.operator import create_operator_app
from events_concierge.api.operator_auth import IapOperatorIdentityVerifier
from events_concierge.config import Settings

VERSION = "projects/123/locations/global/parameters/ec-operator-rbac/versions/release-1"
SUBJECT = "accounts.google.com:123456"
EMAIL = "operator@example.test"


def payload(**overrides: object) -> bytes:
    return json.dumps(
        {
            "schema": 1,
            "bindings": [{"subject": SUBJECT, "email": EMAIL, "role": "reviewer"}],
            **overrides,
        }
    ).encode()


def production_settings(**overrides: object) -> Settings:
    return _settings(
        env="production",
        operator_subject_roles={},
        operator_allowed_email=None,
        operator_policy_version=VERSION,
        **overrides,
    )


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"x" * 65_537,
        b"not-json",
        b"\xff",
        payload(schema=True),
        payload(schema=2),
        payload(extra=True),
        b'{"schema":1,"schema":1,"bindings":[]}',
        payload(bindings="all"),
        payload(bindings=[{}]),
        payload(bindings=[{"subject": SUBJECT, "email": EMAIL, "role": "admin"}]),
        payload(bindings=[{"subject": SUBJECT, "email": EMAIL, "role": "viewer", "extra": 1}]),
        payload(bindings=[{"subject": "bad\nsubject", "email": EMAIL, "role": "viewer"}]),
        payload(bindings=[{"subject": SUBJECT, "email": "bad email", "role": "viewer"}]),
        payload(bindings=[{"subject": SUBJECT, "email": EMAIL, "role": "viewer"}] * 2),
        payload(
            bindings=[{"subject": str(i), "email": EMAIL, "role": "viewer"} for i in range(101)]
        ),
    ],
)
def test_invalid_policy_never_exposes_validation_contents(data: bytes) -> None:
    with pytest.raises(OperatorPolicyUnavailableError) as error:
        parse_operator_policy(data)
    assert str(error.value) == "operator authorization policy unavailable"
    assert EMAIL not in repr(error.value)


def test_explicit_empty_policy_denies_all_and_exact_identity_is_required() -> None:
    policy = parse_operator_policy(payload())
    assert policy.role_for(SUBJECT, EMAIL) == "reviewer"
    assert policy.role_for(SUBJECT, "another@example.test") is None
    assert policy.role_for("another-subject", EMAIL) is None
    assert policy.role_for(SUBJECT, None) is None
    assert parse_operator_policy(payload(bindings=[])).role_for(SUBJECT, EMAIL) is None
    assert EMAIL not in repr(policy)


@pytest.mark.parametrize(
    "version",
    [
        VERSION + "?view=BASIC",
        VERSION.replace("global", "us-west1"),
        VERSION.replace("release-1", "latest"),
        VERSION.replace("release-1", "LATEST"),
        VERSION.replace("release-1", "-bad"),
        VERSION.replace("ec-operator-rbac", "-bad"),
        VERSION.replace("123", "example-project"),
        "https://attacker.example.test",
        "",
    ],
)
def test_reference_never_accepts_external_urls_or_moving_versions(version: str) -> None:
    with pytest.raises(ValueError):
        ParameterManagerOperatorPolicy(version)


@pytest.mark.parametrize(
    "overrides",
    [
        {"operator_policy_version": None},
        {"operator_subject_roles": {SUBJECT: "reviewer"}},
        {"operator_allowed_email": EMAIL},
        {"operator_policy_cache_seconds": 61},
        {"operator_policy_cache_seconds": 0},
    ],
)
def test_production_settings_require_one_external_policy(overrides: dict[str, object]) -> None:
    values = production_settings().model_dump()
    values.update(overrides)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


async def test_cache_refresh_failure_never_reuses_previous_roles() -> None:
    now = [10.0]
    fetch = Mock(return_value=payload())
    source = ParameterManagerOperatorPolicy(VERSION, 30, fetch=fetch, clock=lambda: now[0])
    assert await source.role_for(SUBJECT, EMAIL) == "reviewer"
    now[0] = 39.0
    assert await source.role_for(SUBJECT, EMAIL) == "reviewer"
    assert fetch.call_count == 1
    now[0] = 40.0
    fetch.side_effect = RuntimeError("private cloud details " + EMAIL)
    with pytest.raises(OperatorPolicyUnavailableError) as error:
        await source.role_for(SUBJECT, EMAIL)
    assert EMAIL not in str(error.value)
    assert not await source.ready()
    assert fetch.call_count == 2
    now[0] = 42.0
    fetch.side_effect = None
    fetch.return_value = payload(bindings=[])
    assert await source.ready()
    assert await source.role_for(SUBJECT, EMAIL) is None


async def test_simultaneous_requests_share_one_refresh_and_caller_cancellation() -> None:
    started, release = Event(), Event()

    def fetch(version: str) -> bytes:
        assert version == VERSION
        started.set()
        assert release.wait(timeout=2)
        return payload()

    source = ParameterManagerOperatorPolicy(VERSION, fetch=fetch)
    first = asyncio.create_task(source.role_for(SUBJECT, EMAIL))
    assert await asyncio.to_thread(started.wait, 2)
    second = asyncio.create_task(source.role_for(SUBJECT, EMAIL))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    assert await second == "reviewer"


async def test_over_deadline_result_does_not_restore_permissions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("events_concierge.adapters.operator_policy._FETCH_SECONDS", 0.05)
    started, release = Event(), Event()
    count = [0]

    def fetch(version: str) -> bytes:
        count[0] += 1
        started.set()
        release.wait(timeout=2)
        return payload()

    source = ParameterManagerOperatorPolicy(VERSION, fetch=fetch)
    first = asyncio.create_task(source.role_for(SUBJECT, EMAIL))
    assert await asyncio.to_thread(started.wait, 2)
    with pytest.raises(OperatorPolicyUnavailableError):
        await first
    assert source._inflight is not None
    with pytest.raises(OperatorPolicyUnavailableError):
        await source.role_for(SUBJECT, EMAIL)
    assert count[0] == 1
    task = source._inflight
    release.set()
    assert task is not None
    with pytest.raises(OperatorPolicyUnavailableError):
        await task
    assert source._cached is None
    assert not await source.ready()


@pytest.mark.parametrize(
    "claims",
    [
        {"email": "another@example.test"},
        {"sub": "accounts.google.com:unassigned"},
        {"email": None},
    ],
)
async def test_verified_but_unassigned_claims_have_no_operator_access(
    claims: dict[str, object],
) -> None:
    source = ParameterManagerOperatorPolicy(VERSION, fetch=lambda version: payload())
    verifier = IapOperatorIdentityVerifier(audience=_AUDIENCE, policy=source, key_resolver=_key)
    with pytest.raises(HTTPException) as failure:
        await verifier.verify(_token(**claims))
    assert failure.value.status_code == 403
    assert (await verifier.verify(_token())).role == "reviewer"


async def test_jwt_is_verified_before_policy_lookup_and_policy_errors_are_sanitized() -> None:
    fetch = Mock(side_effect=RuntimeError("private cloud details " + EMAIL))
    source = ParameterManagerOperatorPolicy(VERSION, fetch=fetch)
    verifier = IapOperatorIdentityVerifier(audience=_AUDIENCE, policy=source, key_resolver=_key)
    with pytest.raises(HTTPException) as failure:
        await verifier.verify(_token(aud="wrong-audience"))
    assert failure.value.status_code == 401
    fetch.assert_not_called()
    with pytest.raises(HTTPException) as failure:
        await verifier.verify(_token())
    assert failure.value.status_code == 503
    assert failure.value.detail == "operator authorization unavailable"
    assert failure.value.headers == {"Cache-Control": "no-store, max-age=0"}


@pytest.mark.parametrize(
    "override",
    [
        {"disabled": True},
        {"disabled": "false"},
        {"name": VERSION + "-different"},
        {"payload": {}},
        {"payload": {"data": "not valid base64!"}},
        {"payload": {"data": 123}},
    ],
)
async def test_cloud_response_rejects_disabled_mismatched_or_malformed_versions(
    monkeypatch: pytest.MonkeyPatch, override: dict[str, object]
) -> None:
    document = {
        "name": VERSION,
        "payload": {"data": base64.b64encode(payload()).decode()},
        **override,
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=document))
    real_client = httpx.Client
    monkeypatch.setattr(
        "events_concierge.adapters.operator_policy.httpx.Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    credentials = Mock()
    monkeypatch.setattr(
        "events_concierge.adapters.operator_policy.google.auth.default",
        Mock(return_value=(credentials, None)),
    )
    source = ParameterManagerOperatorPolicy(VERSION)
    assert not await source.ready()


async def test_cloud_get_full_uses_adc_and_only_fixed_version_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert (
            str(request.url)
            == "https://parametermanager.googleapis.com/v1/" + VERSION + "?view=FULL"
        )
        assert request.headers["authorization"] == "Bearer offline-test"
        return httpx.Response(
            200,
            json={
                "name": VERSION,
                "payload": {"data": base64.b64encode(payload()).decode()},
            },
        )

    transport = httpx.MockTransport(respond)
    real_client = httpx.Client
    monkeypatch.setattr(
        "events_concierge.adapters.operator_policy.httpx.Client",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    credentials = Mock()
    credentials.before_request.side_effect = lambda request, method, url, headers: headers.update(
        authorization="Bearer offline-test"
    )
    discover = Mock(return_value=(credentials, None))
    monkeypatch.setattr("events_concierge.adapters.operator_policy.google.auth.default", discover)
    source = ParameterManagerOperatorPolicy(VERSION)
    assert await source.role_for(SUBJECT, EMAIL) == "reviewer"
    assert discover.call_args.kwargs["scopes"] == ["https://www.googleapis.com/auth/cloud-platform"]


async def test_unavailable_policy_prevents_startup_database_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("events_concierge.api.operator.preflight_operator_runtime", Mock())
    monkeypatch.setattr(ParameterManagerOperatorPolicy, "ready", lambda self: _false())
    build = Mock()
    monkeypatch.setattr("events_concierge.api.operator.build_operator_services", build)
    app = create_operator_app(production_settings())
    with pytest.raises(RuntimeError, match="operator authorization policy unavailable"):
        async with app.router.lifespan_context(app):
            pytest.fail("startup must reject an unusable policy")
    build.assert_not_called()


async def _false() -> bool:
    return False
