"""Temporal implementation of the request-start boundary used by ADR-003's start-outbox."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

from temporalio.api.common.v1 import WorkflowExecution as ProtoWorkflowExecution
from temporalio.api.workflowservice.v1 import DeleteWorkflowExecutionRequest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from ..config import Settings
from ..domain.ids import (
    catalog_paged_refresh_workflow_id,
    catalog_refresh_workflow_id,
    registration_workflow_id,
    request_workflow_id,
)
from ..infra.logging import get_logger
from ..ports.calendar_repair import ClosedWorkflowSignalError
from ..ports.change_detection import OrganizerChangeDelivery
from ..ports.workflows import (
    CatalogPagedRefreshWorkflowStarter,
    CatalogRefreshWorkflowStarter,
    WorkflowLivenessInspector,
)
from .dto import (
    CatalogRefreshInput,
    HandoffCompletionSignal,
    OrganizerChangeSignal,
    RequestInput,
    UnrsvpSignal,
)
from .workflows import CatalogPagedRefreshWorkflow, CatalogRefreshWorkflow, EventRequestWorkflow

_log = get_logger("worker_start")
_MAX_ERASURE_HISTORY_RUNS = 100


class TemporalRequestWorkflowStarter:
    """Ensure a deterministic parent workflow exists; duplicate rejection is a successful replay.

    The caller owns the durable start-outbox lease and retry schedule.  This adapter deliberately
    lets every other engine failure propagate so it is persisted for replay instead of being only
    logged by an HTTP request (FR-6.8, AC-48, ADR-003).
    """

    def __init__(self, client: Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._rpc_timeout = _rpc_timeout(settings)

    async def start(self, tenant_id: UUID, request_id: UUID) -> None:
        """Start an opaque parent identity; activity code re-reads request text under RLS (ADR-011)."""
        workflow_id = request_workflow_id(tenant_id, request_id)
        try:
            await self._client.start_workflow(
                EventRequestWorkflow.run,
                RequestInput(
                    tenant_id=str(tenant_id),
                    request_id=str(request_id),
                    attempt_budget=self._settings.attempt_budget,
                ),
                id=workflow_id,
                task_queue=self._settings.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                rpc_timeout=self._rpc_timeout,
            )
        except WorkflowAlreadyStartedError:
            _log.info("request workflow already started", workflow_id=workflow_id)

    async def cancel(self, tenant_id: UUID, request_id: UUID) -> None:
        """Close, delete, and verify every run of a parent that lost the erasure postcheck race."""
        workflow_id = request_workflow_id(tenant_id, request_id)
        async with asyncio.timeout(max(10.0, self._rpc_timeout.total_seconds() * 6)):
            for _ in range(_MAX_ERASURE_HISTORY_RUNS):
                handle = self._client.get_workflow_handle(workflow_id)
                try:
                    description = await handle.describe(rpc_timeout=self._rpc_timeout)
                except RPCError as error:
                    if error.status is RPCStatusCode.NOT_FOUND:
                        return
                    raise
                if description.status is WorkflowExecutionStatus.RUNNING:
                    await handle.cancel(rpc_timeout=self._rpc_timeout)
                    while True:
                        description = await handle.describe(rpc_timeout=self._rpc_timeout)
                        if description.status is not WorkflowExecutionStatus.RUNNING:
                            break
                        await asyncio.sleep(0.1)
                await self._delete_erasure_history(workflow_id, description.run_id)
            raise RuntimeError("request workflow erasure history run limit exceeded")

    async def _delete_erasure_history(self, workflow_id: str, run_id: str) -> None:
        """Request deletion of one exact run and verify it is no longer addressable."""
        try:
            await self._client.workflow_service.delete_workflow_execution(
                DeleteWorkflowExecutionRequest(
                    namespace=self._client.namespace,
                    workflow_execution=ProtoWorkflowExecution(
                        workflow_id=workflow_id,
                        run_id=run_id,
                    ),
                ),
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


class TemporalCatalogRefreshStarter(CatalogRefreshWorkflowStarter):
    """Start P15a's deterministic one-GET refresh continuation (NFR-8, ADR-003/005).

    ``ALLOW_DUPLICATE_FAILED_ONLY`` retains the existing catalog ledger's retry posture for an
    unexpected activity failure, while an active Pacer timer remains reject-duplicate and therefore
    cannot be replaced by a second dispatcher.  The workflow itself rejects every multi-GET mode.
    """

    def __init__(self, client: Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._rpc_timeout = _rpc_timeout(settings)

    async def start(self, source_key: str, run_key: str) -> None:
        """Ensure one source/run continuation is open only with shared Pacer state (ADR-005)."""
        if not self._settings.uses_shared_pacer_redis:
            raise ValueError("P15a catalog refresh requires a shared Redis Pacer")
        workflow_id = catalog_refresh_workflow_id(source_key, run_key)
        try:
            await self._client.start_workflow(
                CatalogRefreshWorkflow.run,
                CatalogRefreshInput(source_key=source_key, run_key=run_key),
                id=workflow_id,
                task_queue=self._settings.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                rpc_timeout=self._rpc_timeout,
            )
        except WorkflowAlreadyStartedError:
            _log.info("catalog refresh workflow already started", workflow_id=workflow_id)


class TemporalCatalogPagedRefreshStarter(CatalogPagedRefreshWorkflowStarter):
    """Start P15b/P15c/P15d/P15e's deterministic per-page Legistar continuation (ADR-003/005)."""

    def __init__(self, client: Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings
        self._rpc_timeout = _rpc_timeout(settings)

    async def start(self, source_key: str, run_key: str) -> None:
        """Ensure one paged source/run workflow exists only with shared Pacer state (NFR-8)."""
        if not self._settings.uses_shared_pacer_redis:
            raise ValueError("paged catalog refresh requires a shared Redis Pacer")
        workflow_id = catalog_paged_refresh_workflow_id(source_key, run_key)
        try:
            await self._client.start_workflow(
                CatalogPagedRefreshWorkflow.run,
                CatalogRefreshInput(source_key=source_key, run_key=run_key),
                id=workflow_id,
                task_queue=self._settings.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE_FAILED_ONLY,
                rpc_timeout=self._rpc_timeout,
            )
        except WorkflowAlreadyStartedError:
            _log.info("catalog paged refresh workflow already started", workflow_id=workflow_id)


class TemporalRegistrationLifecycleSignaler:
    """Temporal adapter for the narrow post-booking signal port (FR-6.7, FR-8.8)."""

    def __init__(self, client: Client, settings: Settings) -> None:
        self._client = client
        self._rpc_timeout = _rpc_timeout(settings)

    async def signal_unrsvp(self, workflow_id: str, request_id: str) -> None:
        """Append a durable JSON-native command to the real attempt-suffixed child workflow."""
        handle = self._client.get_workflow_handle(workflow_id)
        await handle.signal(
            "unrsvp_requested",
            UnrsvpSignal(request_id=request_id),
            rpc_timeout=self._rpc_timeout,
        )

    async def signal_handoff_completed(
        self,
        workflow_id: str,
        task_id: str,
        completion_id: str,
    ) -> None:
        """Append one opaque, replay-deduplicable completion command to the retained child."""
        handle = self._client.get_workflow_handle(workflow_id)
        await handle.signal(
            "handoff_completed",
            HandoffCompletionSignal(
                task_id=task_id,
                completion_id=completion_id,
            ),
            rpc_timeout=self._rpc_timeout,
        )


class TemporalWorkflowLivenessInspector(WorkflowLivenessInspector):
    """Read the authoritative Temporal execution state for the ADR-007 orphan repair guard."""

    def __init__(self, client: Client, settings: Settings) -> None:
        self._client = client
        self._rpc_timeout = _rpc_timeout(settings)

    async def is_open(self, workflow_id: str) -> bool:
        """Return false only for a closed/not-found execution; transport uncertainty must retry."""
        handle = self._client.get_workflow_handle(workflow_id)
        try:
            description = await handle.describe(rpc_timeout=self._rpc_timeout)
        except RPCError as error:
            if error.status is RPCStatusCode.NOT_FOUND:
                return False
            raise
        if description.status is None:
            raise RuntimeError("Temporal workflow liveness response omitted an execution status")
        return description.status is WorkflowExecutionStatus.RUNNING


class TemporalOrganizerChangeFanout:
    """Outer-layer ADR-008 adapter that signals one opaque leased delivery to Temporal.

    The change ledger owns deduplication/retry and the child workflow owns idempotent reconciliation.
    This adapter deliberately carries only normalized public event fields plus opaque tenant/workflow
    identifiers; it neither polls a source nor reads a tenant's lifecycle (FR-8.7a, ADR-008).
    """

    def __init__(self, client: Client, settings: Settings) -> None:
        self._client = client
        self._rpc_timeout = _rpc_timeout(settings)

    async def signal_organizer_change(self, delivery: OrganizerChangeDelivery) -> None:
        """Append the fingerprint-keyed command to the target lifecycle workflow."""
        expected_workflow_id = registration_workflow_id(
            delivery.tenant_id, delivery.change.canonical_event_id
        )
        if delivery.workflow_id != expected_workflow_id:
            # ``event_change_deliveries`` is an opaque global queue.  Refuse a malformed row at
            # the last side-effect boundary so it can never signal a guessed cross-tenant workflow
            # id; normal lifecycle creation always uses this deterministic identity (ADR-003/008).
            _log.warning(
                "invalid organizer-change delivery workflow identity",
                workflow_id=delivery.workflow_id,
                expected_workflow_id=expected_workflow_id,
                tenant_id=str(delivery.tenant_id),
                canonical_event_id=str(delivery.change.canonical_event_id),
            )
            raise ClosedWorkflowSignalError(
                "organizer-change delivery violates deterministic workflow identity"
            )
        change = delivery.change
        handle = self._client.get_workflow_handle(delivery.workflow_id)
        try:
            await handle.signal(
                "organizer_change",
                OrganizerChangeSignal(
                    fingerprint=change.fingerprint,
                    canonical_event_id=str(change.canonical_event_id),
                    source=change.source.value,
                    event_status=change.event_status.value,
                    start_at=change.start_at.isoformat() if change.start_at is not None else None,
                    end_at=change.end_at.isoformat() if change.end_at is not None else None,
                    time_zone=change.time_zone,
                    title=change.title,
                    venue_name=change.venue_name,
                ),
                rpc_timeout=self._rpc_timeout,
            )
        except RPCError as error:
            if error.status is RPCStatusCode.NOT_FOUND:
                raise ClosedWorkflowSignalError("Temporal target workflow is closed") from error
            raise


def _rpc_timeout(settings: Settings) -> timedelta:
    """Translate the validated deployment setting at the Temporal adapter boundary."""
    return timedelta(seconds=settings.temporal_rpc_timeout_seconds)
