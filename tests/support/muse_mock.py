"""Loopback Muse simulator; exercises the real connector without provider requests.

Run with ``python -m tests.support.muse_mock`` against the isolated 14301/14302 preview.
The test proxy supplies a fresh demo account to the unchanged frontend on port 14303.
This module and its controls are excluded from the production wheel and image.
"""

from __future__ import annotations

import asyncio
import re
import secrets
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, ConfigDict

API_ORIGIN = "http://127.0.0.1:14302"
WEB_ORIGIN = "http://127.0.0.1:14301"
MOCK_ORIGIN = "http://127.0.0.1:14303"
PREVIEW_PATH = (
    "/?view=events&when=all&city=sanfrancisco"
    "&source=muse-queue-preview-luma&source=muse-queue-preview-meetup"
    "&source=muse-queue-preview-partiful"
)
# Match both public facts, then freeze their canonical UUIDs before creating an account.
FIXTURES = {
    "LOCAL TEST - Queue with Muse Luma signup": "https://lu.ma/ec-muse-queue-test-not-a-real-event",
    "LOCAL TEST - Queue with Muse Meetup signup": (
        "https://www.meetup.com/ec-muse-local-test/events/000000001/"
    ),
}
# The original preview remains usable without reseeding it. When present, this exact
# additional fixture is checked and frozen by UUID before the new mock account exists.
OPTIONAL_FIXTURES = {
    "LOCAL TEST - Queue with Muse Partiful signup": (
        "https://partiful.com/e/ec-muse-queue-test-not-a-real-event"
    ),
}
ALLOWED_FIXTURES = FIXTURES | OPTIONAL_FIXTURES
RESULTS = {"registered", "needs_input", "uncertain", "failed", "waitlisted", "awaiting_approval"}


class MockError(ValueError):
    """Bounded local failure; never includes upstream bodies or credential headers."""


def validate_fixture(event: dict, *, catalog: bool = False) -> str:
    event_id = str(UUID(event["canonical_event_id"]))
    url = ALLOWED_FIXTURES.get(event.get("title"))
    actual = event.get("registration_urls") if catalog else [event.get("registration_url")]
    if url is None or actual != [url] or event.get("price_status") != "free":
        raise MockError("Only the reviewed synthetic fixtures are supported")
    return event_id


