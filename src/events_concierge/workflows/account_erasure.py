"""Temporal cancellation adapter for tenant account erasure."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from temporalio.api.common.v1 import WorkflowExecution as ProtoWorkflowExecution
from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

_REQUIRED_QUIET_SCANS = 2
_MAX_HISTORY_RUNS_PER_WORKFLOW_ID = 100


@dataclass(frozen=True, slots=True)
class _ExecutionTarget:
    workflow_id: str
    run_id: str
    status: WorkflowExecutionStatus | None


class TemporalTenantWorkflowCancellation:
    """Close and erase known plus visibility-discovered tenant workflow histories."""

    def __init__(
        self,
        client: Client,
        *,
        rpc_timeout_seconds: float,
        settle_timeout_seconds: float | None = None,
        quiet_scan_seconds: float = 0.5,
    ) -> None:
        if rpc_timeout_seconds <= 0:
            raise ValueError("Temporal erasure RPC timeout must be positive")
        if settle_timeout_seconds is not None and settle_timeout_seconds <= 0:
            raise ValueError("Temporal erasure settle timeout must be positive")
        if quiet_scan_seconds <= 0:
            raise ValueError("Temporal erasure quiet scan must be positive")
        self._client = client
        self._rpc_timeout = timedelta(seconds=rpc_timeout_seconds)
        self._settle_timeout = settle_timeout_seconds or max(10.0, rpc_timeout_seconds * 6)
        self._quiet_scan_seconds = quiet_scan_seconds

    async def cancel(self, workflow_id: str) -> None:
        if not workflow_id.strip():
            raise ValueError("account erasure workflow id cannot be empty")
        await self._cancel_execution(workflow_id)

    async def _cancel_execution(self, workflow_id: str, run_id: str | None = None) -> None:
        """Close one exact execution, treating an already deleted history as success."""
        handle = self._client.get_workflow_handle(workflow_id, run_id=run_id)
        try:
            description = await handle.describe(rpc_timeout=self._rpc_timeout)
            if description.status is not WorkflowExecutionStatus.RUNNING:
                return
            await handle.cancel(rpc_timeout=self._rpc_timeout)
        except RPCError as error:
            if error.status is RPCStatusCode.NOT_FOUND:
                return
            raise
        while True:
            try:
                description = await handle.describe(rpc_timeout=self._rpc_timeout)
            except RPCError as error:
                if error.status is RPCStatusCode.NOT_FOUND:
                    return
                raise
            if description.status is not WorkflowExecutionStatus.RUNNING:
                return
            await asyncio.sleep(0.1)

    async def quiesce_tenant(
        self,
        tenant_id: UUID,
        known_workflow_ids: tuple[str, ...],
    ) -> None:
        """Close executions, delete every history run, and require two quiet visibility scans.

        The prefix scan captures an ABANDON child started before its first lifecycle commit. The
        request-start DB fence and pre-child durable registry prevent new side effects; two scans
        after every known parent closes settle visibility lag before the stage is acknowledged.
        A successful stage therefore means Temporal's describe API verifies every targeted run is
        absent; namespace retention is no longer mistaken for immediate account erasure.
        """
        async with asyncio.timeout(self._settle_timeout):
            parent_prefix = f"req:{tenant_id}:"
            parents = tuple(
                workflow_id
                for workflow_id in known_workflow_ids
                if workflow_id.startswith(parent_prefix)
            )
            children = tuple(
                workflow_id
                for workflow_id in known_workflow_ids
                if not workflow_id.startswith(parent_prefix)
            )
            # Parents can emit ABANDON-policy children. They must be closed before a known child
            # receives its final cancellation; otherwise a committed registry row followed by a
            # not-yet-started child can race past the erasure scan.
            for workflow_id in parents:
                await self.cancel(workflow_id)
            for workflow_id in children:
                await self.cancel(workflow_id)

            # Delete every run reachable by each deterministic ID. Omitting run_id in describe
            # selects the latest remaining run, so the loop also covers continue-as-new chains.
            for workflow_id in (*parents, *children):
                await self._delete_all_histories(workflow_id)

            quiet_scans = 0
            while quiet_scans < _REQUIRED_QUIET_SCANS:
                executions = await self._tenant_workflow_executions(tenant_id)
                if executions:
                    quiet_scans = 0
                    ordered = sorted(
                        executions,
                        key=lambda target: (
                            not target.workflow_id.startswith(parent_prefix),
                            target.workflow_id,
                            target.run_id,
                        ),
                    )
                    for target in ordered:
                        if target.status is WorkflowExecutionStatus.RUNNING:
                            await self._cancel_execution(target.workflow_id, target.run_id)
                    for target in ordered:
                        await self._delete_history(target.workflow_id, target.run_id)
                else:
                    quiet_scans += 1
                if quiet_scans < _REQUIRED_QUIET_SCANS:
                    await asyncio.sleep(self._quiet_scan_seconds)

    async def _delete_all_histories(self, workflow_id: str) -> None:
        """Delete every run currently addressable by one deterministic workflow ID."""
        for _ in range(_MAX_HISTORY_RUNS_PER_WORKFLOW_ID):
            handle = self._client.get_workflow_handle(workflow_id)
            try:
                description = await handle.describe(rpc_timeout=self._rpc_timeout)
            except RPCError as error:
                if error.status is RPCStatusCode.NOT_FOUND:
                    return
                raise
            if description.status is WorkflowExecutionStatus.RUNNING:
                await self._cancel_execution(workflow_id, description.run_id)
            await self._delete_history(workflow_id, description.run_id)
        raise RuntimeError("account erasure workflow history run limit exceeded")

    async def _delete_history(self, workflow_id: str, run_id: str) -> None:
        """Delete one exact history and verify the execution is no longer addressable."""
        request = DeleteWorkflowExecutionRequest(
            namespace=self._client.namespace,
            workflow_execution=ProtoWorkflowExecution(
                workflow_id=workflow_id,
                run_id=run_id,
            ),
        )
        try:
            await self._client.workflow_service.delete_workflow_execution(
                request,
                timeout=self._rpc_timeout,
            )
        except RPCError as error:
            if error.status is not RPCStatusCode.NOT_FOUND:
                raise
        while True:
            try:
                await self._client.get_workflow_handle(
                    workflow_id, run_id=run_id
                ).describe(rpc_timeout=self._rpc_timeout)
            except RPCError as error:
                if error.status is RPCStatusCode.NOT_FOUND:
                    return
                raise
            await asyncio.sleep(0.1)

    async def _tenant_workflow_executions(
        self, tenant_id: UUID
    ) -> tuple[_ExecutionTarget, ...]:
        """Enumerate running and closed executions because both retain workflow payload history."""
        prefixes = (f"{tenant_id}:", f"req:{tenant_id}:")
        executions: dict[tuple[str, str], _ExecutionTarget] = {}
        for prefix in prefixes:
            query = f'WorkflowId STARTS_WITH "{prefix}"'
            async for execution in self._client.list_workflows(
                query,
                page_size=100,
                rpc_timeout=self._rpc_timeout,
            ):
                if execution.id.startswith(prefix):
                    target = _ExecutionTarget(
                        workflow_id=execution.id,
                        run_id=execution.run_id,
                        status=execution.status,
                    )
                    executions[(target.workflow_id, target.run_id)] = target
        return tuple(executions[key] for key in sorted(executions))


class UnavailableTenantWorkflowCancellation:
    """Fail a non-empty workflow stage while the durable engine boundary is unavailable."""

    async def cancel(self, workflow_id: str) -> None:
        del workflow_id
        raise RuntimeError("workflow engine unavailable for account erasure")

    async def quiesce_tenant(
        self,
        tenant_id: UUID,
        known_workflow_ids: tuple[str, ...],
    ) -> None:
        del tenant_id, known_workflow_ids
        raise RuntimeError("workflow engine unavailable for account erasure")


class NoopTenantSessionRevocation:
    """Local-demo boundary: no server-side browser session store exists to fence or purge."""

    async def revoke_tenant_sessions(self, tenant_id: UUID) -> None:
        del tenant_id


class UnavailableTenantExternalEffectDrain:
    """Fail closed until every tenant-mutating external boundary shares the durable fence."""

    async def drain_tenant_effects(self, tenant_id: UUID) -> None:
        del tenant_id
        raise RuntimeError("tenant external-effect drain inventory is not installed")
