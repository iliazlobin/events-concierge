"""Resume a fixed ingestion plan one source at a time, yielding between sources.

The command lease is still the authority for effects. A persisted source task is a continuation,
not a new provider authority: every entry reuses its run key and the guarded refresh router. Queue
availability order lets already-waiting commands run before the next fleet continuation.
"""

from __future__ import annotations

import asyncio
import math
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, datetime
from time import monotonic
from typing import Literal
from uuid import uuid4

from ..domain.ingestion_admin import (
    IngestionCommandAction,
    IngestionCommandLease,
    IngestionCommandRunTarget,
    IngestionProcessReport,
    SafeCommandResult,
)
from ..domain.ingestion_investigation import CommandInvestigationLeaseLostError, CommandTask
from ..infra.logging import get_logger
from ..ports.ingestion_admin import IngestionAdminRepository
from ..ports.ingestion_command_execution import (
    CommandExecutionStore,
    CommandExecutionUncertainError,
)
from .catalog_refresh import CatalogRefreshOutcome, CatalogRefreshResult
from .catalog_refresh_dispatcher import CatalogRefreshRunner
from .ingestion_telemetry import (
    IngestionTelemetryContext,
    IngestionTelemetryEvent,
    bind_ingestion_telemetry,
    emit_ingestion_event,
    ingestion_error_type,
)

_log = get_logger(__name__)
_UNRESOLVED = frozenset({"pending", "running", "deferred", "busy"})
_MAX_RETRY_SECONDS = 21_600
_BUSY_RETRY_SECONDS = 60
_MIN_LEASE_SECONDS = 300
_MAX_PLAN_SIZE = 500
_MAX_CLAIM_BATCH = 100
_RENEWAL_TIMEOUT_SECONDS = 10.0
_UNCERTAIN_PERSISTENCE_ERRORS = (
    ConnectionError,
    TimeoutError,
    CommandExecutionUncertainError,
)
type _Disposition = Literal["completed", "deferred", "failed", "lost_lease"]


class _LeaseLostError(RuntimeError):
    pass


