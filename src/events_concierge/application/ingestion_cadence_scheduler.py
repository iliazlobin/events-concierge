"""Durably schedule local ingestion cadence work without performing provider I/O.

The scheduler probes only the reviewed due-source projection and appends one fleet command to the
existing ingestion-admin queue. PostgreSQL remains authoritative for both active-command
exclusion and command replay; this service contributes a deterministic time-slot command UUID so
process restarts converge on the same durable request.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID, uuid5

from ..domain.catalog_sources import CatalogRefreshDue
from ..domain.ingestion_admin import (
    IngestionBuildIdentity,
    IngestionCommand,
    IngestionCommandAction,
    IngestionCommandStatus,
)
from ..ports.ingestion_admin import (
    IngestionCommandConflictError,
    IngestionCommandUnavailableError,
)

_MIN_INTERVAL_SECONDS = 60
_MAX_INTERVAL_SECONDS = 3_600
_SCHEDULER_ACTOR = "local-cadence-scheduler"
_COMMAND_NAMESPACE = UUID("b5d9a886-8e9d-4d3d-b25f-1c71bbd49e2d")


class IngestionCadenceQueue(Protocol):
    """Small queue seam needed by the enqueue-only cadence scheduler."""

    async def list_due_refreshes(
        self,
        now: datetime,
        *,
        limit: int,
    ) -> list[CatalogRefreshDue]: ...

    async def enqueue(
        self,
        command_id: UUID,
        action: IngestionCommandAction,
        source_key: str | None,
        requested_by: str,
        release_revision: str,
        image_digest: str | None,
    ) -> IngestionCommand: ...


class IngestionCadenceScheduleOutcome(StrEnum):
    """Closed operational result for one scheduler probe."""

    IDLE = "idle"
    SCHEDULED = "scheduled"
    ACTIVE_COMMAND = "active_command"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class IngestionCadenceScheduleReport:
    """PII-free result of checking one bounded scheduler slot."""

    outcome: IngestionCadenceScheduleOutcome
    due_sources: int
    command_id: UUID | None = None
    command_status: IngestionCommandStatus | None = None


class IngestionCadenceScheduler:
    """Append at most one deterministic due-fleet command per bounded time slot."""

    def __init__(
        self,
        repository: IngestionCadenceQueue,
        *,
        interval_seconds: int = 300,
        release_revision: str = "development",
        image_digest: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not _MIN_INTERVAL_SECONDS <= interval_seconds <= _MAX_INTERVAL_SECONDS:
            raise ValueError("ingestion cadence interval must be between 60 and 3600 seconds")
        self._repository = repository
        self._interval_seconds = interval_seconds
        self._build_identity = IngestionBuildIdentity(
            release_revision=release_revision,
            image_digest=image_digest,
        )
        self._now = now or (lambda: datetime.now(UTC))

    async def schedule_once(self) -> IngestionCadenceScheduleReport:
        """Queue a fleet pass only when at least one reviewed non-fixture source is due."""
        now = _utc(self._now())
        due = await self._repository.list_due_refreshes(now, limit=1)
        if not due:
            return IngestionCadenceScheduleReport(
                outcome=IngestionCadenceScheduleOutcome.IDLE,
                due_sources=0,
            )

        command_id = cadence_command_id(now, interval_seconds=self._interval_seconds)
        try:
            command = await self._repository.enqueue(
                command_id,
                IngestionCommandAction.REFRESH_DUE,
                None,
                _SCHEDULER_ACTOR,
                self._build_identity.release_revision,
                self._build_identity.image_digest,
            )
        except IngestionCommandConflictError:
            # A different slot or an operator already owns the unique active fleet-command target.
            # The next scheduler probe will re-read due state after that durable work progresses.
            return IngestionCadenceScheduleReport(
                outcome=IngestionCadenceScheduleOutcome.ACTIVE_COMMAND,
                due_sources=1,
                command_id=command_id,
            )
        except IngestionCommandUnavailableError:
            # Admission policy can change after the due read. Preserve that fail-closed result as
            # ordinary scheduler posture rather than treating it as an infrastructure exception.
            return IngestionCadenceScheduleReport(
                outcome=IngestionCadenceScheduleOutcome.UNAVAILABLE,
                due_sources=1,
                command_id=command_id,
            )
        return IngestionCadenceScheduleReport(
            outcome=IngestionCadenceScheduleOutcome.SCHEDULED,
            due_sources=1,
            command_id=command.command_id,
            command_status=command.status,
        )


def cadence_command_id(now: datetime, *, interval_seconds: int) -> UUID:
    """Return one stable UUID for the UTC scheduler interval containing ``now``."""
    if not _MIN_INTERVAL_SECONDS <= interval_seconds <= _MAX_INTERVAL_SECONDS:
        raise ValueError("ingestion cadence interval must be between 60 and 3600 seconds")
    timestamp = int(_utc(now).timestamp())
    slot = timestamp // interval_seconds
    return uuid5(_COMMAND_NAMESPACE, f"refresh-due:{interval_seconds}:{slot}")


def _utc(value: datetime) -> datetime:
    """Reject ambiguous scheduler clocks before they can mint an idempotency identity."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("ingestion cadence clock must return a timezone-aware datetime")
    return value.astimezone(UTC)
