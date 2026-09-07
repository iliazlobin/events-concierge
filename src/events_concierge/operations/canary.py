"""Non-mutating staging/release canary with sanitized evidence output."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from .network_safety import is_loopback_host, is_non_remote_host

_SHA256_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_SEMANTIC_VERSION = re.compile(
    r"^v?(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_MAX_PORT = 65_535
_MAX_TIMEOUT_SECONDS = 60.0
_CSRF_HEADER_NAME = "X-EC-CSRF"
_SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ),
    "referrer-policy": ("strict-origin-when-cross-origin",),
    "x-content-type-options": ("nosniff",),
    "x-frame-options": ("DENY",),
    "permissions-policy": ("camera=()", "microphone=()", "geolocation=()", "payment=()"),
}


@dataclass(frozen=True, slots=True)
class CanaryOptions:
    """Expected deployment truth; secrets never belong in this value."""

    base_url: str
    expected_release_revision: str | None = None
    expected_image_digest: str | None = None
    allow_http: bool = False
    allow_local_mode: bool = False
    require_temporal: bool = True
    session_cookie: str | None = None
    csrf_token: str | None = None
    timeout_seconds: float = 5.0


@dataclass(frozen=True, slots=True)
class CanaryCheck:
    name: str
    passed: bool
    detail: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class CanaryReport:
    """A credential-free release evidence record."""

    generated_at: str
    origin: str
    expected_release_revision: str | None
    expected_image_digest: str | None
    checks: tuple[CanaryCheck, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "kind": "events-concierge-release-canary",
            "evidence_class": "deployment_canary",
            "release_eligible": False,
            "generated_at": self.generated_at,
            "origin": self.origin,
            "expected_release_revision": self.expected_release_revision,
            "expected_image_digest": self.expected_image_digest,
            "status": "passed" if self.passed else "failed",
            "checks": [asdict(check) for check in self.checks],
        }


def run_canary(
    options: CanaryOptions,
    *,
    transport: httpx.BaseTransport | None = None,
) -> CanaryReport:
    """Run a bounded release canary without creating product or provider state.

    If a deployment supplies a session cookie from its secret store, the canary additionally proves
    that the session resolves and that a valid authenticated mutation is rejected without CSRF
    evidence.  The cookie is never included in the returned report.
    """

    origin = _validate_options(options)
    checks: list[CanaryCheck] = []
    with httpx.Client(
        base_url=origin,
        timeout=options.timeout_seconds,
        follow_redirects=False,
        transport=transport,
        headers={"User-Agent": "events-concierge-release-canary/1"},
    ) as client:
        health = _request(client, "GET", "/healthz", checks, "liveness")
        if health is not None:
            _expect_json(
                health,
                checks,
                "liveness_contract",
                lambda body: body == {"status": "ok"},
                "liveness reports status=ok",
            )

        readiness = _request(client, "GET", "/readyz", checks, "readiness")
        if readiness is not None:
            _check_readiness(
                readiness,
                checks,
                require_temporal=options.require_temporal,
                require_identity=not options.allow_local_mode,
            )

        shell = _request(client, "GET", "/", checks, "consumer_shell")
        if shell is not None:
            _check_shell(shell, checks)

        for path, media_prefix, name in (
            ("/assets/app.css", "text/css", "consumer_css"),
            ("/assets/app.js", "text/javascript", "consumer_javascript"),
            ("/manifest.webmanifest", "application/manifest+json", "consumer_manifest"),
        ):
            response = _request(client, "GET", path, checks, name)
            if response is not None:
                _expect(
                    checks,
                    f"{name}_content_type",
                    response.headers.get("content-type", "").startswith(media_prefix),
                    f"{name} has its expected content type",
                    f"{name} returned an unexpected content type",
                )
                _expect(
                    checks,
                    f"{name}_nosniff",
                    response.headers.get("x-content-type-options") == "nosniff",
                    f"{name} disables MIME sniffing",
                    f"{name} did not disable MIME sniffing",
                )
                _expect(
                    checks,
                    f"{name}_cache_policy",
                    "no-cache" in response.headers.get("cache-control", "").casefold(),
                    f"{name} requires revalidation",
                    f"{name} did not require cache revalidation",
                )

        ui_config = _request(client, "GET", "/v1/ui-config", checks, "ui_config")
        if ui_config is not None:
            _check_ui_config(ui_config, checks, options)

        if not options.allow_local_mode:
            unsafe_login = _request(
                client,
                "GET",
                "/auth/login?return_to=https%3A%2F%2Fevil.invalid%2Fapp",
                checks,
                "unsafe_login_redirect_rejected",
                expected_status=400,
            )
            del unsafe_login
            unauthenticated_reauth = _request(
                client,
                "POST",
                "/auth/reauth",
                checks,
                "unauthenticated_reauth_rejected",
                expected_status=401,
                headers={"Origin": origin},
                json={"return_to": "/app#/settings"},
            )
            del unauthenticated_reauth

        onboard = _request(
            client,
            "POST",
            "/v1/onboard",
            checks,
            "mock_onboarding_absent",
            expected_status=404 if not options.allow_local_mode else 422,
            # An invalid body proves route presence/absence without creating a local fixture user.
            json={},
        )
        del onboard

        if not options.allow_local_mode:
            local_header = _request(
                client,
                "GET",
                "/v1/me",
                checks,
                "local_tenant_header_rejected",
                expected_status=401,
                headers={"X-EC-Tenant-ID": str(uuid4())},
            )
            del local_header

        if options.expected_release_revision or options.expected_image_digest:
            version = _request(client, "GET", "/versionz", checks, "release_identity")
            if version is not None:
                _check_release_identity(version, checks, options)

        metrics = _request(client, "GET", "/metrics", checks, "metrics")
        if metrics is not None:
            _expect(
                checks,
                "metrics_contract",
                "events_concierge_build_info" in metrics.text
                and "events_concierge_http_requests_total" in metrics.text,
                "bounded application metrics are exposed",
                "expected application metric families are absent",
            )
            _expect(
                checks,
                "metrics_content_type",
                metrics.headers.get("content-type", "").startswith(
                    "text/plain; version=0.0.4"
                ),
                "metrics use the Prometheus text content type",
                "metrics returned an unexpected content type",
            )
            _expect(
                checks,
                "metrics_cache_policy",
                "no-store" in metrics.headers.get("cache-control", "").casefold(),
                "metrics disable response caching",
                "metrics did not disable response caching",
            )

        if options.session_cookie is not None:
            _check_authenticated_boundary(
                client,
                checks,
                options.session_cookie,
                trusted_origin=origin,
                csrf_token=options.csrf_token,
            )

    return CanaryReport(
        generated_at=datetime.now(UTC).isoformat(),
        origin=origin,
        expected_release_revision=options.expected_release_revision,
        expected_image_digest=options.expected_image_digest,
        checks=tuple(checks),
    )


def _validate_options(options: CanaryOptions) -> str:
    try:
        parsed = urlsplit(options.base_url)
        port = parsed.port
    except ValueError as error:
        raise ValueError("canary base URL is malformed") from error
    allowed_schemes = {"https", "http"} if options.allow_http else {"https"}
    if (
        parsed.scheme not in allowed_schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or (port is not None and not 1 <= port <= _MAX_PORT)
    ):
        raise ValueError("canary base URL must be an HTTPS origin without path/query/userinfo")
    if options.timeout_seconds <= 0 or options.timeout_seconds > _MAX_TIMEOUT_SECONDS:
        raise ValueError("canary timeout must be within (0, 60] seconds")
    if (options.allow_http or not options.require_temporal) and not options.allow_local_mode:
        raise ValueError("HTTP and Temporal-degraded canaries require explicit local mode")
    if options.allow_local_mode and not is_loopback_host(parsed.hostname):
        raise ValueError("local-mode canary must target a loopback origin")
    if not options.allow_local_mode and is_non_remote_host(parsed.hostname):
        raise ValueError("production canary must target a remote origin")
    if parsed.scheme == "http" and (
        options.session_cookie is not None or options.csrf_token is not None
    ):
        raise ValueError("authenticated canary evidence requires HTTPS")
    if not options.allow_local_mode and (
        options.expected_release_revision is None or options.expected_image_digest is None
    ):
        raise ValueError("production canary requires an expected revision and image digest")
    if options.expected_image_digest is not None and not _SHA256_DIGEST.fullmatch(
        options.expected_image_digest
    ):
        raise ValueError("expected image digest must use sha256:<64 lowercase hex characters>")
    if options.expected_release_revision is not None and not (
        _COMMIT_REVISION.fullmatch(options.expected_release_revision)
        or _SEMANTIC_VERSION.fullmatch(options.expected_release_revision)
    ):
        raise ValueError("expected release revision must be a full commit or semantic version")
    if options.csrf_token is not None and options.session_cookie is None:
        raise ValueError("CSRF acceptance canary requires a throwaway session cookie")
    return f"{parsed.scheme}://{parsed.netloc}"


def _request(
    client: httpx.Client,
    method: str,
    path: str,
    checks: list[CanaryCheck],
    name: str,
    *,
    expected_status: int = 200,
    **kwargs: Any,
) -> httpx.Response | None:
    started = monotonic()
    try:
        response = client.request(method, path, **kwargs)
    except httpx.HTTPError as error:
        checks.append(
            CanaryCheck(
                name=name,
                passed=False,
                detail=f"request failed ({type(error).__name__})",
                duration_ms=_duration_ms(started),
            )
        )
        return None
    checks.append(
        CanaryCheck(
            name=name,
            passed=response.status_code == expected_status,
            detail=(
                f"HTTP {response.status_code} matched the release contract"
                if response.status_code == expected_status
                else f"expected HTTP {expected_status}, received HTTP {response.status_code}"
            ),
            duration_ms=_duration_ms(started),
        )
    )
    return response if response.status_code == expected_status else None


def _check_readiness(
    response: httpx.Response,
    checks: list[CanaryCheck],
    *,
    require_temporal: bool,
    require_identity: bool,
) -> None:
    try:
        body = response.json()
    except ValueError:
        _expect(checks, "readiness_contract", False, "", "readiness did not return JSON")
        return
    components = body.get("components", {}) if isinstance(body, dict) else {}
    _expect(
        checks,
        "readiness_contract",
        isinstance(body, dict) and body.get("status") == "ready",
        "readiness reports the serving surface ready",
        "readiness did not report the serving surface ready",
    )
    database_ready = components.get("database") == "ready"
    temporal_ready = components.get("temporal") == "ready"
    identity_ready = components.get("identity") == "ready"
    _expect(
        checks,
        "database_ready",
        database_ready,
        "durable database reports ready",
        "durable database does not report ready",
    )
    _expect(
        checks,
        "temporal_ready",
        temporal_ready or not require_temporal,
        "Temporal reports ready" if temporal_ready else "Temporal degradation was explicitly allowed",
        "Temporal does not report ready",
    )
    _expect(
        checks,
        "identity_ready",
        identity_ready or not require_identity,
        "browser identity control plane reports ready"
        if identity_ready
        else "browser identity readiness was explicitly not required",
        "browser identity control plane does not report ready",
    )


def _check_shell(response: httpx.Response, checks: list[CanaryCheck]) -> None:
    _expect(
        checks,
        "consumer_shell_content_type",
        response.headers.get("content-type", "").startswith("text/html"),
        "consumer shell is HTML",
        "consumer shell has an unexpected content type",
    )
    _expect(
        checks,
        "consumer_shell_cache_policy",
        "no-cache" in response.headers.get("cache-control", "").casefold(),
        "consumer shell requires cache revalidation",
        "consumer shell did not require cache revalidation",
    )
    for header, fragments in _SECURITY_HEADERS.items():
        value = response.headers.get(header, "")
        passed = bool(value) and all(fragment in value for fragment in fragments)
        if header == "content-security-policy":
            passed = passed and "'unsafe-inline'" not in value and "'unsafe-eval'" not in value
        _expect(
            checks,
            f"consumer_shell_{header}",
            passed,
            f"consumer shell enforces {header}",
            f"consumer shell is missing the required {header} policy",
        )


def _check_ui_config(
    response: httpx.Response,
    checks: list[CanaryCheck],
    options: CanaryOptions,
) -> None:
    try:
        body = response.json()
    except ValueError:
        _expect(checks, "ui_config_contract", False, "", "UI config did not return JSON")
        return
    expected_mode = "local_demo" if options.allow_local_mode else "deployment_session"
    passed = (
        isinstance(body, dict)
        and body.get("auth_mode") == expected_mode
        and body.get("local_demo") is options.allow_local_mode
        and (options.allow_local_mode or bool(body.get("auth_start_url")))
        and (
            options.allow_local_mode
            or (
                body.get("auth_start_url") == "/auth/login"
                and body.get("reauth_url") == "/auth/reauth"
                and body.get("logout_url") == "/auth/logout"
                and body.get("csrf_cookie_name") == "__Host-ec_csrf"
                and body.get("csrf_header_name") == _CSRF_HEADER_NAME
            )
        )
    )
    _expect(
        checks,
        "ui_config_contract",
        passed,
        f"UI config reports {expected_mode}",
        f"UI config does not report the required {expected_mode} contract",
    )


def _check_release_identity(
    response: httpx.Response,
    checks: list[CanaryCheck],
    options: CanaryOptions,
) -> None:
    try:
        body = response.json()
    except ValueError:
        _expect(checks, "release_identity_contract", False, "", "release identity was not JSON")
        return
    passed = isinstance(body, dict)
    if options.expected_release_revision is not None:
        passed = passed and body.get("release_revision") == options.expected_release_revision
    if options.expected_image_digest is not None:
        passed = passed and body.get("image_digest") == options.expected_image_digest
    _expect(
        checks,
        "release_identity_contract",
        passed,
        "serving release matches the expected immutable identity",
        "serving release does not match the expected immutable identity",
    )
    _expect(
        checks,
        "release_identity_cache_policy",
        "no-store" in response.headers.get("cache-control", "").casefold(),
        "release identity disables response caching",
        "release identity did not disable response caching",
    )


def _check_authenticated_boundary(
    client: httpx.Client,
    checks: list[CanaryCheck],
    session_cookie: str,
    *,
    trusted_origin: str,
    csrf_token: str | None,
) -> None:
    headers = {"Cookie": session_cookie, "Origin": trusted_origin}
    session = _request(
        client,
        "GET",
        "/v1/me",
        checks,
        "deployment_session",
        headers=headers,
    )
    if session is None:
        return
    missing_csrf = _request(
        client,
        "POST",
        "/v1/feed-feedback",
        checks,
        "missing_csrf_rejected",
        expected_status=403,
        headers=headers,
        json={
            "signal_id": str(uuid4()),
            "canonical_event_id": str(uuid4()),
            "kind": "click",
        },
    )
    del missing_csrf
    if csrf_token is not None:
        logout = _request(
            client,
            "POST",
            "/auth/logout",
            checks,
            "csrf_bound_logout_accepted",
            expected_status=204,
            headers={
                "Cookie": session_cookie,
                "Origin": trusted_origin,
                _CSRF_HEADER_NAME: csrf_token,
            },
        )
        del logout


def _expect_json(
    response: httpx.Response,
    checks: list[CanaryCheck],
    name: str,
    predicate: Any,
    success: str,
) -> None:
    try:
        body = response.json()
    except ValueError:
        _expect(checks, name, False, success, "response did not contain valid JSON")
        return
    _expect(checks, name, bool(predicate(body)), success, "JSON response violated the contract")


def _expect(
    checks: list[CanaryCheck],
    name: str,
    passed: bool,
    success: str,
    failure: str,
) -> None:
    checks.append(
        CanaryCheck(
            name=name,
            passed=passed,
            detail=success if passed else failure,
            duration_ms=0,
        )
    )


def _duration_ms(started: float) -> int:
    return max(round((monotonic() - started) * 1000), 0)
