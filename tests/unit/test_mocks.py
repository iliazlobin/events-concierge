"""Slice tests for the in-memory mock cloud adapters: idempotent calendar upsert + notifier dedup."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from events_concierge.adapters.mock import MockCalendar, MockNotifier
from events_concierge.adapters.mock.sources import ConfirmingSource
from events_concierge.domain.enums import Modality, Source
from events_concierge.ports.calendar import CalendarEntry
from events_concierge.ports.notifications import Notification, NotificationKind
from events_concierge.ports.sources import RegistrationTarget


def _entry(calendar_event_id: str, title: str) -> CalendarEntry:
    return CalendarEntry(
        calendar_event_id=calendar_event_id,
        canonical_event_id=uuid4(),
        title=title,
        start_at=datetime(2026, 7, 15, 18, 0, tzinfo=UTC),
        end_at=datetime(2026, 7, 15, 20, 0, tzinfo=UTC),
        time_zone="America/New_York",
    )


@pytest.mark.asyncio
async def test_calendar_upsert_is_idempotent() -> None:
    """Upserting the same calendar_event_id twice collapses to one entry (FR-9.2)."""
    calendar = MockCalendar()
    tenant_id = uuid4()

    await calendar.upsert_event(tenant_id, _entry("evt-1", "First title"))
    await calendar.upsert_event(tenant_id, _entry("evt-1", "Patched title"))

    entries = calendar.entries(tenant_id)
    assert len(entries) == 1
    assert entries[0].title == "Patched title"


@pytest.mark.asyncio
async def test_calendar_delete_removes_entry() -> None:
    """delete_event accepts a canonical lookup key and remains idempotent in the offline double."""
    calendar = MockCalendar()
    tenant_id = uuid4()
    entry = _entry("evt-1", "Title")

    await calendar.upsert_event(tenant_id, entry)
    await calendar.delete_event(
        tenant_id,
        "evt-1",
        canonical_event_id=entry.canonical_event_id,
    )
    assert calendar.entries(tenant_id) == []

    await calendar.delete_event(tenant_id, "evt-1")  # idempotent
    assert calendar.entries(tenant_id) == []


@pytest.mark.asyncio
async def test_notifier_dedups_by_dedup_key() -> None:
    """The dedup_key makes delivery at-most-once user-visible under retry."""
    notifier = MockNotifier()
    tenant_id = uuid4()

    def _notification(dedup_key: str, subject: str) -> Notification:
        return Notification(
            tenant_id=tenant_id,
            kind=NotificationKind.COMPLETION,
            subject=subject,
            body="body",
            dedup_key=dedup_key,
        )

    await notifier.send(_notification("k-1", "First"))
    await notifier.send(_notification("k-1", "Retry"))  # same key -> suppressed
    await notifier.send(_notification("k-2", "Distinct"))

    assert len(notifier.sent) == 2
    assert [n.subject for n in notifier.sent] == ["First", "Distinct"]


@pytest.mark.asyncio
async def test_confirming_source_observes_effect_before_simulated_pending_ack_loss() -> None:
    """The fixture exposes the one RSVP effect before its lost-ACK seam (NFR-8, ADR-003)."""
    source = ConfirmingSource(Source.MEETUP, pending_after_effect_once=True)

    with pytest.raises(RuntimeError, match="simulated pending RSVP acknowledgement loss"):
        await source.register(
            uuid4(),
            RegistrationTarget("fixture-event", "https://fixture.invalid/event"),
            Modality.API,
            "fixture-key",
        )

    await asyncio.wait_for(source.wait_for_registration_effect(), timeout=0.1)
    assert source.registration_effects == 1
