"""Live Temporal compatibility proof for account-erasure closure and history deletion."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from temporalio import workflow
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from events_concierge.workflows.account_erasure import TemporalTenantWorkflowCancellation

pytestmark = pytest.mark.integration


@workflow.defn(name="AccountErasureWaitingFixture", sandboxed=False)
class _WaitingWorkflow:
    @workflow.run
    async def run(self) -> None:
        await workflow.wait_condition(lambda: False)


async def test_temporal_erasure_closes_deletes_and_verifies_parent_and_child_histories() -> None:
    tenant_id = uuid4()
    parent_id = f"req:{tenant_id}:{uuid4()}"
    child_id = f"{tenant_id}:{uuid4()}"
    task_queue = f"account-erasure-history-{uuid4().hex}"
    target = os.environ.get("EC_TEMPORAL_TARGET")
    if target is None:
        pytest.skip("EC_TEMPORAL_TARGET not set; run make test-integration")
    client = await Client.connect(target)

    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[_WaitingWorkflow],
    ):
        parent = await client.start_workflow(
            _WaitingWorkflow.run,
            id=parent_id,
            task_queue=task_queue,
        )
        child = await client.start_workflow(
            _WaitingWorkflow.run,
            id=child_id,
            task_queue=task_queue,
        )
        eraser = TemporalTenantWorkflowCancellation(
            client,
            # The complete integration suite can briefly saturate the local Temporal dev server.
            # Keep this above the production default RPC bound so the history-erasure assertion
            # measures behavior rather than scheduler contention.
            rpc_timeout_seconds=10,
            settle_timeout_seconds=60,
            quiet_scan_seconds=0.01,
        )

        await eraser.quiesce_tenant(tenant_id, (child_id, parent_id))

        for handle in (parent, child):
            with pytest.raises(RPCError) as raised:
                await handle.describe()
            assert raised.value.status is RPCStatusCode.NOT_FOUND
