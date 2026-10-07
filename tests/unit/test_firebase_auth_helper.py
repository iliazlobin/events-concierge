"""Fixed-destination helper transport without provider calls or application credentials."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from starlette.requests import Request

from events_concierge.api import firebase_auth_helper as helper_module
from events_concierge.config import Settings
from events_concierge.infra.logging import configure_logging, redact_sensitive_event

_ORIGIN = "https://events.example.test"
_PROJECT = "events-identity-test"


def settings(**changes: Any) -> Settings:
    return Settings(
        _env_file=None,
        **{
            "mock_cloud": False,
            "identity_platform_enabled": True,
            "identity_platform_project_id": _PROJECT,
            "identity_platform_api_key": "restricted-browser-key-fixture",
            "identity_platform_auth_domain": "events.example.test",
            "identity_platform_google_client_id": "123456789-consumer-web.apps.googleusercontent.com",
            "identity_platform_providers": ("google.com",),
            "public_base_url": _ORIGIN,
            "consumer_legal_mode": "deferred",
            **changes,
        },
    )


async def relay(
    path: str, upstream: Any, *, config: Settings | None = None, **kwargs: Any
) -> httpx.Response:
    app = FastAPI()
    helper_module.install_firebase_auth_helper(app, config or settings())
    real_client = httpx.AsyncClient

    def fixed_client(**options: Any) -> httpx.AsyncClient:
        assert options == {"trust_env": False, "follow_redirects": False, "timeout": 20}
        return real_client(transport=httpx.MockTransport(upstream), **options)

    async with real_client(transport=httpx.ASGITransport(app=app), base_url=_ORIGIN) as browser:
        with patch.object(helper_module.httpx, "AsyncClient", fixed_client):
            return await browser.request(kwargs.pop("method", "GET"), path, **kwargs)


@pytest.mark.parametrize("helper", sorted(helper_module.HELPERS))
async def test_named_helpers_preserve_bytes_query_and_csp_without_cookies(helper: str) -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert (
            str(request.url)
            == f"https://{_PROJECT}.firebaseapp.com/__/auth/{helper}?state=fixture&code=secret%2Bvalue"
        )
        assert request.method == "GET"
        assert request.headers.get("accept") == "text/html"
        assert request.headers.get("accept-encoding") == "identity"
        for name in (
            "cookie",
            "authorization",
            "x-goog-iap-jwt-assertion",
            "forwarded",
            "x-forwarded-host",
            "referer",
            "origin",
        ):
            assert name not in request.headers
        return httpx.Response(
            200,
            content=b"<script>window.fixture = true;</script>",
            headers={
                "content-type": "text/html; charset=UTF-8",
                "content-security-policy": "script-src 'self'",
                "x-frame-options": "SAMEORIGIN",
                "set-cookie": "__Host-ec_session=attacker; Secure; Path=/",
                "cache-control": "public, max-age=3600",
                "x-provider-debug": "private-value",
            },
        )

    response = await relay(
        f"/__/auth/{helper}?state=fixture&code=secret%2Bvalue",
        upstream,
        headers={
            "Accept": "text/html",
            "Cookie": "__Host-ec_session=consumer; GCP_IAP_AUTH_TOKEN=admin",
            "Authorization": "Bearer consumer",
            "X-Goog-IAP-JWT-Assertion": "admin.jwt.bytes",
            "Forwarded": "host=attacker",
            "X-Forwarded-Host": "attacker",
            "Referer": _ORIGIN + "/admin?private=secret",
            "Origin": _ORIGIN,
        },
    )
    assert response.status_code == 200
    assert response.content == b"<script>window.fixture = true;</script>"
    assert response.headers["content-security-policy"] == "script-src 'self'"
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert "set-cookie" not in response.headers and "x-provider-debug" not in response.headers


async def test_provider_callback_post_preserves_form_and_does_not_follow_redirect() -> None:
    calls = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert request.content == b"state=one&code=fixture%2Bvalue"
        assert request.headers["content-type"] == "application/x-www-form-urlencoded"
        return httpx.Response(
            302, headers={"Location": "https://accounts.google.com/o/oauth2/auth?state=one"}
        )

    response = await relay(
        "/__/auth/handler",
        upstream,
        method="POST",
        content=b"state=one&code=fixture%2Bvalue",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert response.status_code == 302 and len(calls) == 1
    assert response.headers["location"] == "https://accounts.google.com/o/oauth2/auth?state=one"


@pytest.mark.parametrize(
    "path",
    [
        "/__/auth/credentials",
        "/__/auth/action",
        "/__/auth/handler/extra",
        "/__/auth/%68andler",
        "/__/auth/handler%2f",
        "/__/auth//handler",
        "/__/firebase/init.json",
        "/__/auth/handler.js/",
    ],
)
async def test_unlisted_or_ambiguous_paths_never_call_upstream(path: str) -> None:
    response = await relay(path, lambda _: pytest.fail("unexpected upstream call"))
    assert response.status_code == 404


@pytest.mark.parametrize(
    "method,path",
    [
        ("PUT", "handler"),
        ("DELETE", "iframe"),
        ("HEAD", "handler"),
        ("POST", "iframe"),
        ("POST", "handler.js"),
    ],
)
async def test_method_boundary(method: str, path: str) -> None:
    response = await relay(
        f"/__/auth/{path}", lambda _: pytest.fail("unexpected upstream call"), method=method
    )
    assert response.status_code == 405


async def test_query_body_and_decoded_response_limits() -> None:
    def no_call(_: httpx.Request) -> httpx.Response:
        pytest.fail("unexpected upstream call")

    assert (await relay("/__/auth/handler?" + "x" * 8193, no_call)).status_code == 400
    assert (
        await relay("/__/auth/handler", no_call, method="POST", content=b"x" * (64 * 1024 + 1))
    ).status_code == 413
    assert (
        await relay(
            "/__/auth/handler", lambda _: httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
        )
    ).status_code == 502


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/steal",
        "http://accounts.google.com/auth",
        "https://user:password@accounts.google.com/auth",
        f"https://{_PROJECT}.firebaseapp.com/admin",
        "https://accounts.google.com:444/auth",
    ],
)
async def test_unapproved_redirects_fail_without_following_or_echo(location: str) -> None:
    response = await relay(
        "/__/auth/handler", lambda _: httpx.Response(302, headers={"location": location})
    )
    assert response.status_code == 502 and "location" not in response.headers
    assert location.encode() not in response.content


async def test_same_host_helper_redirect_rewrites_authority_only() -> None:
    response = await relay(
        "/__/auth/handler",
        lambda _: httpx.Response(302, headers={"location": "/__/auth/iframe?code=fixture#state"}),
    )
    assert response.headers["location"] == _ORIGIN + "/__/auth/iframe?code=fixture#state"


async def test_inactive_auth_domain_keeps_proxy_closed() -> None:
    response = await relay(
        "/__/auth/handler",
        lambda _: pytest.fail("unexpected upstream call"),
        config=settings(identity_platform_auth_domain=f"{_PROJECT}.firebaseapp.com"),
    )
    assert response.status_code == 404


async def test_disabled_identity_keeps_helpers_closed() -> None:
    response = await relay(
        "/__/auth/iframe",
        lambda _: pytest.fail("unexpected upstream call"),
        config=settings(identity_platform_enabled=False, consumer_legal_mode="required"),
    )
    assert response.status_code == 404


async def test_total_upstream_deadline_is_a_static_error() -> None:
    async def stalled(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.02)
        return httpx.Response(200)

    with patch.object(helper_module, "UPSTREAM_SECONDS", 0.001):
        app = FastAPI()
        helper_module.install_firebase_auth_helper(app, settings())
        real_client = httpx.AsyncClient
        async with real_client(transport=httpx.ASGITransport(app=app), base_url=_ORIGIN) as browser:
            with patch.object(
                helper_module.httpx,
                "AsyncClient",
                lambda **opts: real_client(transport=httpx.MockTransport(stalled), **opts),
            ):
                response = await browser.get("/__/auth/iframe?state=private-deadline-fixture")
    assert response.status_code == 502 and "private-deadline-fixture" not in response.text


async def test_request_body_deadline_precedes_any_upstream() -> None:
    async def receive() -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/__/auth/handler",
            "raw_path": b"/__/auth/handler",
            "query_string": b"",
            "headers": [],
        },
        receive,
    )
    with patch.object(
        helper_module.httpx, "AsyncClient", side_effect=AssertionError("unexpected upstream")
    ):
        response = await helper_module._relay(
            request, "handler", settings(request_body_timeout_seconds=0.1)
        )
    assert response.status_code == 408


async def test_network_errors_do_not_return_or_log_provider_values(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging("debug")

    def unavailable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("state=do-not-log-me code=do-not-log-me", request=request)

    response = await relay("/__/auth/handler?code=do-not-log-me", unavailable)
    assert response.status_code == 502
    assert "do-not-log-me" not in response.text
    assert "do-not-log-me" not in caplog.text
    logging.getLogger("httpx").info(
        "HTTP Request: GET https://host/__/auth/handler?code=do-not-log-me"
    )
    assert "do-not-log-me" not in caplog.text
    event = redact_sensitive_event(
        None,
        "error",
        {"error": "https://host/__/auth/handler?state=private-state&credential=private-credential"},
    )
    assert "private-state" not in str(event) and "private-credential" not in str(event)


@pytest.mark.parametrize("domain", [f"{_PROJECT}.firebaseapp.com", "events.example.test"])
def test_only_fixed_project_or_exact_application_auth_domain_is_valid(domain: str) -> None:
    assert settings(identity_platform_auth_domain=domain).identity_platform_auth_domain == domain


@pytest.mark.parametrize(
    "domain",
    [
        "attacker.example",
        "events.example.test:443",
        "events.example.test/__/auth",
        "events.example.test.attacker.example",
        "https://events.example.test",
        "other-project.firebaseapp.com",
    ],
)
def test_other_auth_domains_refused(domain: str) -> None:
    with pytest.raises(ValidationError):
        settings(identity_platform_auth_domain=domain)


@pytest.mark.parametrize("port", ["444", "invalid"])
def test_same_origin_helper_rejects_a_nonstandard_or_invalid_https_port(port: str) -> None:
    with pytest.raises(ValidationError):
        settings(public_base_url=f"{_ORIGIN}:{port}")
