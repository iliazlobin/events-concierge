"""Outer Temporal fanout adapter tests for the ADR-008 delivery worker."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

import pytest
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from events_concierge.domain.enums import EventStatus, Source
from events_concierge.ports.calendar_repair import ClosedWorkflowSignalError
from events_concierge.ports.change_detection import (
    DetectedEventChange,
    OrganizerChangeDelivery,
    organizer_change_fingerprint,
)
from events_concierge.workflows.dto import OrganizerChangeSignal
from events_concierge.workflows.start import TemporalOrganizerChangeFanout


class RecordingWorkflowHandle:
    """Minimal Temporal handle seam that records one signal payload."""

    def __init__(self) -> None:
        self.signals: list[tuple[str, OrganizerChangeSignal]] = []

    async def signal(self, name: str, arg: OrganizerChangeSignal) -> None:
        self.signals.append((name, arg))


class RecordingClient:
    """Minimal client surface exercised by the outer adapter."""

    def __init__(self, handle: RecordingWorkflowHandle) -> None:
        self.handle = handle
        self.workflow_ids: list[str] = []

    def get_workflow_handle(self, workflow_id: str) -> RecordingWorkflowHandle:
        self.workflow_ids.append(workflow_id)
        return self.handle


class NotFoundWorkflowHandle(RecordingWorkflowHandle):
    """Model Temporal's authoritative closed-workflow signal response."""

    async def signal(self, name: str, arg: OrganizerChangeSignal) -> None:
        del name, arg
        raise RPCError("workflow execution not found", RPCStatusCode.NOT_FOUND, b"")


async def test_temporal_fanout_preserves_normalized_change_and_workflow_identity() -> None:
    """The worker signals only the opaque target and normalized ADR-008 command."""
    tenant_id = uuid4()
    canonical_event_id = uuid4()
    workflow_id = f"{tenant_id}:{canonical_event_id}"
    start_at = datetime(2026, 8, 1, 18, tzinfo=UTC)
    end_at = start_at + timedelta(hours=2)
    change = DetectedEventChange(
        fingerprint=organizer_change_fingerprint(
            canonical_event_id, EventStatus.RESCHEDULED, start_at, end_at
        ),
        canonical_event_id=canonical_event_id,
        source=Source.LUMA,
        event_status=EventStatus.RESCHEDULED,
        start_at=start_at,
        end_at=end_at,
        time_zone="America/Los_Angeles",
        title="Moved fixture",
        venue_name="Fixture Hall",
    )
    delivery = OrganizerChangeDelivery(
        change=change,
        tenant_id=tenant_id,
        workflow_id=workflow_id,
        attempt_count=1,
        lease_token="lease",
    )
    handle = RecordingWorkflowHandle()
    client = RecordingClient(handle)
    fanout = TemporalOrganizerChangeFanout(cast(Client, client))

    await fanout.signal_organizer_change(delivery)

    assert client.workflow_ids == [workflow_id]
    assert handle.signals == [
        (
            "organizer_change",
            OrganizerChangeSignal(
                fingerprint=change.fingerprint,
                canonical_event_id=str(canonical_event_id),
                source="luma",
                event_status="rescheduled",
                start_at=start_at.isoformat(),
                end_at=end_at.isoformat(),
                time_zone="America/Los_Angeles",
                title="Moved fixture",
                venue_name="Fixture Hall",
            ),
        )
    ]


async def test_temporal_fanout_refuses_a_malformed_global_delivery_identity() -> None:
    """An opaque control-row corruption can never signal a guessed cross-tenant workflow (ADR-003/008)."""
    tenant_id = uuid4()
    canonical_event_id = uuid4()
    start_at = datetime(2026, 8, 1, 18, tzinfo=UTC)
    end_at = start_at + timedelta(hours=2)
    change = DetectedEventChange(
        fingerprint=organizer_change_fingerprint(
            canonical_event_id, EventStatus.RESCHEDULED, start_at, end_at
        ),
        canonical_event_id=canonical_event_id,
        source=Source.LUMA,
        event_status=EventStatus.RESCHEDULED,
        start_at=start_at,
        end_at=end_at,
    )
    delivery = OrganizerChangeDelivery(
        change=change,
        tenant_id=tenant_id,
        workflow_id=f"wrong:{uuid4()}",
        attempt_count=1,
        lease_token="lease",
    )
    handle = RecordingWorkflowHandle()
    client = RecordingClient(handle)

    with pytest.raises(ClosedWorkflowSignalError, match="deterministic"):
        await TemporalOrganizerChangeFanout(cast(Client, client)).signal_organizer_change(delivery)

    assert client.workflow_ids == []
    assert handle.signals == []


async def test_temporal_fanout_classifies_a_closed_target_for_delayed_calendar_repair() -> None:
    """NOT_FOUND is not retried as an ordinary transport failure (ADR-008)."""
    tenant_id = uuid4()
    canonical_event_id = uuid4()
    start_at = datetime(2026, 8, 1, 18, tzinfo=UTC)
    end_at = start_at + timedelta(hours=2)
    change = DetectedEventChange(
        fingerprint=organizer_change_fingerprint(
            canonical_event_id, EventStatus.RESCHEDULED, start_at, end_at
        ),
        canonical_event_id=canonical_event_id,
        source=Source.LUMA,
        event_status=EventStatus.RESCHEDULED,
        start_at=start_at,
        end_at=end_at,
    )
    delivery = OrganizerChangeDelivery(
        change=change,
        tenant_id=tenant_id,
        workflow_id=f"{tenant_id}:{canonical_event_id}",
        attempt_count=1,
        lease_token="lease",
    )
    client = RecordingClient(NotFoundWorkflowHandle())

    with pytest.raises(ClosedWorkflowSignalError, match="closed"):
        await TemporalOrganizerChangeFanout(cast(Client, client)).signal_organizer_change(delivery)
