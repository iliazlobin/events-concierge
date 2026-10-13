"""Hermetic local-Muse controls cannot operate another account or real event."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from tests.support.muse_mock import (
    API_ORIGIN,
    FIXTURES,
    MOCK_ORIGIN,
    WEB_ORIGIN,
    MockError,
    MuseMock,
    create_app,
)

FIXTURE_TOKEN = "synthetic-local-connector-key"


def catalog_fixtures():
    now = datetime.now(UTC)
    return [
        {
            "canonical_event_id": str(uuid4()),
            "title": title,
            "registration_urls": [url],
            "price_status": "free",
            "event_status": "scheduled",
            "start_at": (now + timedelta(days=7)).isoformat(),
            "sources": [
                {"registration_url": url, "last_seen_at": (now - timedelta(hours=1)).isoformat()}
            ],
        }
        for title, url in FIXTURES.items()
    ]


def signup_item(event, *, status="queued", attempt=None):
    return {
        "event": {
            "canonical_event_id": event["canonical_event_id"],
            "title": event["title"],
            "registration_url": event["registration_urls"][0],
            "price_status": event["price_status"],
        },
        "status": status,
        "attempt_id": attempt,
        "outcome": None,
    }


class FakeBackend:
    """Record every request; no hostname except the two fake preview upstreams exists."""

    def __init__(self):
        self.config = {"local_demo": True, "muse_enabled": True}
        self.events = catalog_fixtures()
        self.tenant_id = str(uuid4())
        self.requests = []
        self.batches = []
        self.response_headers = {}

    def __call__(self, request):
        assert str(request.url).startswith((API_ORIGIN + "/", WEB_ORIGIN + "/"))
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/ui-config":
            data = self.config
        elif path == "/v1/catalog/events":
            data = {"items": self.events}
        elif path == "/v1/onboard":
            data = {"tenant_id": self.tenant_id}
        elif path == "/v1/me/muse/connection" and request.method == "POST":
            data = {"token": FIXTURE_TOKEN, "expires_at": "2030-01-01T00:00:00Z"}
        elif path == "/v1/muse/connector/batches":
            cursor = request.url.params.get("cursor")
            batch_ids = [batch["batch_id"] for batch in self.batches]
            start = batch_ids.index(cursor) + 1 if cursor else 0
            data = [{"batch_id": batch_id} for batch_id in batch_ids[start : start + 1]]
        elif path.startswith("/v1/muse/connector/batches/"):
            data = self.connector_request(request)
        else:
            data = {"ok": True}
        return httpx.Response(200, json=deepcopy(data), headers=self.response_headers)

    def connector_request(self, request):
        parts = request.url.path.split("/")
        batch = next(batch for batch in self.batches if batch["batch_id"] == parts[5])
        if len(parts) == 6:
            assert request.method == "GET"
            return batch
        item = next(
            item for item in batch["items"] if item["event"]["canonical_event_id"] == parts[7]
        )
        body = json.loads(request.content)
        if parts[8] == "claim":
            assert request.method == "POST"
            assert item["attempt_id"] is None
            item.update(attempt_id=body["attempt_id"], status="in_progress")
        else:
            assert parts[8] == "outcome" and request.method == "PUT"
            assert body["attempt_id"] == item["attempt_id"]
            item.update(status=body["outcome"]["status"], outcome=body["outcome"])
        return item

    @property
    def writes(self):
        return [request for request in self.requests if request.method not in {"GET", "HEAD"}]


@pytest.fixture
async def preview():
    backend = FakeBackend()
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as upstream:
        mock = MuseMock(upstream)
        mock.owner = {"X-EC-Tenant-ID": backend.tenant_id}
        mock._bearer = {"Authorization": "Bearer " + FIXTURE_TOKEN}
        mock.events = {event["canonical_event_id"]: event for event in backend.events}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(mock, bootstrap=False)),
            base_url=MOCK_ORIGIN,
        ) as client:
            yield mock, backend, client


@pytest.mark.parametrize(
    "config",
    [
        {},
        {"local_demo": False, "muse_enabled": True},
        {"local_demo": True, "muse_enabled": False},
        {"local_demo": "true", "muse_enabled": True},
        {"local_demo": True, "muse_enabled": 1},
    ],
)
async def test_start_rejects_nonlocal_or_disabled_config_before_any_write(config):
    backend = FakeBackend()
    backend.config = config
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as upstream:
        mock = MuseMock(upstream)
        with pytest.raises(MockError, match="isolated local_demo"):
            await mock.start()
    assert [(r.method, r.url.path) for r in backend.requests] == [("GET", "/v1/ui-config")]
    assert backend.writes == []
    assert mock.owner == {} and mock._bearer == {}


@pytest.mark.parametrize(
    "invalid",
    [
        "missing",
        "duplicate",
        "uuid",
        "url",
        "extra_url",
        "paid",
        "past",
        "cancelled",
        "stale",
        "future_observation",
    ],
)
async def test_start_rejects_bad_fixture_before_account_or_key_creation(invalid):
    backend = FakeBackend()
    event = backend.events[0]
    if invalid == "missing":
        backend.events.pop()
    elif invalid == "duplicate":
        backend.events.append(deepcopy(event))
    elif invalid == "uuid":
        event["canonical_event_id"] = "not-a-uuid"
    elif invalid == "url":
        event["registration_urls"] = ["https://lu.ma/a-real-event"]
    elif invalid == "extra_url":
        event["registration_urls"].append("https://lu.ma/a-real-event")
    elif invalid == "paid":
        event["price_status"] = "paid"
    elif invalid == "past":
        event["start_at"] = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    elif invalid == "cancelled":
        event["event_status"] = "cancelled"
    else:
        offset = timedelta(hours=-49) if invalid == "stale" else timedelta(minutes=10)
        event["sources"][0]["last_seen_at"] = (datetime.now(UTC) + offset).isoformat()
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as upstream:
        mock = MuseMock(upstream)
        with pytest.raises(ValueError):
            await mock.start()
    assert backend.writes == []
    assert mock.owner == {} and mock._bearer == {}


async def test_valid_start_creates_only_a_fresh_demo_owner_and_key():
    backend = FakeBackend()
    backend.response_headers = {"set-cookie": "upstream-session=poisoned; Path=/"}
    async with httpx.AsyncClient(transport=httpx.MockTransport(backend)) as upstream:
        mock = MuseMock(upstream)
        await mock.start()
    assert [(r.method, r.url.path) for r in backend.writes] == [
        ("POST", "/v1/onboard"),
        ("POST", "/v1/me/muse/connection"),
    ]
    assert json.loads(backend.writes[0].content) == {"notify_email": mock.email}
    assert mock.email.startswith("muse-mock-") and mock.email.endswith("@example.test")
    assert backend.writes[1].headers["x-ec-tenant-id"] == backend.tenant_id
    assert mock.owner == {"X-EC-Tenant-ID": backend.tenant_id}
    assert set(mock.events) == {event["canonical_event_id"] for event in backend.events}
    assert mock._bearer == {"Authorization": "Bearer " + FIXTURE_TOKEN}
    assert all("cookie" not in request.headers for request in backend.requests)
    assert all("authorization" not in request.headers for request in backend.requests)


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"host": "evil.example.test", "origin": MOCK_ORIGIN}, 403),
        ({"host": "localhost:14303", "origin": MOCK_ORIGIN}, 403),
        ({}, 403),
        ({"origin": "https://evil.example.test"}, 403),
        ({"origin": MOCK_ORIGIN}, 403),
        ({"origin": MOCK_ORIGIN, "x-mock-control": "guessed"}, 403),
    ],
)
async def test_controls_require_exact_loopback_host_origin_and_nonce(preview, headers, expected):
    mock, backend, client = preview
    response = await client.post(
        "/__muse_mock/advance",
        headers=headers,
        json={"event_id": next(iter(mock.events)), "action": "registered"},
    )
    assert response.status_code == expected
    assert backend.requests == []


async def test_get_rejects_rebound_host_before_upstream_request(preview):
    _, backend, client = preview
    response = await client.get("/v1/me", headers={"host": "attacker.example.test"})
    assert response.status_code == 403
    assert backend.requests == []


@pytest.mark.parametrize(
    ("body", "content_type", "expected"),
    [("{}", "text/plain", 415), ("x" * 16385, "application/json", 413)],
)
async def test_proxy_rejects_nonjson_or_oversized_writes(preview, body, content_type, expected):
    _, backend, client = preview
    response = await client.post(
        "/v1/me/muse/registrations",
        content=body,
        headers={"origin": MOCK_ORIGIN, "content-type": content_type},
    )
    assert response.status_code == expected
    assert backend.requests == []


async def test_proxy_discards_browser_credentials_and_upstream_cookie_jar(preview):
    mock, backend, client = preview
    mock.client.cookies.set("upstream-session", "must-not-travel", domain="127.0.0.1")
    mock.client.headers.update(
        {"Authorization": "Bearer upstream-default", "X-EC-Tenant-ID": "default-tenant"}
    )
    backend.response_headers = {
        "set-cookie": "poisoned-session=upstream; Path=/",
        "authorization": "upstream-secret",
    }
    incoming = {
        "cookie": "browser-session=owner",
        "authorization": "Bearer browser-secret",
        "x-ec-tenant-id": "browser-tenant",
    }
    for path in ["/v1/me", "/docs/muse", "/v1/me/muse/registrations"]:
        response = await client.get(path, headers=incoming)
        assert response.status_code == 200
        assert "set-cookie" not in response.headers and "authorization" not in response.headers
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        upstream = backend.requests[-1]
        assert "cookie" not in upstream.headers and "authorization" not in upstream.headers
        if path.startswith("/v1/"):
            assert upstream.headers["x-ec-tenant-id"] == backend.tenant_id
            assert str(upstream.url).startswith(API_ORIGIN)
        else:
            assert "x-ec-tenant-id" not in upstream.headers
            assert str(upstream.url).startswith(WEB_ORIGIN)
    assert backend.writes == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/me/muse/connection"),
        ("DELETE", "/v1/me/muse/connection"),
        ("POST", "/v1/onboard"),
        ("PUT", "/v1/me/profile"),
        ("DELETE", "/v1/me"),
        ("POST", "/v1/admin/ingestion/commands"),
        ("GET", "/v1/admin/overview"),
        ("POST", "/v1/catalog/events"),
        ("PUT", "/v1/catalog/events/synthetic"),
        ("POST", "/v1/muse/connector/batches/arbitrary/items/arbitrary/claim"),
        ("GET", "/auth/google"),
        ("GET", "/api/admin"),
    ],
)
async def test_proxy_exposes_no_key_account_admin_or_catalog_writes(preview, method, path):
    _, backend, client = preview
    response = await client.request(method, path, headers={"origin": MOCK_ORIGIN}, json={})
    assert response.status_code == 403
    assert backend.requests == []


@pytest.mark.parametrize("body", [{"event_id": str(uuid4())}, {}, [], "not-json"])
async def test_proxy_cannot_queue_arbitrary_events_or_malformed_selections(preview, body):
    _, backend, client = preview
    request = (
        {"content": body, "headers": {"origin": MOCK_ORIGIN, "content-type": "application/json"}}
        if isinstance(body, str)
        else {"json": body, "headers": {"origin": MOCK_ORIGIN}}
    )
    response = await client.post("/v1/me/muse/registrations", **request)
    assert response.status_code == 422
    assert backend.requests == []


@pytest.mark.parametrize("items", [[{"event_id": str(uuid4()), "version": 1}], [None], None])
async def test_proxy_cannot_acknowledge_other_events(preview, items):
    _, backend, client = preview
    response = await client.post(
        "/v1/me/muse/registrations/seen", headers={"origin": MOCK_ORIGIN}, json={"items": items}
    )
    assert response.status_code == 422
    assert backend.requests == []


async def test_proxy_queues_only_a_frozen_fixture_for_its_fresh_owner(preview):
    mock, backend, client = preview
    selection = {"request_id": str(uuid4()), "event_id": next(iter(mock.events))}
    response = await client.post(
        "/v1/me/muse/registrations",
        headers={"origin": MOCK_ORIGIN, "x-ec-tenant-id": "other-owner"},
        json=selection,
    )
    assert response.status_code == 200
    assert len(backend.writes) == 1
    assert json.loads(backend.writes[0].content) == selection
    assert backend.writes[0].headers["x-ec-tenant-id"] == backend.tenant_id


async def test_bearer_reads_every_batch_at_limit_one_without_owner_authority(preview):
    mock, backend, _ = preview
    mock.client.cookies.set("upstream-session", "must-not-travel", domain="127.0.0.1")
    mock.client.headers["X-EC-Tenant-ID"] = "default-owner-must-not-travel"
    backend.batches = [
        {"batch_id": str(uuid4()), "items": [signup_item(event)]} for event in backend.events
    ]
    items = await mock.items()
    assert [item["event"]["canonical_event_id"] for item in items] == list(mock.events)
    lists = [r for r in backend.requests if r.url.path == "/v1/muse/connector/batches"]
    assert [dict(r.url.params) for r in lists] == [
        {"limit": "1"},
        {"limit": "1", "cursor": backend.batches[0]["batch_id"]},
        {"limit": "1", "cursor": backend.batches[1]["batch_id"]},
    ]
    assert backend.writes == []
    for request in backend.requests:
        assert request.headers["authorization"] == "Bearer " + FIXTURE_TOKEN
        assert "x-ec-tenant-id" not in request.headers and "cookie" not in request.headers
    assert FIXTURE_TOKEN not in json.dumps(mock.calls)


async def test_repeated_cursor_fails_instead_of_looping_or_writing(preview):
    mock, backend, _ = preview
    batch_id = str(uuid4())
    backend.batches = [
        {"batch_id": batch_id, "items": [signup_item(event)]} for event in backend.events
    ]
    with pytest.raises(MockError, match="repeated a batch cursor"):
        await mock.items()
    assert len(backend.requests) == 3
    assert backend.writes == []


@pytest.mark.parametrize("first_status", ["needs_input", "uncertain"])
async def test_resuming_a_report_uses_the_same_attempt_and_never_reclaims(preview, first_status):
    mock, backend, client = preview
    event = backend.events[0]
    event_id = event["canonical_event_id"]
    backend.batches = [{"batch_id": str(uuid4()), "items": [signup_item(event)]}]
    headers = {"origin": MOCK_ORIGIN, "x-mock-control": mock.nonce}
    for action in [first_status, "registered"]:
        response = await client.post(
            "/__muse_mock/advance", headers=headers, json={"event_id": event_id, "action": action}
        )
        assert response.status_code == 204
    claims = [r for r in backend.writes if r.url.path.endswith("/claim")]
    outcomes = [json.loads(r.content) for r in backend.writes if r.url.path.endswith("/outcome")]
    assert len(claims) == 1
    attempt = json.loads(claims[0].content)["attempt_id"]
    assert str(UUID(attempt)) == attempt
    assert [result["attempt_id"] for result in outcomes] == [attempt, attempt]
    assert [result["outcome"]["status"] for result in outcomes] == [first_status, "registered"]
    assert outcomes[-1]["outcome"]["confirmation_reference"] == "MOCK-" + attempt


@pytest.mark.parametrize("terminal", ["registered", "failed"])
async def test_completed_tasks_cannot_be_claimed_or_overwritten(preview, terminal):
    mock, backend, client = preview
    event = backend.events[0]
    backend.batches = [
        {
            "batch_id": str(uuid4()),
            "items": [signup_item(event, status=terminal, attempt=str(uuid4()))],
        }
    ]
    for action in ["claim", "registered", "uncertain"]:
        response = await client.post(
            "/__muse_mock/advance",
            headers={"origin": MOCK_ORIGIN, "x-mock-control": mock.nonce},
            json={"event_id": event["canonical_event_id"], "action": action},
        )
        assert response.status_code == 409
    assert backend.writes == []


async def test_connector_rejects_same_fixture_facts_with_unapproved_uuid(preview):
    mock, backend, _ = preview
    event = deepcopy(backend.events[0])
    event["canonical_event_id"] = str(uuid4())
    backend.batches = [{"batch_id": str(uuid4()), "items": [signup_item(event)]}]
    with pytest.raises(MockError, match="startup UUID allowlist"):
        await mock.items()
    assert backend.writes == []


async def test_advance_rejects_unknown_event_or_action_before_connector_reads(preview):
    mock, backend, _ = preview
    for event_id, action in [(uuid4(), "registered"), (UUID(next(iter(mock.events))), "paid")]:
        with pytest.raises(MockError, match="Unsupported synthetic"):
            await mock.advance(event_id, action)
    assert backend.requests == []
