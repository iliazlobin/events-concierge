"""Execute in the private API pod. Creates only synthetic development fixtures."""

import argparse
import asyncio
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import httpx
from temporalio import workflow
from temporalio.client import WorkflowExecutionStatus
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from events_concierge.config import get_settings
from events_concierge.deployment.development_runtime import build_runtime_ports
from events_concierge.domain.ids import request_workflow_id
from events_concierge.infra.db import dispose_engine, init_engine
from events_concierge.workflows.temporal_client import connect_temporal


@workflow.defn
class DevelopmentClaimCheckEcho:
    @workflow.run
    async def run(self, value: str) -> int:
        return len(value)


async def main():
    s = get_settings()
    if s.env != "development" or not s.mock_cloud:
        raise SystemExit("Synthetic development profile required")
    init_engine(s.database_url, pool_size=2, max_overflow=0)
    ports = build_runtime_ports(s)
    store = ports.object_store
    fixture = uuid.uuid4()
    payload = os.urandom(512 * 1024)
    await store.put(fixture, "deployment-smoke", payload)
    assert await store.get(fixture, "deployment-smoke") == payload
    await store.delete_tenant(fixture)
    print("Workload Identity / GCS round-trip passed")
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000", timeout=30) as http:
        for path in ["/healthz", "/readyz"]:
            r = await http.get(path)
            r.raise_for_status()
        onboard = await http.post(
            "/v1/onboard", json={"notify_email": "gke-smoke-" + fixture.hex + "@example.invalid"}
        )
        onboard.raise_for_status()
        tenant = onboard.json()["tenant_id"]
        headers = {"X-EC-Tenant-ID": tenant}
        r = await http.post(
            "/v1/requests",
            json={"text": "development infrastructure smoke " + fixture.hex},
            headers=headers,
        )
        r.raise_for_status()
        request = r.json()["request_id"]
        again = await http.post(
            "/v1/requests",
            json={"text": "development infrastructure smoke " + fixture.hex},
            headers=headers,
        )
        again.raise_for_status()
        assert again.json()["request_id"] == request
        client = await connect_temporal(s, store)
        handle = client.get_workflow_handle(
            request_workflow_id(uuid.UUID(tenant), uuid.UUID(request))
        )
        result = await asyncio.wait_for(handle.result(), timeout=180)
        description = await handle.describe()
        assert description.status == WorkflowExecutionStatus.COMPLETED
        history = await handle.fetch_history()
        assert len(history.events) > 5
        print(
            json.dumps(
                {
                    "tenant_id": tenant,
                    "request_id": request,
                    "workflow_id": handle.id,
                    "workflow_status": "COMPLETED",
                    "history_events": len(history.events),
                    "result": result,
                },
                default=str,
            )
        )
        feed = await http.post("/v1/feed", json={"text": "jazz"}, headers=headers)
        feed.raise_for_status()
        print("API, request replay, real Temporal worker completion and feed passed")
        large = "development-claim-check:" + ("x" * (2 * 1024 * 1024))
        with ThreadPoolExecutor(max_workers=2) as executor:
            async with Worker(
                client,
                task_queue="development-claim-check-smoke",
                workflows=[DevelopmentClaimCheckEcho],
                workflow_runner=UnsandboxedWorkflowRunner(),
                workflow_task_executor=executor,
                max_concurrent_workflow_tasks=2,
                max_concurrent_activities=2,
            ):
                claim_handle = await client.start_workflow(
                    DevelopmentClaimCheckEcho.run,
                    large,
                    id=request_workflow_id(uuid.UUID(tenant), uuid.uuid4()),
                    task_queue="development-claim-check-smoke",
                )
                assert await asyncio.wait_for(claim_handle.result(), 90) == len(large)
                history = await claim_handle.fetch_history()
                encoded = b"".join(e.SerializeToString() for e in history.events)
                assert large.encode() not in encoded and len(encoded) < 256 * 1024
        print("Real Temporal / GCS claim-check >2 MiB round-trip passed:", claim_handle.id)

    await dispose_engine()


async def repeated():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=1, choices=range(1, 21))
    args = parser.parse_args()
    for index in range(args.repeat):
        await main()
        if index + 1 < args.repeat:
            await asyncio.sleep(10)


asyncio.run(repeated())
