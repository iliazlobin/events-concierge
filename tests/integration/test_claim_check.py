"""Temporal-engine claim-check coverage for ADR-003's oversized payload boundary."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from events_concierge.adapters.mock.object_store import MockFilesystemObjectStore
from events_concierge.domain.ids import request_workflow_id
from events_concierge.workflows.claim_check import build_claim_check_data_converter

pytestmark = pytest.mark.integration


@workflow.defn
class ClaimCheckEchoWorkflow:
    """Minimal durable target proving an engine run can decode an externalized input."""

    @workflow.run
    async def run(self, value: str) -> int:
        return len(value)


async def test_temporal_workflow_round_trips_more_than_two_mebibytes_via_claim_check(
    tmp_path: Path,
) -> None:
    """A >2 MiB start input remains outside Temporal history and reaches its workflow (AC-58)."""
    tenant_id, request_id = uuid4(), uuid4()
    workflow_id = request_workflow_id(tenant_id, request_id)
    large_value = "large-claim-check:" + "x" * (2 * 1024 * 1024)
    converter = build_claim_check_data_converter(
        MockFilesystemObjectStore(tmp_path / "claim-check"),
        threshold_bytes=256 * 1024,
    )

    async with (
        await WorkflowEnvironment.start_time_skipping(data_converter=converter) as environment,
        Worker(
            environment.client,
            task_queue="claim-check-test",
            workflows=[ClaimCheckEchoWorkflow],
            # Pytest's importlib mode does not expose the test module to Temporal's sandbox;
            # the production claim-check converter itself is exercised unchanged.
            workflow_runner=UnsandboxedWorkflowRunner(),
        ),
    ):
        handle = await environment.client.start_workflow(
            ClaimCheckEchoWorkflow.run,
            large_value,
            id=workflow_id,
            task_queue="claim-check-test",
        )
        result = await handle.result()
        history = await handle.fetch_history()

    history_bytes = b"".join(event.SerializeToString() for event in history.events)
    assert result == len(large_value)
    assert large_value.encode() not in history_bytes
    assert len(history_bytes) < 256 * 1024
