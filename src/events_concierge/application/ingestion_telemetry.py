"""Closed, payload-free observations of actual command execution boundaries.

Only the executor binds this context around a claimed source task. An API or workflow dispatcher
must not bind its own build as evidence of execution in another process. Context does not cross
Temporal automatically; activity identities require an explicit, verified activity-side binding.
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID, uuid4

import httpx

from ..infra.logging import get_logger
from ..ports.sources import SourceAccessDeniedError, SourceRateLimitedError, SourceTransientError

_log = get_logger("ingestion.telemetry")
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:/-]{1,512}\Z")
_MAX_COUNTER = 9_223_372_036_854_775_807
_DELIVERY_TIMEOUT_SECONDS = 1.0
_MAX_ERROR_CAUSES = 5
_PROGRESS_INTERVAL_SECONDS = 5.0


class IngestionEventCode(StrEnum):
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    PROGRESS = "progress"
    ERROR = "error"


class IngestionStage(StrEnum):
    ADMISSION = "admission"
    COLLECT = "collect"
    EXTRACT_ENRICH = "extract_enrich"
    NORMALIZE_DEDUPE = "normalize_dedupe"
    CATALOG_PUBLISH = "catalog_publish"


class IngestionOutcome(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEFERRED = "deferred"
    SKIPPED = "skipped"
    BUSY = "busy"
    ALREADY_SUCCEEDED = "already_succeeded"
    PROGRESSED = "progressed"
    QUEUED = "queued"


class IngestionErrorType(StrEnum):
    TIMEOUT = "timeout"
    NETWORK = "network"
    RATE_LIMITED = "rate_limited"
    ACCESS_DENIED = "access_denied"
    VALIDATION = "validation"
    INTERNAL = "internal"


@dataclass(frozen=True, slots=True)
class IngestionTelemetryContext:
    """Actual claimed task and executing process identity; no lease tokens or provider payloads."""

    command_id: UUID
    command_attempt: int
    source_key: str
    run_key: str
    task_attempt: int
    worker_id: str
    release_revision: str
    image_digest: str | None = None
    temporal_workflow_id: str | None = None
    temporal_run_id: str | None = None
    activity_id: str | None = None
    activity_attempt: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.command_id, UUID):
            raise ValueError("telemetry command_id must be a UUID")
        for value in (self.command_attempt, self.task_attempt, self.activity_attempt):
            if value is not None and (type(value) is not int or not 1 <= value <= _MAX_COUNTER):
                raise ValueError("telemetry attempts must be positive bounded integers")
        for identifier in (
            self.source_key,
            self.run_key,
            self.worker_id,
            self.release_revision,
            self.image_digest,
            self.temporal_workflow_id,
            self.temporal_run_id,
            self.activity_id,
        ):
            if identifier is not None and not _IDENTIFIER.fullmatch(identifier):
                raise ValueError("telemetry identity must be bounded opaque identifier text")


@dataclass(frozen=True, slots=True)
class IngestionTelemetryEvent:
    """One bounded observation; UUID deduplicates delivery, not chronological ordering."""

    context: IngestionTelemetryContext
    event_code: IngestionEventCode
    event_id: UUID
    observed_at: datetime
    stage: IngestionStage | None = None
    outcome_code: IngestionOutcome | None = None
    duration_ms: int | None = None
    candidate_count: int | None = None
    canonical_count: int | None = None
    request_count: int | None = None
    page_count: int | None = None
    error_type: IngestionErrorType | None = None

    def __post_init__(self) -> None:
        # Validate at the public value-object boundary as well as the convenience emitter.
        IngestionEventCode(self.event_code)
        if self.stage is not None:
            IngestionStage(self.stage)
        if self.outcome_code is not None:
            IngestionOutcome(self.outcome_code)
        if self.error_type is not None:
            IngestionErrorType(self.error_type)
        for value in (
            self.duration_ms,
            self.candidate_count,
            self.canonical_count,
            self.request_count,
            self.page_count,
        ):
            if value is not None and (type(value) is not int or not 0 <= value <= _MAX_COUNTER):
                raise ValueError("telemetry counters must be non-negative bounded integers")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("telemetry observation time must be timezone-aware")


IngestionEventEmitter = Callable[[IngestionTelemetryEvent], Awaitable[object]]


@dataclass(slots=True)
class _CollectionProgress:
    started_at: float
    request_count: int = 0
    page_count: int = 0
    candidate_count: int | None = None
    last_emitted_at: float | None = None
    dirty: bool = False


@dataclass(slots=True)
class _BoundTelemetry:
    context: IngestionTelemetryContext
    emit: IngestionEventEmitter | None
    delivery_failed: bool = False
    collection: _CollectionProgress | None = None


_bound: ContextVar[_BoundTelemetry | None] = ContextVar("ingestion_telemetry", default=None)


@contextmanager
def bind_ingestion_telemetry(
    context: IngestionTelemetryContext, *, emit: IngestionEventEmitter | None = None
) -> Iterator[None]:
    """Scope source-task observations and best-effort durable delivery to the current async task."""
    token = _bound.set(_BoundTelemetry(context, emit))
    try:
        yield
    finally:
        _bound.reset(token)


def get_ingestion_telemetry_context() -> IngestionTelemetryContext | None:
    bound = _bound.get()
    return bound.context if bound is not None else None


async def emit_ingestion_event(
    event_code: IngestionEventCode | str,
    *,
    stage: IngestionStage | str | None = None,
    outcome_code: IngestionOutcome | str | None = None,
    duration_ms: int | None = None,
    candidate_count: int | None = None,
    canonical_count: int | None = None,
    request_count: int | None = None,
    page_count: int | None = None,
    error_type: IngestionErrorType | str | None = None,
    source_key: str | None = None,
    run_key: str | None = None,
) -> IngestionTelemetryEvent | None:
    """Log and deliver the same closed event, never arbitrary text, URLs, payloads or exceptions.

    Optional source/run arguments are identity guards, not overrides. An unrelated helper cannot
    attribute its work to an enclosing command. Sink failures open a circuit for this binding;
    subsequent observations still log without repeatedly delaying provider work. Cancellation
    propagates normally and no background delivery tasks survive the binding.
    """
    bound = _bound.get()
    if bound is None:
        return None
    context = bound.context
    if (source_key is not None and source_key != context.source_key) or (
        run_key is not None and run_key != context.run_key
    ):
        return None
    event = IngestionTelemetryEvent(
        context=context,
        event_code=IngestionEventCode(event_code),
        event_id=uuid4(),
        observed_at=datetime.now(UTC),
        stage=IngestionStage(stage) if stage is not None else None,
        outcome_code=IngestionOutcome(outcome_code) if outcome_code is not None else None,
        duration_ms=duration_ms,
        candidate_count=candidate_count,
        canonical_count=canonical_count,
        request_count=request_count,
        page_count=page_count,
        error_type=IngestionErrorType(error_type) if error_type is not None else None,
    )
    fields = asdict(event)
    fields.pop("context")
    fields.update(asdict(context))
    fields["command_id"] = str(context.command_id)
    fields["event_id"] = str(event.event_id)
    fields["observed_at"] = event.observed_at.isoformat()
    # Logging availability is not authority to fail a lease-fenced catalog operation.
    with suppress(Exception):
        _log.info("ingestion_operational_event", schema_version=1, **fields)
    if bound.emit is not None and not bound.delivery_failed:
        try:
            async with asyncio.timeout(_DELIVERY_TIMEOUT_SECONDS):
                await bound.emit(event)
        except Exception:
            bound.delivery_failed = True
    return event


def start_ingestion_collection_progress(*, source_key: str, run_key: str) -> None:
    """Reset counters only for the actual matching collection stage of a bound source task."""
    bound = _bound.get()
    if bound is not None and (source_key, run_key) == (
        bound.context.source_key,
        bound.context.run_key,
    ):
        bound.collection = _CollectionProgress(started_at=time.monotonic())


async def record_ingestion_collection_progress(
    *,
    source_key: str,
    run_key: str | None = None,
    request_completed: bool = False,
    page_completed: bool = False,
    candidate_count: int | None = None,
    force: bool = False,
) -> None:
    """Count completed approved HTTP reads/pages, emitting at most once per five seconds.

    Adapter callers invoke this only after their existing response approval and validation. No
    response, URL, cursor, event identity or payload is accepted. Counts are cumulative within one
    actual collection stage, not claims about requests attempted or provider-side processing.
    The stage's final flush bypasses the throttle so its final counts are retained.
    """
    bound = _bound.get()
    if (
        bound is None
        or bound.context.source_key != source_key
        or bound.collection is None
        or (run_key is not None and bound.context.run_key != run_key)
    ):
        return
    progress = bound.collection
    progress.request_count += int(request_completed)
    progress.page_count += int(page_completed)
    progress.dirty = progress.dirty or request_completed or page_completed
    if candidate_count is not None:
        progress.dirty = progress.dirty or candidate_count != progress.candidate_count
        progress.candidate_count = candidate_count
    if not progress.dirty:
        return
    now = time.monotonic()
    if (
        not force
        and progress.last_emitted_at is not None
        and now - progress.last_emitted_at < _PROGRESS_INTERVAL_SECONDS
    ):
        return
    progress.last_emitted_at = now
    progress.dirty = False
    await emit_ingestion_event(
        "progress",
        stage="collect",
        outcome_code="progressed",
        duration_ms=max(0, int((now - progress.started_at) * 1_000)),
        candidate_count=progress.candidate_count,
        # Uninstrumented adapters must not claim to have performed zero requests.
        request_count=progress.request_count if progress.request_count else None,
        page_count=progress.page_count if progress.page_count else None,
        source_key=source_key,
    )


def ingestion_error_type(error: BaseException) -> IngestionErrorType:
    """Classify a bounded cause chain without reading exception messages or provider responses."""
    current = error
    for _ in range(_MAX_ERROR_CAUSES):
        if isinstance(current, (TimeoutError, httpx.TimeoutException)):
            return IngestionErrorType.TIMEOUT
        if isinstance(current, SourceRateLimitedError):
            return IngestionErrorType.RATE_LIMITED
        if isinstance(current, SourceAccessDeniedError):
            return IngestionErrorType.ACCESS_DENIED
        if isinstance(current, (ConnectionError, httpx.TransportError, SourceTransientError)):
            return IngestionErrorType.NETWORK
        if current.__cause__ is None:
            break
        current = current.__cause__
    return (
        IngestionErrorType.VALIDATION
        if isinstance(current, (ValueError, TypeError))
        else IngestionErrorType.INTERNAL
    )