class MuseMock:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.email = f"muse-mock-{uuid4().hex}@example.test"
        self.nonce = secrets.token_urlsafe(32)
        self.owner: dict[str, str] = {}
        self._bearer: dict[str, str] = {}
        self.events: dict[str, dict] = {}
        self.lock = asyncio.Lock()
        self.calls: list[dict] = []

    async def api(self, method: str, path: str, *, connector: bool = False, **kwargs):
        headers = self._bearer if connector else self.owner
        # Explicit requests ignore the client's cookie jar (ports share host cookies).
        result = await self.client.send(
            httpx.Request(method, API_ORIGIN + path, headers=headers, **kwargs)
        )
        if connector:
            self.calls.append(
                {
                    "method": method,
                    "path": path.split("?", maxsplit=1)[0],
                    "status": result.status_code,
                }
            )
            del self.calls[:-20]
        if not result.is_success:
            raise MockError(
                f"Local API returned {result.status_code} for {method} {path.split('?', maxsplit=1)[0]}"
            )
        return result.json() if result.content else None

    async def start(self) -> None:
        config = await self.api("GET", "/v1/ui-config")
        if config.get("local_demo") is not True or config.get("muse_enabled") is not True:
            raise MockError("Requires an isolated local_demo preview with Muse enabled")
        page = await self.api("GET", "/v1/catalog/events?when=all&limit=100")
        for event in page["items"]:
            if event.get("title") in ALLOWED_FIXTURES:
                event_id = validate_fixture(event, catalog=True)
                if any(e["title"] == event["title"] for e in self.events.values()):
                    raise MockError("Synthetic fixture titles must be unique")
                now = datetime.now(UTC)
                observed = [
                    datetime.fromisoformat(s["last_seen_at"])
                    for s in event["sources"]
                    if s["registration_url"] == ALLOWED_FIXTURES[event["title"]]
                ]
                if (
                    datetime.fromisoformat(event["start_at"]) <= now
                    or event.get("event_status") != "scheduled"
                    or not any(
                        now - timedelta(hours=48) <= seen <= now + timedelta(minutes=5)
                        for seen in observed
                    )
                ):
                    raise MockError(
                        "Synthetic fixtures are stale; prepare a fresh isolated preview"
                    )
                self.events[event_id] = event
        if not set(FIXTURES).issubset(event["title"] for event in self.events.values()):
            raise MockError("The isolated catalog must contain both synthetic queue fixtures")
        account = await self.api("POST", "/v1/onboard", json={"notify_email": self.email})
        self.owner = {"X-EC-Tenant-ID": str(UUID(account["tenant_id"]))}
        issued = await self.api("POST", "/v1/me/muse/connection")
        self._bearer = {"Authorization": "Bearer " + issued["token"]}

    async def items(self) -> list[dict]:
        items, cursor, visited = [], None, set()
        while True:
            suffix = "?limit=1" + (f"&cursor={cursor}" if cursor else "")
            batches = await self.api("GET", "/v1/muse/connector/batches" + suffix, connector=True)
            if not batches:
                return items
            for summary in batches:
                batch_id = str(UUID(summary["batch_id"]))
                if batch_id in visited:
                    raise MockError("Connector repeated a batch cursor")
                visited.add(batch_id)
                batch = await self.api(
                    "GET", f"/v1/muse/connector/batches/{batch_id}", connector=True
                )
                for item in batch["items"]:
                    event_id = validate_fixture(item["event"])
                    if event_id not in self.events:
                        raise MockError(
                            "Connector returned an event outside the startup UUID allowlist"
                        )
                    items.append(item | {"batch_id": batch_id})
                cursor = batch_id

    async def advance(self, event_id: UUID, action: str) -> None:
        if action not in RESULTS | {"claim"} or str(event_id) not in self.events:
            raise MockError("Unsupported synthetic event or action")
        async with self.lock:
            item = next(
                (
                    i
                    for i in await self.items()
                    if i["event"]["canonical_event_id"] == str(event_id)
                ),
                None,
            )
            if item is None:
                raise MockError("Queue this event in the preview first")
            if item["status"] in {"registered", "failed"}:
                raise MockError(
                    "Completed results cannot be replaced; restart for a fresh test account"
                )
            path = f"/v1/muse/connector/batches/{item['batch_id']}/items/{event_id}"
            # Recover the durable attempt even after needs_input or uncertain; never submit again.
            attempt = item["attempt_id"]
            if attempt is None:
                attempt = str(uuid4())
                await self.api(
                    "POST", path + "/claim", connector=True, json={"attempt_id": attempt}
                )
            if action != "claim":
                outcome = {"status": action, "note": "Synthetic local test. No provider contacted."}
                if action == "registered":
                    outcome["confirmation_reference"] = "MOCK-" + attempt
                await self.api(
                    "PUT",
                    path + "/outcome",
                    connector=True,
                    json={"attempt_id": attempt, "outcome": outcome},
                )


class Advance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: UUID
    action: str


