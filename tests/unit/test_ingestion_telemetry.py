"""Safe correlated event delivery, truthful stage boundaries and bounded provider progress."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from uuid import uuid4

import httpx
import pytest
from structlog.testing import CapturingLogger

from events_concierge.application import ingestion_telemetry as telemetry
from events_concierge.application.catalog_run_evidence import CatalogRunEvidenceSession
from events_concierge.application.ingestion_telemetry import (
    IngestionTelemetryContext,
    IngestionTelemetryEvent,
    bind_ingestion_telemetry,
    emit_ingestion_event,
    get_ingestion_telemetry_context,
    ingestion_error_type,
    record_ingestion_collection_progress,
)


@pytest.fixture(autouse=True)
def telemetry_logger(monkeypatch: pytest.MonkeyPatch) -> CapturingLogger:
    """Inspect the emission boundary independently of process-global cached logging config."""
    logger = CapturingLogger()
    monkeypatch.setattr(telemetry, "_log", logger)
    return logger


def _context(source_key: str = "test-events") -> IngestionTelemetryContext:
    command_id = uuid4()
    return IngestionTelemetryContext(
        command_id=command_id,
        command_attempt=2,
        source_key=source_key,
        run_key=f"admin:{command_id}",
        task_attempt=3,
        worker_id="ingestion-command-worker-abc",
        release_revision="abc123",
        image_digest="sha256:0123456789",
    )


async def test_stage_started_is_available_before_work_and_finish_has_safe_counts(
    telemetry_logger: CapturingLogger,
) -> None:
    context = _context()
    events: list[IngestionTelemetryEvent] = []

    async def emit(event: IngestionTelemetryEvent) -> None:
        events.append(event)

    with bind_ingestion_telemetry(context, emit=emit):
        session = CatalogRunEvidenceSession(
            None,
            context.source_key,
            context.run_key,
            include_process_metrics=True,
        )
        collect = await session.start_stage("collect")
        assert [(event.event_code, event.stage) for event in events] == [
            ("stage_started", "collect"),
        ]
        await collect.progress(candidate_count=7)
        await collect.finish("succeeded")
        publish = await session.start_stage("catalog_publish")
        await publish.finish("succeeded", candidate_count=7, canonical_count=5)
        await publish.finish("succeeded")  # Failure handling cannot duplicate a finished stage.
        await session.finish("succeeded")

    logs = [call.kwargs for call in telemetry_logger.calls]
    assert len(events) == 5
    assert events[1].candidate_count == 7
    assert events[1].request_count is None  # Unknown request coverage is never reported as zero.
    assert events[-1].canonical_count == 5
    assert all(event.context == context for event in events)
    assert all(event.context.temporal_run_id is None for event in events)
    assert all(log["command_id"] == str(context.command_id) for log in logs)
    assert [log["event_id"] for log in logs] == [str(event.event_id) for event in events]
    assert get_ingestion_telemetry_context() is None


async def test_error_observation_never_contains_exception_text_or_request_payload(
    telemetry_logger: CapturingLogger,
) -> None:
    context = _context()
    events: list[IngestionTelemetryEvent] = []

    async def emit(event: IngestionTelemetryEvent) -> None:
        events.append(event)

    with bind_ingestion_telemetry(context, emit=emit):
        session = CatalogRunEvidenceSession(
            None,
            context.source_key,
            context.run_key,
            include_process_metrics=False,
        )
        await session.start_stage("admission")
        await session.finish("failed", error=ValueError("private payload password=not-for-logs"))

    assert [(event.event_code, event.error_type) for event in events] == [
        ("stage_started", None),
        ("error", "validation"),
        ("stage_completed", None),
    ]
    assert "not-for-logs" not in repr(telemetry_logger.calls)
    assert "private payload" not in repr([asdict(event) for event in events])


async def test_context_isolated_between_concurrent_tasks_and_unrelated_source_is_ignored() -> None:
    seen: list[IngestionTelemetryEvent] = []

    async def emit(event: IngestionTelemetryEvent) -> None:
        seen.append(event)

    async def work(source_key: str) -> None:
        context = _context(source_key)
        with bind_ingestion_telemetry(context, emit=emit):
            await asyncio.sleep(0)
            await emit_ingestion_event("stage_started", stage="collect", source_key=source_key)
            await emit_ingestion_event("progress", source_key="unrelated-events")
            await emit_ingestion_event("progress", run_key="unrelated-run")
            assert get_ingestion_telemetry_context() == context

    await asyncio.gather(work("first-events"), work("second-events"))
    assert {event.context.source_key for event in seen} == {"first-events", "second-events"}
    assert len(seen) == 2
    assert await emit_ingestion_event("progress") is None


async def test_sink_timeout_opens_circuit_without_failing_work_or_retrying_every_event(
    monkeypatch: pytest.MonkeyPatch,
    telemetry_logger: CapturingLogger,
) -> None:
    monkeypatch.setattr(telemetry, "_DELIVERY_TIMEOUT_SECONDS", 0.01)
    calls = 0
    cancelled = asyncio.Event()

    async def sink(event: IngestionTelemetryEvent) -> None:
        nonlocal calls
        calls += 1
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with bind_ingestion_telemetry(_context(), emit=sink):
        await emit_ingestion_event("progress", candidate_count=1)
        await emit_ingestion_event("progress", candidate_count=2)
    assert calls == 1
    assert cancelled.is_set()
    assert len(telemetry_logger.calls) == 2


async def test_cancellation_is_not_swallowed_by_best_effort_delivery() -> None:
    async def cancelled_sink(event: IngestionTelemetryEvent) -> None:
        raise asyncio.CancelledError

    with (
        bind_ingestion_telemetry(_context(), emit=cancelled_sink),
        pytest.raises(asyncio.CancelledError),
    ):
        await emit_ingestion_event("progress")
    assert get_ingestion_telemetry_context() is None


async def test_progress_throttles_five_seconds_but_final_flush_preserves_last_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    monkeypatch.setattr(telemetry.time, "monotonic", lambda: now)
    events: list[IngestionTelemetryEvent] = []

    async def emit(event: IngestionTelemetryEvent) -> None:
        events.append(event)

    context = _context()
    with bind_ingestion_telemetry(context, emit=emit):
        session = CatalogRunEvidenceSession(
            None,
            context.source_key,
            context.run_key,
            include_process_metrics=True,
        )
        collect = await session.start_stage("collect")
        for observed_time in (100.0, 101.0, 105.0, 106.0):
            now = observed_time
            await record_ingestion_collection_progress(
                source_key=context.source_key,
                request_completed=True,
                page_completed=True,
            )
        progress = [event for event in events if event.event_code == "progress"]
        assert [event.request_count for event in progress] == [1, 3]
        await collect.progress(candidate_count=40)
        await collect.finish("succeeded")
    progress = [event for event in events if event.event_code == "progress"]
    assert [event.request_count for event in progress] == [1, 3, 4]
    assert progress[-1].page_count == 4
    assert progress[-1].candidate_count == 40
    assert progress[-1].duration_ms == 6_000


async def test_closed_schema_rejects_freeform_fields_and_invalid_counters() -> None:
    with pytest.raises(ValueError):
        replace(_context(), worker_id="password=secret arbitrary text")
    with bind_ingestion_telemetry(_context()):
        for kwargs in (
            {"stage": "provider body"},
            {"error_type": "SecretException"},
            {"candidate_count": -1},
            {"request_count": True},
        ):
            with pytest.raises(ValueError):
                await emit_ingestion_event("progress", **kwargs)  # type: ignore[arg-type]


def test_wrapped_transport_error_maps_to_closed_family_without_message_introspection() -> None:
    inner = httpx.ReadTimeout("password=never-inspected")
    outer = RuntimeError("provider URL and payload")
    outer.__cause__ = inner
    assert ingestion_error_type(outer) == "timeout"
    assert ingestion_error_type(RuntimeError("arbitrary")) == "internal"
