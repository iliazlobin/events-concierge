"""Read-only Temporal liveness adapter tests for ADR-007."""

from __future__ import annotations

from typing import cast

import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from events_concierge.workflows.start import TemporalWorkflowLivenessInspector


class _Description:
    """Minimal Temporal description shape consumed by the liveness adapter."""

    def __init__(self, status: WorkflowExecutionStatus | None) -> None:
        self.status = status


class _WorkflowHandle:
    """Return a configured execution description or modeled RPC failure."""

    def __init__(self, result: _Description | RPCError) -> None:
        self._result = result

    async def describe(self) -> _Description:
        if isinstance(self._result, RPCError):
            raise self._result
        return self._result


class _Client:
    """Small outer-client seam that records the opaque workflow identity queried."""

    def __init__(self, handle: _WorkflowHandle) -> None:
        self._handle = handle
        self.workflow_ids: list[str] = []

    def get_workflow_handle(self, workflow_id: str) -> _WorkflowHandle:
        self.workflow_ids.append(workflow_id)
        return self._handle


async def test_temporal_liveness_treats_only_running_as_open() -> None:
    """A non-running execution is definitively closed for the read-only ADR-007 check."""
    client = _Client(_WorkflowHandle(_Description(WorkflowExecutionStatus.RUNNING)))

    assert await TemporalWorkflowLivenessInspector(cast(Client, client)).is_open("opaque-workflow")
    assert client.workflow_ids == ["opaque-workflow"]

    closed_client = _Client(_WorkflowHandle(_Description(WorkflowExecutionStatus.COMPLETED)))
    assert not await TemporalWorkflowLivenessInspector(cast(Client, closed_client)).is_open(
        "closed-workflow"
    )


async def test_temporal_liveness_treats_not_found_as_authoritatively_closed() -> None:
    """Temporal's absent execution response is the one non-running lookup result safe to classify."""
    client = _Client(
        _WorkflowHandle(RPCError("workflow execution not found", RPCStatusCode.NOT_FOUND, b""))
    )

    assert not await TemporalWorkflowLivenessInspector(cast(Client, client)).is_open(
        "gone-workflow"
    )


async def test_temporal_liveness_does_not_classify_an_unknown_status_as_closed() -> None:
    """An incomplete engine response is uncertainty, never authorization for a repair path."""

    with pytest.raises(RuntimeError, match="omitted an execution status"):
        await TemporalWorkflowLivenessInspector(
            cast(Client, _Client(_WorkflowHandle(_Description(None))))
        ).is_open("missing-status-workflow")
