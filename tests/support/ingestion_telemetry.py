"""Capture typed provider progress under the same collection scope used by source workers."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from events_concierge.application.ingestion_telemetry import (
    IngestionTelemetryContext,
    IngestionTelemetryEvent,
    bind_ingestion_telemetry,
    record_ingestion_collection_progress,
    start_ingestion_collection_progress,
)


@asynccontextmanager
async def capture_collection_progress(
    source_key: str,
) -> AsyncIterator[list[IngestionTelemetryEvent]]:
    events: list[IngestionTelemetryEvent] = []
    command_id = uuid4()
    context = IngestionTelemetryContext(
        command_id=command_id, command_attempt=1, source_key=source_key,
        run_key=f"admin:{command_id}", task_attempt=1,
        worker_id="unit-test", release_revision="unit-test",
    )

    async def emit(event: IngestionTelemetryEvent) -> None:
        events.append(event)

    with bind_ingestion_telemetry(context, emit=emit):
        start_ingestion_collection_progress(source_key=source_key, run_key=context.run_key)
        try:
            yield events
        finally:
            await record_ingestion_collection_progress(source_key=source_key, force=True)
