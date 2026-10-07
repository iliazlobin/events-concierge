"""Same-origin Firebase helpers; fixed hosting upstream, no application credentials."""

from __future__ import annotations

import asyncio
from urllib.parse import urljoin, urlsplit

import httpx
from fastapi import FastAPI, Request
from starlette.requests import ClientDisconnect
from starlette.responses import Response

from ..config import Settings

HELPERS = frozenset(
    {"handler", "handler.js", "iframe", "iframe.js", "experiments.js", "links", "links.js"}
)
MAX_QUERY_BYTES = 8192
MAX_BODY_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
UPSTREAM_SECONDS = 20
_MIN_PRINTABLE = 32
_DELETE_CHARACTER = 127
_REQUEST_HEADERS = frozenset({"accept", "content-type"})
_RESPONSE_HEADERS = frozenset({"content-type", "content-security-policy", "x-frame-options"})
_SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    # Helper scripts use the restricted browser key; cross-site referrers contain no URL state.
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
}


class HelperRejectedError(Exception):
    """Static proxy rejection; never carry provider request/response content."""


def _error(status: int) -> Response:
    return Response("Sign-in helper unavailable", status_code=status, headers=_SECURITY_HEADERS)


def _location(value: str, hosting_origin: str, public_origin: str) -> str:
    if len(value.encode()) > MAX_QUERY_BYTES or any(ord(char) < _MIN_PRINTABLE for char in value):
        raise HelperRejectedError
    parsed = urlsplit(urljoin(hosting_origin, value))
    if (
        parsed.username
        or parsed.password
        or parsed.scheme != "https"
        or parsed.port not in {None, 443}
    ):
        raise HelperRejectedError
    origin = f"https://{parsed.netloc}"
    if origin == hosting_origin:
        if parsed.path not in {f"/__/auth/{helper}" for helper in HELPERS}:
            raise HelperRejectedError
        return (
            public_origin
            + parsed.path
            + (f"?{parsed.query}" if parsed.query else "")
            + (f"#{parsed.fragment}" if parsed.fragment else "")
        )
    if origin not in {public_origin, "https://accounts.google.com", "https://appleid.apple.com"}:
        raise HelperRejectedError
    return value


async def _bounded_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_BODY_BYTES:
            raise HelperRejectedError
    return bytes(body)


def _request_status(request: Request, helper: str, settings: Settings) -> int | None:
    public_origin = settings.public_base_url.rstrip("/")
    if (
        not settings.identity_platform_enabled
        or settings.identity_platform_auth_domain != urlsplit(public_origin).hostname
        or helper not in HELPERS
        or request.scope["raw_path"] != f"/__/auth/{helper}".encode()
    ):
        return 404
    query = request.scope["query_string"]
    if len(query) > MAX_QUERY_BYTES or any(
        byte < _MIN_PRINTABLE or byte >= _DELETE_CHARACTER for byte in query
    ):
        return 400
    if request.method not in {"GET", "POST"} or (request.method == "POST" and helper != "handler"):
        return 405
    return None


async def _relay(request: Request, helper: str, settings: Settings) -> Response:
    if status := _request_status(request, helper, settings):
        return _error(status)
    public_origin = settings.public_base_url.rstrip("/")
    query = request.scope["query_string"]
    # The upstream cannot be selected by an authDomain, query, Host or forwarding header.
    hosting_origin = f"https://{settings.identity_platform_project_id}.firebaseapp.com"
    target = httpx.URL(hosting_origin + f"/__/auth/{helper}").copy_with(query=query)
    headers = {key: value for key, value in request.headers.items() if key in _REQUEST_HEADERS}
    headers["accept-encoding"] = "identity"
    try:
        async with asyncio.timeout(settings.request_body_timeout_seconds):
            body = await _bounded_body(request)
    except (TimeoutError, ClientDisconnect) as error:
        return _error(408 if isinstance(error, TimeoutError) else 400)
    except HelperRejectedError:
        return _error(413)
    if request.method == "GET" and body:
        return _error(400)
    try:
        async with asyncio.timeout(UPSTREAM_SECONDS):
            # No ambient proxies, cookies, authorization, redirect following or shared client jar.
            async with httpx.AsyncClient(
                trust_env=False, follow_redirects=False, timeout=UPSTREAM_SECONDS
            ) as client:
                async with client.stream(
                    request.method, target, headers=headers, content=body
                ) as upstream:
                    downstream = dict(_SECURITY_HEADERS)
                    for name, value in upstream.headers.items():
                        if name in _RESPONSE_HEADERS:
                            downstream[name] = value
                    if location := upstream.headers.get("location"):
                        downstream["location"] = _location(location, hosting_origin, public_origin)
                    content = bytearray()
                    async for chunk in upstream.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > MAX_RESPONSE_BYTES:
                            raise HelperRejectedError
                    return Response(
                        bytes(content), status_code=upstream.status_code, headers=downstream
                    )
    except (httpx.HTTPError, TimeoutError, HelperRejectedError, ValueError):
        # Exceptions can contain OAuth query/body values. Do not log or return them.
        return _error(502)


def install_firebase_auth_helper(app: FastAPI, settings: Settings) -> None:
    # Catch the whole suffix so FastAPI cannot normalize trailing slashes with a query-bearing
    # redirect before the raw-path gate runs. Only the seven exact leaf names pass that gate.
    @app.api_route("/__/auth/{helper:path}", methods=["GET", "POST"], include_in_schema=False)
    async def firebase_auth_helper(request: Request, helper: str) -> Response:
        return await _relay(request, helper, settings)
