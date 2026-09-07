"""The release canary proves edge contracts without creating application state."""

from __future__ import annotations

import json

import httpx
import pytest

from events_concierge.operations.canary import CanaryOptions, run_canary

_REVISION = "0123456789abcdef0123456789abcdef01234567"
_DIGEST = "sha256:" + "a" * 64


def _response(
    status: int,
    *,
    json_body: object | None = None,
    text: str = "",
    headers: dict[str, str] | None = None,
) -> httpx.Response:
    content = json.dumps(json_body).encode() if json_body is not None else text.encode()
    response_headers = dict(headers or {})
    if json_body is not None:
        response_headers.setdefault("Content-Type", "application/json")
    return httpx.Response(status, content=content, headers=response_headers)


def _production_handler(  # noqa: PLR0911, PLR0912
    request: httpx.Request,
) -> httpx.Response:
    path = request.url.path
    if path == "/healthz":
        return _response(200, json_body={"status": "ok"})
    if path == "/readyz":
        return _response(
            200,
            json_body={
                "status": "ready",
                "components": {
                    "database": "ready",
                    "temporal": "ready",
                    "identity": "ready",
                },
            },
        )
    if path == "/":
        return _response(
            200,
            text="<!doctype html><title>Events Concierge</title>",
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "Cache-Control": "no-cache, max-age=0",
                "Content-Security-Policy": (
                    "default-src 'self'; script-src 'self'; style-src 'self'; "
                    "connect-src 'self'; object-src 'none'; base-uri 'none'; "
                    "frame-ancestors 'none'; form-action 'self'"
                ),
                "Referrer-Policy": "strict-origin-when-cross-origin",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Permissions-Policy": (
                    "camera=(), microphone=(), geolocation=(), payment=()"
                ),
            },
        )
    if path == "/assets/app.css":
        return _response(
            200,
            text="body{}",
            headers={
                "Content-Type": "text/css",
                "Cache-Control": "no-cache, max-age=0",
                "X-Content-Type-Options": "nosniff",
            },
        )
    if path == "/assets/app.js":
        return _response(
            200,
            text="'use strict';",
            headers={
                "Content-Type": "text/javascript",
                "Cache-Control": "no-cache, max-age=0",
                "X-Content-Type-Options": "nosniff",
            },
        )
    if path == "/manifest.webmanifest":
        return _response(
            200,
            text="{}",
            headers={
                "Content-Type": "application/manifest+json",
                "Cache-Control": "no-cache, max-age=0",
                "X-Content-Type-Options": "nosniff",
            },
        )
    if path == "/v1/ui-config":
        return _response(
            200,
            json_body={
                "product_name": "Events Concierge",
                "local_demo": False,
                "auth_mode": "deployment_session",
                "auth_start_url": "/auth/login",
                "reauth_url": "/auth/reauth",
                "logout_url": "/auth/logout",
                "csrf_cookie_name": "__Host-ec_csrf",
                "csrf_header_name": "X-EC-CSRF",
            },
        )
    if path == "/auth/login":
        return _response(400, json_body={"detail": "invalid application return path"})
    if path == "/auth/reauth":
        assert request.method == "POST"
        assert request.headers.get("origin") == "https://staging.concierge.example"
        return _response(401, json_body={"detail": "authentication required"})
    if path == "/v1/onboard":
        return _response(404, json_body={"detail": "Not Found"})
    if path == "/v1/me":
        if request.headers.get("cookie") == "session=fixture-cookie":
            return _response(200, json_body={"notify_email": "redacted"})
        return _response(401, json_body={"detail": "authentication required"})
    if path == "/v1/feed-feedback":
        assert request.headers.get("cookie") == "session=fixture-cookie"
        assert request.headers.get("origin") == "https://staging.concierge.example"
        return _response(403, json_body={"detail": "CSRF verification failed"})
    if path == "/auth/logout":
        assert request.headers.get("cookie") == "session=fixture-cookie"
        assert request.headers.get("origin") == "https://staging.concierge.example"
        assert request.headers.get("x-ec-csrf") == "fixture-csrf-token"
        return _response(204)
    if path == "/versionz":
        return _response(
            200,
            json_body={"release_revision": _REVISION, "image_digest": _DIGEST},
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    if path == "/metrics":
        return _response(
            200,
            text=(
                "events_concierge_build_info 1\n"
                "events_concierge_http_requests_total{method=\"GET\"} 1\n"
            ),
            headers={
                "Content-Type": "text/plain; version=0.0.4; charset=utf-8",
                "Cache-Control": "no-store, max-age=0",
            },
        )
    raise AssertionError(f"unexpected canary path {path}")


def test_production_canary_passes_and_never_records_the_session_cookie() -> None:
    report = run_canary(
        CanaryOptions(
            base_url="https://staging.concierge.example",
            expected_release_revision=_REVISION,
            expected_image_digest=_DIGEST,
            session_cookie="session=fixture-cookie",
            csrf_token="fixture-csrf-token",
        ),
        transport=httpx.MockTransport(_production_handler),
    )

    rendered = str(report.to_dict())
    assert report.passed is True
    assert report.to_dict()["status"] == "passed"
    assert report.to_dict()["evidence_class"] == "deployment_canary"
    assert report.to_dict()["release_eligible"] is False
    assert "fixture-cookie" not in rendered
    assert "fixture-csrf-token" not in rendered
    assert {check.name for check in report.checks} >= {
        "database_ready",
        "temporal_ready",
        "mock_onboarding_absent",
        "unsafe_login_redirect_rejected",
        "unauthenticated_reauth_rejected",
        "local_tenant_header_rejected",
        "release_identity_contract",
        "metrics_contract",
        "deployment_session",
        "missing_csrf_rejected",
        "csrf_bound_logout_accepted",
    }


def test_temporal_degradation_and_missing_edge_header_fail_the_canary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        response = _production_handler(request)
        if request.url.path == "/readyz":
            return _response(
                200,
                json_body={
                    "status": "ready",
                    "components": {
                        "database": "ready",
                        "temporal": "degraded",
                        "identity": "ready",
                    },
                },
            )
        if request.url.path == "/":
            headers = dict(response.headers)
            headers.pop("x-frame-options")
            headers["content-security-policy"] += "; script-src 'unsafe-inline'"
            return _response(200, text=response.text, headers=headers)
        return response

    report = run_canary(
        CanaryOptions(
            base_url="https://staging.concierge.example",
            expected_release_revision=_REVISION,
            expected_image_digest=_DIGEST,
        ),
        transport=httpx.MockTransport(handler),
    )

    assert report.passed is False
    assert next(check for check in report.checks if check.name == "temporal_ready").passed is False
    assert next(
        check for check in report.checks if check.name == "consumer_shell_x-frame-options"
    ).passed is False
    assert next(
        check for check in report.checks if check.name == "consumer_shell_content-security-policy"
    ).passed is False


@pytest.mark.parametrize("reauth_url", [None, "/auth/login", "https://identity.example/reauth"])
def test_production_canary_rejects_missing_or_drifted_reauthentication_route(
    reauth_url: str | None,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/ui-config":
            response = _production_handler(request)
            body = response.json()
            body["reauth_url"] = reauth_url
            return _response(200, json_body=body)
        return _production_handler(request)

    report = run_canary(
        CanaryOptions(
            base_url="https://staging.concierge.example",
            expected_release_revision=_REVISION,
            expected_image_digest=_DIGEST,
        ),
        transport=httpx.MockTransport(handler),
    )

    assert report.passed is False
    assert next(check for check in report.checks if check.name == "ui_config_contract").passed is False


def test_local_smoke_mode_is_non_mutating_and_can_allow_temporal_degradation() -> None:
    observed_onboard_body: bytes | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal observed_onboard_body
        if request.url.path == "/v1/ui-config":
            return _response(
                200,
                json_body={
                    "product_name": "Events Concierge",
                    "local_demo": True,
                    "auth_mode": "local_demo",
                    "auth_start_url": None,
                },
            )
        if request.url.path == "/v1/onboard":
            observed_onboard_body = request.content
            return _response(422, json_body={"detail": "invalid fixture"})
        if request.url.path == "/readyz":
            return _response(
                200,
                json_body={
                    "status": "ready",
                    "components": {
                        "database": "ready",
                        "temporal": "degraded",
                        "identity": "not_configured",
                    },
                },
            )
        return _production_handler(request)

    report = run_canary(
        CanaryOptions(
            base_url="http://127.0.0.1:8000",
            allow_http=True,
            allow_local_mode=True,
            require_temporal=False,
        ),
        transport=httpx.MockTransport(handler),
    )

    assert report.passed is True
    assert observed_onboard_body == b"{}"


@pytest.mark.parametrize(
    "options",
    [
        CanaryOptions(base_url="http://public.example"),
        CanaryOptions(
            base_url="https://app.foo.localhost",
            expected_release_revision=_REVISION,
            expected_image_digest=_DIGEST,
        ),
        CanaryOptions(base_url="https://user:secret@public.example"),
        CanaryOptions(base_url="https://public.example/path"),
        CanaryOptions(base_url="https://public.example?token=secret"),
        CanaryOptions(
            base_url="https://public.example",
            expected_image_digest="latest",
        ),
        CanaryOptions(
            base_url="https://public.example",
            expected_release_revision="main",
            expected_image_digest=_DIGEST,
        ),
    ],
)
def test_canary_rejects_ambiguous_or_mutable_release_targets(options: CanaryOptions) -> None:
    with pytest.raises(ValueError):
        run_canary(options, transport=httpx.MockTransport(_production_handler))


def test_csrf_acceptance_probe_cannot_run_without_a_throwaway_session() -> None:
    with pytest.raises(ValueError, match="throwaway session"):
        run_canary(
            CanaryOptions(
                base_url="https://public.example",
                expected_release_revision=_REVISION,
                expected_image_digest=_DIGEST,
                csrf_token="secret",
            ),
            transport=httpx.MockTransport(_production_handler),
        )


@pytest.mark.parametrize(
    "options",
    [
        CanaryOptions(
            base_url="https://staging.concierge.example",
            expected_release_revision=_REVISION,
        ),
        CanaryOptions(
            base_url="https://staging.concierge.example",
            expected_image_digest=_DIGEST,
        ),
        CanaryOptions(
            base_url="http://127.0.0.1:8000",
            allow_http=True,
            expected_release_revision=_REVISION,
            expected_image_digest=_DIGEST,
        ),
        CanaryOptions(
            base_url="https://staging.concierge.example",
            require_temporal=False,
            expected_release_revision=_REVISION,
            expected_image_digest=_DIGEST,
        ),
        CanaryOptions(
            base_url="http://remote.example",
            allow_http=True,
            allow_local_mode=True,
        ),
        CanaryOptions(
            base_url="http://127.0.0.1:8000",
            allow_http=True,
            allow_local_mode=True,
            session_cookie="session=must-not-cross-plaintext",
        ),
    ],
)
def test_production_canary_requires_release_identity_and_forbids_local_relaxations(
    options: CanaryOptions,
) -> None:
    with pytest.raises(ValueError):
        run_canary(options, transport=httpx.MockTransport(_production_handler))
