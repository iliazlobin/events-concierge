"""Temporal implementation of the request-start boundary used by ADR-003's start-outbox."""

from __future__ import annotations

from uuid import UUID

from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from ..config import Settings
from ..domain.ids import request_workflow_id
from ..infra.logging import get_logger
from ..workflows.dto import RequestInput
from ..workflows.workflows import EventRequestWorkflow

_log = get_logger("api")


class TemporalRequestWorkflowStarter:
    """Ensure a deterministic parent workflow exists; duplicate rejection is a successful replay.

    The caller owns the durable start-outbox lease and retry schedule.  This adapter deliberately
    lets every other engine failure propagate so it is persisted for replay instead of being only
    logged by an HTTP request (FR-6.8, AC-48, ADR-003).
    """

    def __init__(self, client: Client, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    async def start(self, tenant_id: UUID, request_id: UUID, raw_text: str) -> None:
        """Start the parent or accept Temporal's reject-duplicate response as an existing effect."""
        workflow_id = request_workflow_id(tenant_id, request_id)
        try:
            await self._client.start_workflow(
                EventRequestWorkflow.run,
                RequestInput(
                    tenant_id=str(tenant_id),
                    request_id=str(request_id),
                    raw_text=raw_text,
                    attempt_budget=self._settings.attempt_budget,
                ),
                id=workflow_id,
                task_queue=self._settings.temporal_task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            _log.info("request workflow already started", workflow_id=workflow_id)
