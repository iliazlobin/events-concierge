"""Discovery reads share publication rules but never gain registration authority."""

import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from tests.unit.test_muse import published_event
from tests.unit.test_muse_api import ORIGIN, muse_app

from events_concierge.api.muse_discovery import DiscoveryQuery, MuseDiscovery
from events_concierge.api.muse_mcp import MCP_PATH


def fixture(monkeypatch, rows=None):
    app, repo, owner = muse_app(monkeypatch)
    catalog = app.state.container.catalog
    rows = rows if rows is not None else [published_event()]
    catalog.browse_current.return_value = (rows, [])
    catalog.get_browse_event.return_value = rows[0] if rows else None
    return app, repo, owner, catalog


async def connected(app, owner):
    token, _ = await app.state.container.muse.connect(owner)
    return {"Authorization": "Bearer " + token}


async def test_search_and_read_return_evidence_without_registration_writes(monkeypatch):
    app, repo, owner, catalog = fixture(monkeypatch)
    headers = await connected(app, owner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        result = await client.get(
            "/v1/muse/connector/events?q=free&city=San%20Francisco&price=free", headers=headers
        )
        assert result.status_code == 200, result.text
        event = result.json()["items"][0]
        assert event["muse_signup_eligible"] is True
        assert event["sources"][0]["last_seen_at"]
        assert event["sources"][0]["registration_url"] == "https://lu.ma/muse-test"
        assert event["catalog_path"].endswith(event["event_id"])
        assert "does not queue" in result.json()["notice"]
        assert result.headers["cache-control"] == "no-store, max-age=0"
        detail = await client.get("/v1/muse/connector/events/" + event["event_id"], headers=headers)
        assert detail.status_code == 200
        assert detail.json()["event_id"] == event["event_id"]
        repo.create.assert_not_awaited()
        repo.queue_registration.assert_not_awaited()
        repo.claim.assert_not_awaited()
        assert catalog.browse_current.await_args.kwargs["include_providers"] is False


@pytest.mark.parametrize(
    "query",
    [
        "limit=21",
        "limit=0",
        "source_keys=../admin",
        "topics=invented",
        "starts_after=2030-01-01T00:00:00Z",
        "starts_after=2030-01-01T00:00:00&starts_before=2030-01-02T00:00:00",
        "starts_after=2030-01-02T00:00:00Z&starts_before=2030-01-01T00:00:00Z",
        "starts_after=2030-01-01T00:00:00Z&starts_before=2031-01-01T00:00:00Z",
        "q=%00",
    ],
)
async def test_invalid_filters_never_query_catalog(monkeypatch, query):
    app, _, owner, catalog = fixture(monkeypatch)
    headers = await connected(app, owner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        assert (
            await client.get("/v1/muse/connector/events?" + query, headers=headers)
        ).status_code == 422
    catalog.browse_current.assert_not_awaited()


async def test_cursor_bound_to_filters_and_empty_pages(monkeypatch):
    row1, row2 = published_event(), published_event()
    app, _, owner, catalog = fixture(monkeypatch, [row1, row2])
    headers = await connected(app, owner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        first = await client.get("/v1/muse/connector/events?limit=1&q=test", headers=headers)
        cursor = first.json()["next_cursor"]
        assert cursor
        catalog.browse_current.return_value = ([row2], [])
        second = await client.get(
            "/v1/muse/connector/events",
            params={"limit": 1, "q": "test", "cursor": cursor},
            headers=headers,
        )
        assert second.status_code == 200 and second.json()["next_cursor"] is None
        assert (
            catalog.browse_current.await_args.kwargs["after"].canonical_event_id
            == row1.canonical_event.canonical_event_id
        )
        catalog.browse_current.reset_mock()
        assert (
            await client.get(
                "/v1/muse/connector/events",
                params={"q": "different", "cursor": cursor},
                headers=headers,
            )
        ).status_code == 422
        catalog.browse_current.assert_not_awaited()
        catalog.browse_current.return_value = ([], [])
        empty = await client.get("/v1/muse/connector/events", headers=headers)
        assert empty.json()["items"] == [] and empty.json()["next_cursor"] is None
        catalog.get_browse_event.return_value = None
        assert (
            await client.get("/v1/muse/connector/events/" + str(uuid4()), headers=headers)
        ).status_code == 404


async def test_withheld_location_and_stale_unsupported_events_remain_honest(monkeypatch):
    row = published_event(url="https://example.test/event", age=timedelta(days=8))
    row = replace(row, sources=(replace(row.sources[0], location_withheld=True),))
    app, _, owner, _ = fixture(monkeypatch, [row])
    headers = await connected(app, owner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        event = (await client.get("/v1/muse/connector/events", headers=headers)).json()["items"][0]
        assert event["venue_name"] is None
        assert event["source_freshness"] == "stale"
        assert event["muse_signup_eligible"] is False
        assert "recently collected" in event["muse_signup_reason"]
        assert "tenant_id" not in event and "registration_account" not in event


@pytest.mark.parametrize("path", ["/v1/muse/connector/events", MCP_PATH])
async def test_discovery_never_accepts_cookie_or_revoked_bearer(monkeypatch, path):
    app, repo, owner, catalog = fixture(monkeypatch)
    headers = await connected(app, owner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        for invalid in (
            {},
            {"Cookie": "fixture-session=owner"},
            {"Authorization": "Bearer guessed"},
        ):
            assert (await client.get(path, headers=invalid)).status_code == 401
        await repo.revoke(owner)
        assert (await client.get(path, headers=headers)).status_code == 401
    catalog.browse_current.assert_not_awaited()


async def test_mcp_initializes_lists_and_calls_read_only_tools(monkeypatch):
    app, repo, owner, catalog = fixture(monkeypatch)
    headers = {**await connected(app, owner), "Accept": "application/json, text/event-stream"}
    async with (
        app.state.muse_mcp.session_manager.run(),
        AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client,
    ):

        async def rpc(method, params=None, rpc_id=1, extra=None):
            return await asyncio.wait_for(
                client.post(
                    MCP_PATH,
                    headers={**headers, **(extra or {})},
                    json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}},
                ),
                timeout=5,
            )

        init = await rpc(
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        )
        assert init.status_code == 200, init.text
        assert init.json()["result"]["capabilities"]["tools"] == {"listChanged": False}
        listed = await rpc("tools/list")
        tools = listed.json()["result"]["tools"]
        assert {tool["name"] for tool in tools} == {"search_events", "get_event"}
        assert all(tool["annotations"]["readOnlyHint"] for tool in tools)
        result = await rpc(
            "tools/call", {"name": "search_events", "arguments": {"query": {"price": "free"}}}
        )
        assert result.status_code == 200, result.text
        content = result.json()["result"]
        assert not content.get("isError", False), content
        event = content["structuredContent"]["items"][0]
        read = await rpc(
            "tools/call", {"name": "get_event", "arguments": {"event_id": event["event_id"]}}
        )
        assert read.json()["result"]["structuredContent"]["event_id"] == event["event_id"]
        assert (await rpc("tools/call", {"name": "queue_event", "arguments": {}})).json()["result"][
            "isError"
        ] is True
        assert (
            await rpc(
                "tools/call", {"name": "search_events", "arguments": {"query": {"limit": 1000}}}
            )
        ).json()["result"]["isError"] is True
        assert (await rpc("tools/list", extra={"Origin": "https://evil.test"})).status_code == 403
        assert (await rpc("tools/list", extra={"Host": "evil.test"})).status_code == 421
        assert (
            await client.get(MCP_PATH, headers={**headers, "Accept": "text/event-stream"})
        ).status_code == 405
        assert (
            await client.post(
                MCP_PATH,
                headers=headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
        ).status_code == 202
        repo.create.assert_not_awaited()
        repo.claim.assert_not_awaited()
        assert catalog.browse_current.await_count == 1


async def test_discovery_saturation_fails_without_unbounded_waiters(monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()
    active = 0

    async def blocking(_query):
        nonlocal active
        active += 1
        if active == 4:
            entered.set()
        await release.wait()
        return [], None

    discovery = MuseDiscovery(blocking, AsyncMock(), lambda row: {})
    pending = [asyncio.create_task(discovery.search(DiscoveryQuery())) for _ in range(4)]
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)

        with pytest.raises(HTTPException) as error:
            await discovery.search(DiscoveryQuery())
        assert error.value.status_code == 503
        assert active == 4
    finally:
        release.set()
        await asyncio.gather(*pending)
    assert (await discovery.search(DiscoveryQuery())).items == []


async def test_search_preserves_explicit_offset_window_and_price(monkeypatch):
    app, _, owner, catalog = fixture(monkeypatch)
    headers = await connected(app, owner)
    async with AsyncClient(transport=ASGITransport(app=app), base_url=ORIGIN) as client:
        response = await client.get(
            "/v1/muse/connector/events",
            params={
                "starts_after": "2030-06-10T00:00:00-07:00",
                "starts_before": "2030-06-17T00:00:00-07:00",
                "price": "paid",
                "source_keys": "luma-sf",
            },
            headers=headers,
        )
        assert response.status_code == 200
        applied = catalog.browse_current.await_args.kwargs
        assert applied["starts_after"].utcoffset() == timedelta(hours=-7)
        assert applied["starts_before"] - applied["starts_after"] == timedelta(days=7)
        assert applied["price"] == "paid"
        assert applied["source_keys"] == ("luma-sf",)
        assert set(response.json()["items"][0]) >= {
            "price_min_cents",
            "price_max_cents",
            "price_currency",
        }


async def test_official_mcp_client_round_trip(monkeypatch):
    app, _, owner, _ = fixture(monkeypatch)
    headers = await connected(app, owner)
    async with (
        app.state.muse_mcp.session_manager.run(),
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), headers=headers) as http,
        streamable_http_client(ORIGIN + MCP_PATH, http_client=http) as (read, write),
        ClientSession(read, write, read_timeout_seconds=5) as client,
    ):
        await client.initialize()
        tools = await client.list_tools()
        assert {tool.name for tool in tools.tools} == {"search_events", "get_event"}
        result = await client.call_tool("search_events", {"query": {"city": "San Francisco"}})
        assert not result.is_error
        event = result.structured_content["items"][0]
        detail = await client.call_tool("get_event", {"event_id": event["event_id"]})
        assert not detail.is_error
        assert detail.structured_content["event_id"] == event["event_id"]
