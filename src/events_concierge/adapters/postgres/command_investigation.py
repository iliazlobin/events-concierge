"""Capability-only command execution persistence and bounded investigation projections."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import asdict, replace
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...application.catalog_execution_descriptors import CatalogExecutionDescriptorRegistry
from ...domain.ingestion_admin import IngestionCommandLease, IngestionCommandRunTarget
from ...domain.ingestion_investigation import CommandInvestigationLeaseLostError, CommandTask
from ...infra.db import system_session_scope
from .ingestion_admin import _run_status_from_row

SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_MAX_EVENT_PAGE = 200


class CommandInvestigationStore:
    """Never query operational tables directly or use a consumer connection in hosted mode."""

    def __init__(
        self,
        *,
        session_scope: SessionScope = system_session_scope,
        task_queue: str = "events-concierge",
    ) -> None:
        self._session_scope = session_scope
        self._descriptors = CatalogExecutionDescriptorRegistry(task_queue)

    async def _execute(
        self,
        lease: IngestionCommandLease,
        operation: str,
        payload: dict[str, object],
    ) -> dict[str, Any]:
        async with self._session_scope() as session:
            result = (
                await session.execute(
                    text("""
                SELECT public.fn_command_execution_v1(:id,:attempt,:token,:operation,CAST(:payload AS jsonb))
            """),
                    {
                        "id": lease.command_id,
                        "attempt": lease.attempt_count,
                        "token": lease.lease_token,
                        "operation": operation,
                        "payload": json.dumps(payload),
                    },
                )
            ).scalar_one()
        if result.get("lease_lost"):
            raise CommandInvestigationLeaseLostError("command execution lease lost")
        return cast("dict[str, Any]", result)

    async def get_plan(self, lease: IngestionCommandLease) -> tuple[CommandTask, ...] | None:
        result = await self._execute(lease, "get_plan", {})
        return tuple(_task(item) for item in result["tasks"]) if result["planned"] else None

    async def ensure_plan(
        self,
        lease: IngestionCommandLease,
        targets: tuple[IngestionCommandRunTarget, ...],
    ) -> tuple[CommandTask, ...]:
        result = await self._execute(
            lease, "ensure_plan", {"targets": [asdict(target) for target in targets]}
        )
        return tuple(_task(item) for item in result["tasks"])

    async def start_task(
        self,
        lease: IngestionCommandLease,
        task: CommandTask,
        *,
        worker_id: str,
        release_revision: str,
        image_digest: str | None,
    ) -> CommandTask | None:
        result = await self._execute(
            lease,
            "start_task",
            {
                "source_key": task.source_key,
                "run_key": task.run_key,
                "worker_id": worker_id,
                "release_revision": release_revision,
                "image_digest": image_digest,
            },
        )
        return _task(result["task"]) if result["task"] is not None else None

    async def finish_task(
        self,
        lease: IngestionCommandLease,
        task: CommandTask,
        outcome_code: str,
        *,
        candidate_count: int = 0,
        canonical_count: int = 0,
        retry_after_seconds: int | None = None,
    ) -> bool:
        result = await self._execute(
            lease,
            "finish_task",
            {
                "source_key": task.source_key,
                "run_key": task.run_key,
                "task_attempt": task.attempt_count,
                "outcome_code": outcome_code,
                "candidate_count": candidate_count,
                "canonical_count": canonical_count,
                "retry_after_seconds": retry_after_seconds,
            },
        )
        return bool(result["updated"])

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
    ) -> bool:
        try:
            result = await self._execute(
                lease,
                "record_event",
                {
                    "event_id": str(event_id or uuid4()),
                    "event_code": event_code,
                    "source_key": source_key,
                    "run_key": run_key,
                    "task_attempt": task_attempt,
                    "stage": stage,
                    "outcome_code": outcome_code,
                    "duration_ms": duration_ms,
                    "candidate_count": candidate_count,
                    "canonical_count": canonical_count,
                    "request_count": request_count,
                    "page_count": page_count,
                    "error_type": error_type,
                    "worker_id": worker_id,
                    "release_revision": release_revision,
                    "image_digest": image_digest,
                },
            )
        except CommandInvestigationLeaseLostError:
            return False
        return bool(result["updated"])

    async def investigation(
        self,
        command_id: UUID,
        *,
        after_event_id: int = 0,
        limit: int = 100,
        source_key: str | None = None,
    ) -> dict[str, Any] | None:
        """Read the latest event page initially, then follow forward from its returned cursor."""
        if after_event_id < 0 or not 1 <= limit <= _MAX_EVENT_PAGE:
            raise ValueError("invalid investigation cursor")
        async with self._session_scope() as session:
            result = (
                await session.execute(
                    text("""
                SELECT public.fn_get_command_investigation_v1(:id,:after,:limit)
            """),
                    {"id": command_id, "after": after_event_id, "limit": limit},
                )
            ).scalar_one()
            if result is None:
                return None
            tasks = result["plan"]["tasks"]
            selected = source_key or (tasks[0]["source_key"] if tasks else None)
            for task in tasks:
                task["run"] = None
                if task["source_key"] != selected:
                    continue
                row = (
                    await session.execute(
                        text("SELECT public.fn_get_command_run_evidence_v1(:id,:source)"),
                        {"id": command_id, "source": selected},
                    )
                ).scalar_one()
                if row is not None:
                    for key, value in row.items():
                        if key.endswith("_at") and isinstance(value, str):
                            row[key] = datetime.fromisoformat(value)
                    if row.get("command_id") is not None:
                        row["command_id"] = UUID(row["command_id"])
                    run = _run_status_from_row(row, self._descriptors)
                    # Both manual and cadence commands now execute their direct tasks through
                    # the command worker. Preserve the true run trigger while resolving that
                    # command-owned code path; this registry is not historical tracing.
                    command_execution = self._descriptors.describe(
                        source_key=str(row["source_key"]),
                        mode=row.get("mode"),
                        page_limit=row.get("page_limit"),
                        trigger="admin_source",
                    )
                    task["run"] = asdict(replace(run, execution=command_execution))
        for event in result["events"]:
            event["event_id"] = str(event["event_id"])
        return cast("dict[str, Any]", result)


def _task(value: dict[str, Any]) -> CommandTask:
    value = value.copy()
    for field in ("available_at", "started_at", "completed_at", "last_progress_at"):
        if value[field] is not None:
            value[field] = datetime.fromisoformat(value[field])
    return CommandTask(**value)
