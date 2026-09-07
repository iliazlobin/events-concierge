"""Adversarial ordering and history-removal tests for Temporal account erasure."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from events_concierge.workflows.account_erasure import TemporalTenantWorkflowCancellation


def _not_found() -> RPCError:
    return RPCError("execution absent", RPCStatusCode.NOT_FOUND, b"")


class _Handle:
    def __init__(self, client: _TemporalClient, workflow_id: str, run_id: str | None) -> None:
        self._client = client
        self._workflow_id = workflow_id
        self._run_id = run_id

    def _target(self) -> _Execution:
        candidates = [
            execution
            for execution in self._client.executions
            if execution.workflow_id == self._workflow_id
            and (self._run_id is None or execution.run_id == self._run_id)
        ]
        if not candidates:
            raise _not_found()
        return candidates[-1]

    async def describe(self, **kwargs: object) -> SimpleNamespace:
        del kwargs
        target = self._target()
        return SimpleNamespace(status=target.status, run_id=target.run_id)

    async def cancel(self, **kwargs: object) -> None:
        del kwargs
        target = self._target()
        self._client.actions.append(("cancel", target.workflow_id, target.run_id))
        target.status = WorkflowExecutionStatus.CANCELED


class _WorkflowService:
    def __init__(self, client: _TemporalClient) -> None:
        self._client = client

    async def delete_workflow_execution(self, request: Any, **kwargs: object) -> object:
        del kwargs
        target = request.workflow_execution
        for execution in tuple(self._client.executions):
            if (
                execution.workflow_id == target.workflow_id
                and execution.run_id == target.run_id
            ):
                self._client.actions.append(("delete", execution.workflow_id, execution.run_id))
                self._client.executions.remove(execution)
                return object()
        raise _not_found()


class _Execution:
    def __init__(
        self,
        workflow_id: str,
        run_id: str,
        status: WorkflowExecutionStatus,
    ) -> None:
        self.workflow_id = workflow_id
        self.id = workflow_id
        self.run_id = run_id
        self.status = status


class _TemporalClient:
    namespace = "default"

    def __init__(self, executions: list[_Execution]) -> None:
        self.executions = executions
        self.actions: list[tuple[str, str, str]] = []
        self.queries: list[str] = []
        self.workflow_service = _WorkflowService(self)

    def get_workflow_handle(self, workflow_id: str, *, run_id: str | None = None) -> _Handle:
        return _Handle(self, workflow_id, run_id)

    async def list_workflows(self, query: str, **kwargs: object) -> AsyncIterator[_Execution]:
        del kwargs
        self.queries.append(query)
        prefix = query.partition('STARTS_WITH "')[2].removesuffix('"')
        for execution in tuple(self.executions):
            if execution.workflow_id.startswith(prefix):
                yield execution


async def test_quiesce_closes_parents_before_children_and_deletes_every_history_run() -> None:
    tenant_id = uuid4()
    parent = f"req:{tenant_id}:{uuid4()}"
    child = f"{tenant_id}:{uuid4()}"
    orphan = f"{tenant_id}:{uuid4()}"
    client = _TemporalClient(
        [
            _Execution(child, "child-run", WorkflowExecutionStatus.RUNNING),
            _Execution(parent, "parent-old", WorkflowExecutionStatus.CONTINUED_AS_NEW),
            _Execution(parent, "parent-current", WorkflowExecutionStatus.RUNNING),
            _Execution(orphan, "orphan-run", WorkflowExecutionStatus.COMPLETED),
        ]
    )
    adapter = TemporalTenantWorkflowCancellation(
        cast("Any", client),
        rpc_timeout_seconds=1,
        settle_timeout_seconds=2,
        quiet_scan_seconds=0.001,
    )

    # Deliberately supply child first. The adapter must still close the parent before the child,
    # then erase all parent runs and visibility-discovered orphan history.
    await adapter.quiesce_tenant(tenant_id, (child, parent))

    cancel_actions = [action for action in client.actions if action[0] == "cancel"]
    assert cancel_actions[:2] == [
        ("cancel", parent, "parent-current"),
        ("cancel", child, "child-run"),
    ]
    assert {action[1:] for action in client.actions if action[0] == "delete"} == {
        (parent, "parent-current"),
        (parent, "parent-old"),
        (child, "child-run"),
        (orphan, "orphan-run"),
    }
    assert client.executions == []
    # One scan discovers/deletes the orphan, followed by two required quiet scans; every scan
    # checks both accepted workflow-ID prefixes.
    assert len(client.queries) == 6
    assert all("ExecutionStatus" not in query for query in client.queries)
