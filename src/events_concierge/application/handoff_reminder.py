"""Task-scoped durable handoff reminders (FR-6.6, ADR-007/009)."""

from __future__ import annotations

from uuid import UUID

from ..domain.enums import HandoffReminderKind
from ..domain.lifecycle import HandoffReminderResult
from ..ports.repositories import HandoffRepository


class HandoffReminderService:
    """Ask the guarded repository to atomically enqueue one due handoff reminder.

    Temporal owns the durable timer and mints ``reminder_id`` once. This bounded activity-facing
    service deliberately has no clock or outbox access: PostgreSQL verifies the persisted task
    identity, activity state, and due instant before it writes the reminder ledger and outbox row
    in one transaction (FR-6.6, FR-8.3/8.9, ADR-003/007/009).
    """

    def __init__(self, handoffs: HandoffRepository) -> None:
        self._handoffs = handoffs

    async def enqueue(
        self,
        tenant_id: UUID,
        task_id: str,
        workflow_id: str,
        canonical_event_id: UUID,
        expiry_transition_id: str,
        reminder_kind: HandoffReminderKind,
        reminder_id: str,
    ) -> HandoffReminderResult:
        """Persist one replay-safe reminder instruction for a due active task (FR-6.6)."""
        return await self._handoffs.enqueue_reminder(
            tenant_id,
            task_id,
            workflow_id,
            canonical_event_id,
            expiry_transition_id,
            reminder_kind,
            reminder_id,
        )