async def proxy_preview(mock: MuseMock, path: str, request: Request) -> Response:
    path = "/" + path
    if path.startswith("/v1/"):
        readable = path in {
            "/v1/ui-config",
            "/v1/me",
            "/v1/me/muse/connection",
            "/v1/me/muse/registrations",
            "/v1/me/muse/registrations/by-event",
            "/v1/me/muse/registrations/seen",
            "/v1/me/muse/batches",
        } or re.fullmatch(
            r"/v1/catalog/(events|entities|sources|name-suggestions|name-index|entity-resolution)(/[a-zA-Z0-9_-]+(/graph)?)?",
            path,
        )
        writable = path in {"/v1/me/muse/registrations", "/v1/me/muse/registrations/seen"}
        if not readable or (
            request.method not in {"GET", "HEAD"} and (request.method != "POST" or not writable)
        ):
            raise HTTPException(403, "Only catalog reads and this test account's queue are exposed")
        body = await request.body()
        if request.method == "POST":
            try:
                selection = await request.json()
            except ValueError as error:
                raise HTTPException(422, "Invalid JSON selection") from error
            if not isinstance(selection, dict):
                raise HTTPException(422, "A JSON selection is required")
            if path == "/v1/me/muse/registrations" and selection.get("event_id") not in mock.events:
                raise HTTPException(422, "Only the reviewed synthetic events can be queued")
            if path.endswith("/seen") and (
                not isinstance(selection.get("items"), list)
                or any(
                    not isinstance(item, dict) or item.get("event_id") not in mock.events
                    for item in selection["items"]
                )
            ):
                raise HTTPException(422, "Only synthetic updates can be acknowledged")
        url, headers = API_ORIGIN + path, mock.owner | {"Content-Type": "application/json"}
    else:
        if request.method not in {"GET", "HEAD"} or path.startswith(
            ("/auth", "/api/", "/__muse_mock")
        ):
            raise HTTPException(403, "Unsupported local preview route")
        url, headers, body = WEB_ORIGIN + path, {}, b""
    if request.url.query:
        url += "?" + request.url.query
    result = await mock.client.send(
        httpx.Request(request.method, url, headers=headers, content=body)
    )
    # httpx decompresses upstream bodies; never forward their original byte lengths/encoding.
    return Response(
        result.content,
        status_code=result.status_code,
        headers={"Content-Type": result.headers.get("content-type", "application/octet-stream")},
    )


def create_app(mock: MuseMock, *, bootstrap: bool = True) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        try:
            if bootstrap:
                await mock.start()
            yield
        finally:
            # Revoke only this run's fresh key, leaving demo records for visual inspection.
            if bootstrap and mock.owner:
                with suppress(MockError, httpx.HTTPError):
                    await mock.api("DELETE", "/v1/me/muse/connection")
            if bootstrap:
                await mock.client.aclose()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def loopback_only(request: Request, call_next):
        if request.headers.get("host") != "127.0.0.1:14303":
            return JSONResponse({"detail": "Loopback Host required"}, status_code=403)
        if request.method not in {"GET", "HEAD"} and request.headers.get("origin") != MOCK_ORIGIN:
            return JSONResponse({"detail": "Same-origin write required"}, status_code=403)
        if request.method not in {"GET", "HEAD"}:
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                return JSONResponse({"detail": "JSON write required"}, status_code=415)
            if len(await request.body()) > 16384:
                return JSONResponse({"detail": "Mock request too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(MockError)
    async def mock_error(_request, error):
        return JSONResponse({"detail": str(error)}, status_code=409)

    @app.exception_handler(httpx.HTTPError)
    async def upstream_error(_request, _error):
        return JSONResponse({"detail": "Local preview unavailable"}, status_code=502)

    @app.get("/__muse_mock", response_class=HTMLResponse)
    async def controls():
        html = Path(__file__).with_suffix(".html").read_text()
        return HTMLResponse(
            html.replace("CONTROL_NONCE", mock.nonce).replace("PREVIEW_PATH", PREVIEW_PATH)
        )

    @app.get("/__muse_mock/state")
    async def state():
        items = await mock.items()
        return {"account": mock.email, "items": items, "calls": mock.calls}

    @app.post("/__muse_mock/advance", status_code=204)
    async def advance(body: Advance, request: Request):
        if not secrets.compare_digest(request.headers.get("x-mock-control", ""), mock.nonce):
            raise HTTPException(403, "Mock control nonce required")
        await mock.advance(body.event_id, body.action)
        return Response(status_code=204)

    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE"])
    async def preview(path: str, request: Request):
        return await proxy_preview(mock, path, request)

    return app


def main() -> None:
    client = httpx.AsyncClient(timeout=20, trust_env=False, follow_redirects=False)
    print(f"Local mock preview: {MOCK_ORIGIN}{PREVIEW_PATH}", flush=True)
    print(f"Muse controls: {MOCK_ORIGIN}/__muse_mock", flush=True)
    uvicorn.run(
        create_app(MuseMock(client)),
        host="127.0.0.1",
        port=14303,
        access_log=False,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
