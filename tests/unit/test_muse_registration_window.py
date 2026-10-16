"""Ongoing admission shares queue/claim time guards without changing saved attempts."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from tests.unit.test_muse import published_event, service_for

from events_concierge.application import muse as muse_module
from events_concierge.domain.muse import (
    MuseConflictError,
    MuseNotFoundError,
    SignupBatch,
    SignupEvent,
    SignupItem,
    registration_window_open,
)

NOW = datetime(2035, 10, 15, 18, tzinfo=UTC)


def clock(monkeypatch, current):
    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current.astimezone(tz or UTC)

    monkeypatch.setattr(muse_module, "datetime", FrozenDatetime)


def browse_at(start, end, url="https://partiful.com/e/ongoing"):
    browse = published_event(url)
    return replace(
        browse,
        canonical_event=replace(browse.canonical_event, start_at=start, end_at=end),
        sources=(replace(browse.sources[0], last_seen_at=NOW - timedelta(hours=1)),),
    )


@pytest.mark.parametrize("start,end,expected", [
    (1, None, True), (1, 2, True), (0, 1, True), (-1, 1, True),
    (0, None, False), (-1, None, False), (-2, 0, False), (-2, -1, False),
    (1, 1, False), (2, 1, False), (-1, -2, False), (1, -1, False),
])
def test_registration_window_boundaries(start, end, expected):
    start_at = NOW + timedelta(seconds=start)
    end_at = NOW + timedelta(seconds=end) if end is not None else None
    assert registration_window_open(start_at, end_at, NOW) is expected


@pytest.mark.parametrize("field", ["start", "end", "now"])
def test_unknown_timezones_fail_closed(field):
    values = {"start": NOW - timedelta(hours=1), "end": NOW + timedelta(hours=1), "now": NOW}
    values[field] = values[field].replace(tzinfo=None)
    assert not registration_window_open(values["start"], values["end"], values["now"])


@pytest.mark.parametrize("url", [
    "https://luma.com/ongoing", "https://www.meetup.com/group/events/123/",
    "https://partiful.com/e/ongoing",
])
async def test_ongoing_event_queue_and_claim_keep_the_same_occurrence_snapshot(monkeypatch, url):
    clock(monkeypatch, NOW)
    browse = browse_at(NOW - timedelta(hours=1), NOW + timedelta(hours=1), url)
    service, repository, _ = service_for(browse)
    tenant, request, attempt = uuid4(), uuid4(), uuid4()
    event_id = browse.canonical_event.canonical_event_id
    repository.queue_registration.side_effect = [MuseNotFoundError(), "queued"]
    assert await service.queue(tenant, request, event_id) == "queued"
    selected = repository.queue_registration.await_args.args[3]
    assert selected.start_at == browse.canonical_event.start_at
    assert selected.end_at == browse.canonical_event.end_at
    item = SignupItem(event=selected, updated_at=NOW)
    batch = SignupBatch(batch_id=uuid4(), request_id=request, created_at=NOW, items=[item])
    repository.batch.return_value = batch
    claimed = item.model_copy(update={"status": "in_progress", "attempt_id": attempt})
    repository.claim.return_value = claimed
    assert await service.claim(tenant, batch.batch_id, event_id, attempt) == claimed
    repository.claim.assert_awaited_once_with(tenant, batch.batch_id, event_id, attempt)


async def test_queued_future_snapshot_can_be_claimed_after_start_then_replayed_after_end(monkeypatch):
    clock(monkeypatch, NOW)
    browse = browse_at(NOW + timedelta(minutes=1), NOW + timedelta(hours=1))
    service, repository, _ = service_for(browse)
    tenant, attempt, event_id = uuid4(), uuid4(), browse.canonical_event.canonical_event_id
    batch = await service.prepare(tenant, uuid4(), [event_id])
    repository.batch.return_value = batch
    claimed = batch.items[0].model_copy(update={"status": "in_progress", "attempt_id": attempt})
    repository.claim.return_value = claimed
    clock(monkeypatch, NOW + timedelta(minutes=2))
    assert await service.claim(tenant, batch.batch_id, event_id, attempt) == claimed
    repository.batch.return_value = batch.model_copy(update={"items": [claimed]})
    clock(monkeypatch, NOW + timedelta(hours=2))
    assert await service.claim(tenant, batch.batch_id, event_id, attempt) == claimed
    assert repository.claim.await_count == 1


@pytest.mark.parametrize("start,end", [
    (-2, 0), (-2, -1), (-1, None), (0, None), (1, 1), (2, 1),
])
async def test_invalid_or_closed_occurrences_are_rejected_by_both_queue_and_claim(monkeypatch, start, end):
    clock(monkeypatch, NOW)
    browse = browse_at(NOW + timedelta(seconds=start), NOW + timedelta(seconds=end) if end is not None else None)
    service, repository, _ = service_for(browse)
    tenant, request, event_id = uuid4(), uuid4(), browse.canonical_event.canonical_event_id
    repository.queue_registration.side_effect = MuseNotFoundError()
    with pytest.raises(ValueError, match="upcoming or ongoing"):
        await service.queue(tenant, request, event_id)
    assert repository.queue_registration.await_count == 1
    # A retained queued snapshot must fail the same new-attempt guard without deleting its history.
    event = SignupEvent(
        canonical_event_id=event_id, title=browse.canonical_event.title,
        start_at=browse.canonical_event.start_at, end_at=browse.canonical_event.end_at,
        venue_name=browse.canonical_event.venue_name, city=browse.canonical_event.city_norm,
        registration_url=browse.sources[0].registration_url, observed_at=browse.sources[0].last_seen_at,
    )
    item = SignupItem(event=event, updated_at=NOW)
    batch = SignupBatch(batch_id=uuid4(), request_id=request, created_at=NOW, items=[item])
    repository.batch.return_value = batch
    with pytest.raises(MuseConflictError):
        await service.claim(tenant, batch.batch_id, event_id, uuid4())
    repository.claim.assert_not_awaited()