class ResumableIngestionCommandProcessor:
    """Execute one source per claim, with durable plan and per-source attempt budgets."""

    def __init__(
        self,
        repository: IngestionAdminRepository,
        router: CatalogRefreshRunner,
        store: CommandExecutionStore,
        *,
        lease_seconds: int = 300,
        lease_heartbeat_seconds: float | None = None,
        cadence_batch_size: int = 50,
        release_revision: str = "development",
        image_digest: str | None = None,
        worker_id: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        heartbeat = (
            min(60.0, lease_seconds / 3)
            if lease_heartbeat_seconds is None
            else lease_heartbeat_seconds
        )
        if not _MIN_LEASE_SECONDS <= lease_seconds <= _MAX_RETRY_SECONDS:
            raise ValueError("command lease must be between 300 and 21600 seconds")
        if not math.isfinite(heartbeat) or not 0 < heartbeat < lease_seconds:
            raise ValueError("command heartbeat must be positive and shorter than its lease")
        if not 1 <= cadence_batch_size <= _MAX_PLAN_SIZE:
            raise ValueError("cadence batch size must be between 1 and 500")
        self._repository = repository
        self._router = router
        self._store = store
        self._lease_seconds = lease_seconds
        self._heartbeat = heartbeat
        self._cadence_batch_size = cadence_batch_size
        self._release = release_revision
        self._image = image_digest
        self._worker_id = worker_id or f"ingestion-{uuid4().hex}"
        self._now = now or (lambda: datetime.now(UTC))

    async def process_once(self, limit: int = 10) -> IngestionProcessReport:
        if not 1 <= limit <= _MAX_CLAIM_BATCH:
            raise ValueError("command batch size must be between 1 and 100")
        dispositions: list[_Disposition] = []
        for _ in range(limit):
            leases = await self._repository.claim_batch(
                1, self._lease_seconds, self._release, self._image
            )
            if not leases:
                break
            dispositions.append(await self._process(leases[0]))
        return IngestionProcessReport(
            claimed=len(dispositions),
            completed=dispositions.count("completed"),
            deferred=dispositions.count("deferred"),
            failed=dispositions.count("failed"),
            lost_leases=dispositions.count("lost_lease"),
        )

    async def _process(self, lease: IngestionCommandLease) -> _Disposition:
        await self._observe(lease, "command_started")
        try:
            tasks = await self._with_heartbeat(lease)
            unresolved = [task for task in tasks if task.status in _UNRESOLVED]
            if unresolved:
                # Use the DB-relative wakeup hint so worker clock skew cannot postpone a retry.
                # start_task checks availability again before allowing an effect.
                retry = _next_wakeup(unresolved, self._now())
                continuing = any(task.status == "pending" for task in unresolved)
                await self._observe(
                    lease, "continuation_scheduled" if continuing else "retry_scheduled"
                )
                deferred = await self._repository.defer(lease, retry)
                return "deferred" if deferred else "lost_lease"
            result = _result(lease, tasks)
            if lease.action is IngestionCommandAction.REFRESH_SOURCE and any(
                task.status == "failed" for task in tasks
            ):
                failed = await self._repository.fail(lease, "source_refresh_failed")
                return "failed" if failed else "lost_lease"
            completed = await self._repository.complete(lease, result)
            return "completed" if completed else "lost_lease"
        except asyncio.CancelledError:
            raise
        except (_LeaseLostError, CommandInvestigationLeaseLostError):
            await self._observe(lease, "lease_lost")
            return "lost_lease"
        except Exception as error:
            # A persistence failure can have an unknown commit result. Preserve the exact run keys
            # and let lease reclaim recover, rather than inventing a terminal failure or new work.
            await self._observe(lease, "execution_error", error_type=ingestion_error_type(error))
            return "lost_lease"

    async def _with_heartbeat(self, lease: IngestionCommandLease) -> tuple[CommandTask, ...]:
        operation = asyncio.create_task(self._execute_one(lease))
        previous_renewal = monotonic()
        try:
            while True:
                done, _ = await asyncio.wait({operation}, timeout=self._heartbeat)
                if operation in done:
                    return await operation
                renewal_gap_ms = max(0, int((monotonic() - previous_renewal) * 1000))
                try:
                    async with asyncio.timeout(_RENEWAL_TIMEOUT_SECONDS):
                        renewed = await self._repository.renew_lease(lease, self._lease_seconds)
                except Exception as error:
                    # Stop the operation before attempting best-effort diagnostics.
                    operation.cancel()
                    with suppress(asyncio.CancelledError):
                        await operation
                    await self._observe(
                        lease,
                        "lease_renewal_error",
                        error_type=ingestion_error_type(error),
                        duration_ms=renewal_gap_ms,
                    )
                    raise _LeaseLostError from error
                if not renewed:
                    operation.cancel()
                    with suppress(asyncio.CancelledError):
                        await operation
                    await self._observe(lease, "lease_lost", duration_ms=renewal_gap_ms)
                    raise _LeaseLostError
                previous_renewal = monotonic()
                await self._observe(lease, "lease_renewed", duration_ms=renewal_gap_ms)
        finally:
            if not operation.done():
                operation.cancel()
                with suppress(asyncio.CancelledError):
                    await operation

    async def _execute_one(self, lease: IngestionCommandLease) -> tuple[CommandTask, ...]:
        tasks = await self._store.get_plan(lease)
        if tasks is None:
            targets = await self._plan(lease)
            tasks = await self._store.ensure_plan(lease, targets)
        # Preserve the existing command/run navigation for every continuation, without reselecting
        # due sources or changing the plan. Links are committed before any provider boundary.
        await self._repository.link_command_runs(
            lease.command_id,
            lease.attempt_count,
            lease.lease_token,
            tuple(
                IngestionCommandRunTarget(task.position, task.source_key, task.run_key)
                for task in tasks
            ),
        )
        for task in tasks:
            if task.status not in _UNRESOLVED:
                continue
            started = await self._store.start_task(
                lease,
                task,
                worker_id=self._worker_id,
                release_revision=self._release,
                image_digest=self._image,
            )
            if started is None:
                continue
            await self._run_task(lease, started)
            break
        refreshed = await self._store.get_plan(lease)
        if refreshed is None:
            raise RuntimeError("durable command plan disappeared")
        return refreshed

    async def _plan(self, lease: IngestionCommandLease) -> tuple[IngestionCommandRunTarget, ...]:
        if lease.action is IngestionCommandAction.REFRESH_SOURCE:
            if lease.source_key is None:
                raise ValueError("source command requires a source key")
            return (IngestionCommandRunTarget(0, lease.source_key, f"admin:{lease.command_id}"),)
        if lease.action is not IngestionCommandAction.REFRESH_DUE:
            raise ValueError("unsupported ingestion command action")
        due = await self._repository.list_due_refreshes(self._now(), limit=self._cadence_batch_size)
        ordered = sorted(due, key=lambda item: (item.due_at, item.source.source_key))
        return tuple(
            IngestionCommandRunTarget(position, item.source.source_key, item.run_key())
            for position, item in enumerate(ordered)
        )

    async def _run_task(self, lease: IngestionCommandLease, task: CommandTask) -> None:
        context = IngestionTelemetryContext(
            command_id=lease.command_id,
            command_attempt=lease.attempt_count,
            source_key=task.source_key,
            run_key=task.run_key,
            task_attempt=task.attempt_count,
            worker_id=self._worker_id,
            release_revision=self._release,
            image_digest=self._image,
        )

        async def emit(event: IngestionTelemetryEvent) -> None:
            await self._store.record_event(
                lease,
                event.event_code,
                source_key=task.source_key,
                run_key=task.run_key,
                task_attempt=task.attempt_count,
                stage=event.stage,
                outcome_code=event.outcome_code,
                duration_ms=event.duration_ms,
                candidate_count=event.candidate_count,
                canonical_count=event.canonical_count,
                request_count=event.request_count,
                page_count=event.page_count,
                error_type=event.error_type,
                worker_id=self._worker_id,
                release_revision=self._release,
                image_digest=self._image,
                event_id=event.event_id,
            )

        with bind_ingestion_telemetry(context, emit=emit):
            try:
                result = await self._router.refresh(task.source_key, task.run_key)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await emit_ingestion_event("error", error_type=ingestion_error_type(error))
                # A lost acknowledgement can follow a successful catalog commit. Re-enter the
                # exact run key so its source ledger can reconcile success before any new fetch.
                # Deterministic validation/provider failures keep their normal terminal result.
                uncertain = isinstance(error, _UNCERTAIN_PERSISTENCE_ERRORS)
                outcome, delay = (
                    _task_outcome(
                        CatalogRefreshResult(
                            task.source_key,
                            task.run_key,
                            CatalogRefreshOutcome.DEFERRED,
                            retry_after_seconds=_BUSY_RETRY_SECONDS,
                        ),
                        task.attempt_count,
                        lease.action,
                    )
                    if uncertain
                    else ("failed", None)
                )
                if not await self._store.finish_task(
                    lease, task, outcome, retry_after_seconds=delay
                ):
                    raise _LeaseLostError from error
                return
            outcome, delay = _task_outcome(result, task.attempt_count, lease.action)
            if not await self._store.finish_task(
                lease,
                task,
                outcome,
                candidate_count=max(0, result.candidate_count),
                canonical_count=max(0, result.canonical_count),
                retry_after_seconds=delay,
            ):
                raise _LeaseLostError

    async def _observe(
        self,
        lease: IngestionCommandLease,
        event_code: str,
        *,
        error_type: str | None = None,
        duration_ms: int | None = None,
    ) -> None:
        fields = {
            "command_id": str(lease.command_id),
            "command_attempt": lease.attempt_count,
            "worker_id": self._worker_id,
            "release_revision": self._release,
        }
        # Closed error categories and measured renewal gaps; exception messages stay private.
        with suppress(Exception):
            _log.info(event_code, **fields, error_type=error_type, duration_ms=duration_ms)
        try:
            async with asyncio.timeout(2):
                await self._store.record_event(
                    lease,
                    event_code,
                    worker_id=self._worker_id,
                    release_revision=self._release,
                    image_digest=self._image,
                    error_type=error_type,
                    duration_ms=duration_ms,
                )
        except Exception:
            with suppress(Exception):
                _log.warning("command observation unavailable", **fields)


def _task_outcome(
    result: CatalogRefreshResult, attempt: int, action: IngestionCommandAction
) -> tuple[str, int | None]:
    if result.outcome not in {
        CatalogRefreshOutcome.DEFERRED,
        CatalogRefreshOutcome.BUSY,
        CatalogRefreshOutcome.PROGRESSED,
    }:
        return result.outcome.value, None
    # A continuation claim is not a retry of every source in the fleet. Cap only this source's
    # attempts, so large plans and successful earlier work do not consume another source's budget.
    attempt_limit = 50 if action is IngestionCommandAction.REFRESH_DUE else 5
    if attempt >= attempt_limit:
        return "retry_exhausted", None
    delay = result.retry_after_seconds
    if delay is None or not math.isfinite(delay) or delay <= 0:
        delay = 1 if result.outcome is CatalogRefreshOutcome.PROGRESSED else _BUSY_RETRY_SECONDS
    return result.outcome.value, min(_MAX_RETRY_SECONDS, max(1, math.ceil(delay)))


def _next_wakeup(tasks: list[CommandTask], now: datetime) -> int:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("command clock must be timezone-aware")
    delays = [
        1
        if task.status == "pending"
        else max(1, task.retry_after_seconds)
        if task.retry_after_seconds is not None
        else max(1, math.ceil((task.available_at - now).total_seconds()))
        if task.available_at is not None
        else 1
        for task in tasks
    ]
    return min(_MAX_RETRY_SECONDS, min(delays, default=1))


def _result(lease: IngestionCommandLease, tasks: tuple[CommandTask, ...]) -> SafeCommandResult:
    if lease.action is IngestionCommandAction.REFRESH_SOURCE:
        task = tasks[0]
        return {
            "action": lease.action.value,
            "source_key": task.source_key,
            "run_key": task.run_key,
            "outcome": task.last_outcome_code,
            "candidate_count": task.candidate_count or 0,
            "canonical_count": task.canonical_count or 0,
        }
    result: SafeCommandResult = {
        "action": lease.action.value,
        "due_sources": len(tasks),
        "attempted": len(tasks),
    }
    for outcome in (
        "succeeded",
        "queued",
        "skipped",
        "deferred",
        "already_succeeded",
        "busy",
        "progressed",
        "failed",
    ):
        result[outcome] = sum(
            task.status == "failed" if outcome == "failed" else task.last_outcome_code == outcome
            for task in tasks
        )
    return result
