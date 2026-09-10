"""Lease-fenced persistence for resumable ingestion command plans."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from ..domain.ingestion_admin import IngestionCommandLease, IngestionCommandRunTarget
from ..domain.ingestion_investigation import CommandTask


class CommandExecutionUncertainError(RuntimeError):
    """Persistence acknowledgement is uncertain; re-enter the exact source run to reconcile."""


class CommandExecutionStore(Protocol):
    async def get_plan(self, lease: IngestionCommandLease) -> tuple[CommandTask, ...] | None: ...

    async def ensure_plan(
        self, lease: IngestionCommandLease, targets: tuple[IngestionCommandRunTarget, ...]
    ) -> tuple[CommandTask, ...]: ...

    async def start_task(
        self,
        lease: IngestionCommandLease,
        task: CommandTask,
        *,
        worker_id: str,
        release_revision: str,
        image_digest: str | None,
    ) -> CommandTask | None: ...

    async def finish_task(
        self,
        lease: IngestionCommandLease,
        task: CommandTask,
        outcome_code: str,
        *,
        candidate_count: int = 0,
        canonical_count: int = 0,
        retry_after_seconds: int | None = None,
    ) -> bool: ...

    async def record_event(
        self,
        lease: IngestionCommandLease,
        event_code: str,
        *,
        source_key: str | None = None,
        run_key: str | None = None,
        task_attempt: int | None = None,
        stage: str | None = None,
        outcome_code: str | None = None,
        duration_ms: int | None = None,
        candidate_count: int | None = None,
        canonical_count: int | None = None,
        request_count: int | None = None,
        page_count: int | None = None,
        error_type: str | None = None,
        worker_id: str | None = None,
        release_revision: str | None = None,
        image_digest: str | None = None,
        event_id: UUID | None = None,
    ) -> bool: ...
