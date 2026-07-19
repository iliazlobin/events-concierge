"""Task-scoped durable handoff-reminder service tests (FR-6.6, ADR-007/009)."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from events_concierge.application.handoff_reminder import HandoffReminderService
from events_concierge.domain.enums import HandoffReminderKind, HandoffReminderStatus
from events_concierge.domain.lifecycle import HandoffReminderResult
from events_concierge.ports.repositories import HandoffRepository


class _HandoffRepository:
    """A narrow repository double that preserves every durable reminder identity."""

    def __init__(self, result: HandoffReminderResult) -> None:
        self._result = result
        self.calls: list[tuple[UUID, str, str, UUID, str, HandoffReminderKind, str]] = []

    async def enqueue_reminder(
        self,
        tenant_id: UUID,
        task_id: str,
        workflow_id: str,
        canonical_event_id: UUID,
        expiry_transition_id: str,
        reminder_kind: HandoffReminderKind,
        reminder_id: str,
    ) -> HandoffReminderResult:
        self.calls.append(
            (
                tenant_id,
                task_id,
                workflow_id,
                canonical_event_id,
                expiry_transition_id,
                reminder_kind,
                reminder_id,
            )
        )
        return self._result


_TENANT_ID = UUID("11111111-1111-1111-1111-111111111111")
_CANONICAL_EVENT_ID = UUID("22222222-2222-2222-2222-222222222222")
_WORKFLOW_ID = f"{_TENANT_ID}:{_CANONICAL_EVENT_ID}"
_TASK_ID = "01J00000000000000000000000"
_EXPIRY_TRANSITION_ID = f"{_WORKFLOW_ID}:handoff-expiry:{_TASK_ID}"
_REMINDER_ID = f"{_WORKFLOW_ID}:handoff-reminder:t24h"


async def test_reminder_service_forwards_the_once_minted_task_identity_unchanged() -> None:
    """Retries reach the guarded repository with every original identity (ADR-007/009)."""
    expected = HandoffReminderResult(HandoffReminderStatus.ENQUEUED)
    repository = _HandoffRepository(expected)
    service = HandoffReminderService(cast(HandoffRepository, repository))

    result = await service.enqueue(
        _TENANT_ID,
        _TASK_ID,
        _WORKFLOW_ID,
        _CANONICAL_EVENT_ID,
        _EXPIRY_TRANSITION_ID,
        HandoffReminderKind.T24H,
        _REMINDER_ID,
    )

    assert result is expected
    assert repository.calls == [
        (
            _TENANT_ID,
            _TASK_ID,
            _WORKFLOW_ID,
            _CANONICAL_EVENT_ID,
            _EXPIRY_TRANSITION_ID,
            HandoffReminderKind.T24H,
            _REMINDER_ID,
        )
    ]


async def test_reminder_service_preserves_a_guarded_not_due_outcome() -> None:
    """The workflow can safely wait when persistence says the durable task is not due yet."""
    expected = HandoffReminderResult(HandoffReminderStatus.NOT_DUE, "due at persisted timestamp")
    repository = _HandoffRepository(expected)
    service = HandoffReminderService(cast(HandoffRepository, repository))

    result = await service.enqueue(
        _TENANT_ID,
        _TASK_ID,
        _WORKFLOW_ID,
        _CANONICAL_EVENT_ID,
        _EXPIRY_TRANSITION_ID,
        HandoffReminderKind.T5D,
        f"{_WORKFLOW_ID}:handoff-reminder:t5d",
    )

    assert result is expected
    assert repository.calls[0][-2] is HandoffReminderKind.T5D
