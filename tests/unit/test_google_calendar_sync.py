"""Fixture-only Google Calendar incremental sync, push validation, and renewal contracts (FR-9.4)."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from events_concierge.adapters.google_calendar.calendar import GoogleCalendarRetryableError
from events_concierge.adapters.google_calendar.sync import GoogleCalendarSyncAdapter
from events_concierge.adapters.mock.calendar_sync import (
    MockGoogleCalendarSyncSink,
    MockGoogleCalendarSyncState,
)
from events_concierge.application.google_calendar_sync import (
    GoogleCalendarChannelRenewalWorker,
    GoogleCalendarIncrementalSyncService,
    GoogleCalendarSyncMode,
    GoogleCalendarWebhookOutcome,
    GoogleCalendarWebhookService,
)
from events_concierge.ports.google_calendar import (
    GoogleCalendarAccess,
    GoogleCalendarBinding,
    GoogleCalendarChannelState,
    GoogleCalendarSyncChange,
    GoogleCalendarWatch,
    GoogleCalendarWatchRequest,
)

_FIXTURES = Path(__file__).parents[1] / "fixtures" / "calendar"
_WRITE_CALENDAR_ID = "events-concierge@group.calendar.google.com"


class FixtureAccess:
    async def get_access(self, tenant_id: UUID) -> GoogleCalendarAccess:
        del tenant_id
        return GoogleCalendarAccess("fixture-access-token")


class FixtureBindings:
    async def get_binding(self, tenant_id: UUID) -> GoogleCalendarBinding | None:
        del tenant_id
        return GoogleCalendarBinding(_WRITE_CALENDAR_ID, (_WRITE_CALENDAR_ID,))

    async def upsert_binding(self, tenant_id: UUID, binding: GoogleCalendarBinding) -> None:
        del tenant_id, binding


class CrashOnceWhileClearingSyncToken(MockGoogleCalendarSyncState):
    """Persist a pre-crash cursor while simulating a process failure at the reset boundary."""

    def __init__(self) -> None:
        super().__init__()
        self._crash_on_next_clear = True

    async def clear_sync_token(self, tenant_id: UUID, calendar_id: str) -> None:
        if self._crash_on_next_clear:
            self._crash_on_next_clear = False
            raise RuntimeError("injected crash while clearing Google Calendar sync token")
        await super().clear_sync_token(tenant_id, calendar_id)


def _fixture(name: str) -> object:
    return json.loads((_FIXTURES / name).read_text())


def _service(
    client: httpx.AsyncClient,
    states: MockGoogleCalendarSyncState,
    sink: MockGoogleCalendarSyncSink,
) -> GoogleCalendarIncrementalSyncService:
    provider = GoogleCalendarSyncAdapter(FixtureAccess(), client=client)
    return GoogleCalendarIncrementalSyncService(FixtureBindings(), states, provider, sink)


async def test_injected_sync_client_gets_an_explicit_finite_request_timeout() -> None:
    seen_timeout: object = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_timeout
        seen_timeout = request.extensions.get("timeout")
        return httpx.Response(200, json={"items": [], "nextSyncToken": "next"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), timeout=None
    ) as client:
        provider = GoogleCalendarSyncAdapter(FixtureAccess(), client=client, timeout_s=2.5)
        page = await provider.list_events(
            uuid4(),
            _WRITE_CALENDAR_ID,
            sync_token=None,
            page_token=None,
        )

    assert page.next_sync_token == "next"
    assert seen_timeout == {"connect": 2.5, "read": 2.5, "write": 2.5, "pool": 2.5}


@pytest.mark.parametrize("timeout_s", (0.0, 0.09, 60.01, math.inf, math.nan))
def test_google_sync_timeout_must_be_bounded_and_finite(timeout_s: float) -> None:
    with pytest.raises(ValueError, match="finite and between"):
        GoogleCalendarSyncAdapter(FixtureAccess(), timeout_s=timeout_s)


async def test_incremental_sync_preserves_sync_token_through_every_page_then_commits_once() -> None:
    """A delta paging fixture applies both pages before replacing the previous durable token (AC-67)."""
    tenant_id = uuid4()
    states = MockGoogleCalendarSyncState()
    sink = MockGoogleCalendarSyncSink()
    await states.store_sync_token(tenant_id, _WRITE_CALENDAR_ID, "sync-token-before-delta")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        assert request.headers["authorization"] == "Bearer fixture-access-token"
        if request.url.params.get("pageToken") is None:
            assert dict(request.url.params) == {
                "singleEvents": "true",
                "showDeleted": "true",
                "maxResults": "2500",
                "syncToken": "sync-token-before-delta",
            }
            return httpx.Response(200, json=_fixture("sync-delta-page-one.json"))
        assert dict(request.url.params) == {
            "singleEvents": "true",
            "showDeleted": "true",
            "maxResults": "2500",
            "syncToken": "sync-token-before-delta",
            "pageToken": "delta-page-two",
        }
        return httpx.Response(200, json=_fixture("sync-delta-page-two.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _service(client, states, sink).sync_tenant(tenant_id)

    assert result.mode is GoogleCalendarSyncMode.INCREMENTAL
    assert result.pages == 2
    assert result.changes_applied == 2
    assert result.sync_token == "sync-token-after-delta"
    state = await states.get_state(tenant_id, _WRITE_CALENDAR_ID)
    assert state is not None
    assert state.sync_token == "sync-token-after-delta"
    assert len(sink.applied_batches) == 1
    changes = sink.applied_batches[0][2]
    assert [change.provider_event_id for change in changes] == [
        "delta-event-one",
        "delta-event-two",
    ]
    assert changes[0].canonical_event_id == UUID("11111111-1111-1111-1111-111111111111")
    assert changes[1].status == "cancelled"
    assert [request.method for request in requests] == ["GET", "GET"]


async def test_sync_rate_limit_is_retryable_without_reconsent() -> None:
    """Sync shares Calendar mutation's explicit rate/quota classification (FR-9.7)."""
    tenant_id = uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(403, json=_fixture("forbidden-rate-limit.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarSyncAdapter(FixtureAccess(), client=client)
        with pytest.raises(GoogleCalendarRetryableError):
            await provider.list_events(
                tenant_id, _WRITE_CALENDAR_ID, sync_token=None, page_token=None
            )


async def test_expired_sync_token_wipes_projection_then_runs_a_fresh_full_sync() -> None:
    """HTTP 410 never leaves stale locally projected events mixed with a replacement full sync (AC-67)."""
    tenant_id = uuid4()
    states = MockGoogleCalendarSyncState()
    sink = MockGoogleCalendarSyncSink()
    await states.store_sync_token(tenant_id, _WRITE_CALENDAR_ID, "expired-sync-token")
    await sink.apply_changes(
        tenant_id,
        _WRITE_CALENDAR_ID,
        (
            GoogleCalendarSyncChange(
                provider_event_id="stale-event",
                status="confirmed",
                version=None,
                canonical_event_id=None,
                summary="stale",
                start_at=None,
                end_at=None,
            ),
        ),
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "GET"
        if request.url.params.get("syncToken") == "expired-sync-token":
            return httpx.Response(410, json={"error": {"status": "GONE"}})
        assert "syncToken" not in request.url.params
        return httpx.Response(200, json=_fixture("sync-full-after-410.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await _service(client, states, sink).sync_tenant(tenant_id)

    assert result.mode is GoogleCalendarSyncMode.FULL_AFTER_TOKEN_RESET
    assert result.sync_token == "sync-token-after-full"
    assert states.cleared == [(tenant_id, _WRITE_CALENDAR_ID)]
    assert sink.resets == [(tenant_id, _WRITE_CALENDAR_ID)]
    assert [event.provider_event_id for event in sink.events(tenant_id, _WRITE_CALENDAR_ID)] == [
        "full-event-one"
    ]
    state = await states.get_state(tenant_id, _WRITE_CALENDAR_ID)
    assert state is not None
    assert state.sync_token == "sync-token-after-full"
    assert len(requests) == 2


async def test_retry_after_crash_at_410_cursor_clear_cannot_preserve_stale_projection() -> None:
    """A reset-first 410 recovery repeats its idempotent wipe after a crash before cursor clearing (AC-67)."""
    tenant_id = uuid4()
    states = CrashOnceWhileClearingSyncToken()
    sink = MockGoogleCalendarSyncSink()
    await states.store_sync_token(tenant_id, _WRITE_CALENDAR_ID, "expired-sync-token")
    await sink.apply_changes(
        tenant_id,
        _WRITE_CALENDAR_ID,
        (
            GoogleCalendarSyncChange(
                provider_event_id="stale-event",
                status="confirmed",
                version=None,
                canonical_event_id=None,
                summary="stale",
                start_at=None,
                end_at=None,
            ),
        ),
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.params.get("syncToken") == "expired-sync-token":
            return httpx.Response(410, json={"error": {"status": "GONE"}})
        assert "syncToken" not in request.url.params
        return httpx.Response(200, json=_fixture("sync-full-after-410.json"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="injected crash"):
            await _service(client, states, sink).sync_tenant(tenant_id)

        # Simulate a fresh process retrying from the durable state left by the interrupted run.
        after_crash = await states.get_state(tenant_id, _WRITE_CALENDAR_ID)
        assert after_crash is not None
        assert after_crash.sync_token == "expired-sync-token"
        assert sink.events(tenant_id, _WRITE_CALENDAR_ID) == []
        retry = await _service(client, states, sink).sync_tenant(tenant_id)

    assert retry.mode is GoogleCalendarSyncMode.FULL_AFTER_TOKEN_RESET
    assert [event.provider_event_id for event in sink.events(tenant_id, _WRITE_CALENDAR_ID)] == [
        "full-event-one"
    ]
    assert sink.resets == [
        (tenant_id, _WRITE_CALENDAR_ID),
        (tenant_id, _WRITE_CALENDAR_ID),
    ]
    state = await states.get_state(tenant_id, _WRITE_CALENDAR_ID)
    assert state is not None
    assert state.sync_token == "sync-token-after-full"
    assert [request.url.params.get("syncToken") for request in requests] == [
        "expired-sync-token",
        "expired-sync-token",
        None,
    ]


async def test_webhook_rejects_forged_headers_before_sync_and_accepts_the_active_channel() -> None:
    """Only a current channel/resource/token triplet can turn a body-free push into a Google read."""
    tenant_id = uuid4()
    states = MockGoogleCalendarSyncState()
    sink = MockGoogleCalendarSyncSink()
    request = GoogleCalendarWatchRequest(
        channel_id="fixture-channel",
        channel_token="fixture-callback-token",
        callback_url="https://callbacks.example.test/google-calendar",
    )
    watch = GoogleCalendarWatch(
        channel_id="fixture-channel",
        resource_id="fixture-resource",
        expires_at=datetime(2026, 7, 15, 19, 0, tzinfo=UTC),
    )
    await states.store_channel(
        tenant_id,
        _WRITE_CALENDAR_ID,
        GoogleCalendarChannelState.from_watch(request, watch),
    )
    requests: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        requests.append(http_request)
        return httpx.Response(200, json={"items": [], "nextSyncToken": "sync-after-webhook"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sync = _service(client, states, sink)
        webhook = GoogleCalendarWebhookService(
            FixtureBindings(),
            states,
            sync,
            now=lambda: datetime(2026, 7, 15, 18, 0, tzinfo=UTC),
        )
        forged = await webhook.handle(
            tenant_id,
            {
                "X-Goog-Channel-ID": "fixture-channel",
                "X-Goog-Resource-ID": "fixture-resource",
                "X-Goog-Channel-Token": "wrong-token",
                "X-Goog-Resource-State": "exists",
                "X-Goog-Message-Number": "1",
            },
        )
        accepted = await webhook.handle(
            tenant_id,
            {
                "X-Goog-Channel-ID": "fixture-channel",
                "X-Goog-Resource-ID": "fixture-resource",
                "X-Goog-Channel-Token": "fixture-callback-token",
                "X-Goog-Resource-State": "exists",
                "X-Goog-Message-Number": "2",
            },
        )

    assert forged.outcome is GoogleCalendarWebhookOutcome.REJECTED
    assert accepted.outcome is GoogleCalendarWebhookOutcome.SYNCED
    assert accepted.sync is not None
    assert accepted.sync.sync_token == "sync-after-webhook"
    assert [request.method for request in requests] == ["GET"]


async def test_renewal_worker_uses_returned_expiration_not_a_fixed_channel_ttl() -> None:
    """A due channel is replaced and the next renewal deadline derives from the watch response (FR-9.4)."""
    tenant_id = uuid4()
    states = MockGoogleCalendarSyncState()
    old_request = GoogleCalendarWatchRequest(
        channel_id="fixture-channel-old",
        channel_token="fixture-old-token",
        callback_url="https://callbacks.example.test/google-calendar",
    )
    old_watch = GoogleCalendarWatch(
        channel_id="fixture-channel-old",
        resource_id="fixture-resource-old",
        expires_at=datetime(2026, 7, 15, 17, 14, tzinfo=UTC),
    )
    await states.store_channel(
        tenant_id,
        _WRITE_CALENDAR_ID,
        GoogleCalendarChannelState.from_watch(old_request, old_watch),
    )
    requests: list[httpx.Request] = []

    def handler(http_request: httpx.Request) -> httpx.Response:
        requests.append(http_request)
        assert http_request.method == "POST"
        assert http_request.url.path.endswith("/events/watch")
        assert json.loads(http_request.content) == {
            "id": "fixture-channel-renewed",
            "type": "web_hook",
            "address": "https://callbacks.example.test/google-calendar",
            "token": "fixture-renewed-token",
        }
        return httpx.Response(200, json=_fixture("watch-success.json"))

    replacement_request = GoogleCalendarWatchRequest(
        channel_id="fixture-channel-renewed",
        channel_token="fixture-renewed-token",
        callback_url="https://callbacks.example.test/google-calendar",
    )

    def now() -> datetime:
        return datetime(2026, 7, 15, 17, 0, tzinfo=UTC)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = GoogleCalendarSyncAdapter(FixtureAccess(), client=client)
        worker = GoogleCalendarChannelRenewalWorker(
            FixtureBindings(),
            states,
            provider,
            channel_factory=lambda: replacement_request,
            renewal_lead=timedelta(minutes=15),
            now=now,
        )
        renewed = await worker.renew_if_due(tenant_id)
        not_due = await worker.renew_if_due(tenant_id)

    assert renewed.renewed is True
    assert renewed.renewal_due_at == datetime(2026, 7, 15, 17, 45, tzinfo=UTC)
    assert not_due.renewed is False
    assert not_due.renewal_due_at == renewed.renewal_due_at
    state = await states.get_state(tenant_id, _WRITE_CALENDAR_ID)
    assert state is not None
    assert state.channel is not None
    assert state.channel.channel_id == "fixture-channel-renewed"
    assert state.channel.resource_id == "fixture-resource-renewed"
    assert [request.method for request in requests] == ["POST"]
