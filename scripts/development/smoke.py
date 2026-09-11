"""Execute in the private API pod. Creates only synthetic development fixtures."""

import argparse
import asyncio
import json
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

import httpx
from temporalio import workflow
from temporalio.client import WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from events_concierge.config import get_settings
from events_concierge.deployment.development_runtime import build_runtime_ports
from events_concierge.domain.ids import request_workflow_id
from events_concierge.infra.db import dispose_engine, init_engine
from events_concierge.workflows.temporal_client import connect_temporal

MIN_REQUEST_HISTORY_EVENTS = 6


@workflow.defn
class DevelopmentClaimCheckEcho:
    @workflow.run
    async def run(self, value: str) -> int:
        return len(value)


async def check_discovery(http, headers):
    # Empty mutation bodies cannot create work even if a deferred route accidentally returns.
    for method, path in (
        ("POST", "/v1/requests"),
        ("POST", "/v1/feed"),
        ("GET", "/v1/me/api-keys"),
    ):
        response = await http.request(method, path, headers=headers)
        if response.status_code != HTTPStatus.NOT_FOUND:
            raise RuntimeError(f"Deferred discovery route is exposed: {method} {path}")
    me = await http.get("/v1/me", headers=headers)
    me.raise_for_status()
    profile = {
        "display_name": "Development smoke fixture",
        "time_zone": "America/Los_Angeles",
        "revision": me.json()["profile"]["revision"] + 1,
    }
    updated = await http.put("/v1/me/profile", json=profile, headers=headers)
    updated.raise_for_status()
    reloaded = await http.get("/v1/me", headers=headers)
    reloaded.raise_for_status()
    assert all(reloaded.json()["profile"][key] == value for key, value in profile.items())
    now = datetime.now(UTC)
    bounds = {
        "starts_after": now.isoformat(),
        "starts_before": (now + timedelta(days=30)).isoformat(),
    }
    events = await http.get("/v1/catalog/events", params={**bounds, "limit": 2}, headers=headers)
    events.raise_for_status()
    page = events.json()
    assert isinstance(page["items"], list)
    if page["next_cursor"]:
        following = await http.get(
            "/v1/catalog/events",
            params={**bounds, "limit": 2, "cursor": page["next_cursor"]},
            headers=headers,
        )
        following.raise_for_status()
        assert isinstance(following.json()["items"], list)
    summary = await http.get(
        "/v1/catalog/events/summary",
        params={**bounds, "time_zone": "America/Los_Angeles"},
        headers=headers,
    )
    summary.raise_for_status()
    count = summary.json()["total_event_count"]
    assert isinstance(count, int) and count >= len(page["items"])
    print("Discovery catalog/profile and deferred 404 checks passed; next 30 days:", count)


async def check_full(http, headers, tenant, fixture, client):
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
    handle = client.get_workflow_handle(request_workflow_id(uuid.UUID(tenant), uuid.UUID(request)))
    # Intake may durably defer the start to RequestStartWorker.
    async with asyncio.timeout(60):
        while True:
            try:
                await handle.describe()
                break
            except RPCError as error:
                if error.status != RPCStatusCode.NOT_FOUND:
                    raise
                await asyncio.sleep(0.5)
    result = await asyncio.wait_for(handle.result(), timeout=180)
    description = await handle.describe()
    assert description.status == WorkflowExecutionStatus.COMPLETED
    history = await handle.fetch_history()
    assert len(history.events) >= MIN_REQUEST_HISTORY_EVENTS
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


async def check_claim_check(client, tenant):
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


async def main():
    s = get_settings()
    if (
        s.env != "development"
        or not s.mock_cloud
        or s.temporal_namespace != "events-development"
        or s.google_calendar_enabled
    ):
        raise SystemExit("Synthetic development profile and events-development namespace required")
    # A loopback client ignores ambient HTTP proxies; config is checked before any fixture write.
    async with httpx.AsyncClient(
        base_url="http://127.0.0.1:8000", timeout=30, trust_env=False
    ) as http:
        for path in ("/healthz", "/readyz"):
            response = await http.get(path)
            response.raise_for_status()
        response = await http.get("/v1/ui-config")
        response.raise_for_status()
        config = response.json()
        if (
            config.get("release_profile") != s.release_profile
            or config.get("local_demo") is not True
            or config.get("auth_mode") != "local_demo"
        ):
            raise SystemExit(
                "API release/auth profile does not match the synthetic development smoke"
            )
        init_engine(s.database_url, pool_size=2, max_overflow=0)
        try:
            store = build_runtime_ports(s).object_store
            fixture = uuid.uuid4()
            payload = os.urandom(512 * 1024)
            try:
                await store.put(fixture, "deployment-smoke", payload)
                assert await store.get(fixture, "deployment-smoke") == payload
            finally:
                await store.delete_tenant(fixture)
            print("Workload Identity / GCS round-trip passed")
            onboard = await http.post(
                "/v1/onboard",
                json={"notify_email": "gke-smoke-" + fixture.hex + "@example.invalid"},
            )
            onboard.raise_for_status()
            tenant = str(uuid.UUID(onboard.json()["tenant_id"]))
            headers = {"X-EC-Tenant-ID": tenant}
            print("Synthetic development tenant:", tenant)
            try:
                client = await connect_temporal(s, store)
                if s.release_profile == "discovery":
                    await check_discovery(http, headers)
                else:
                    await check_full(http, headers, tenant, fixture, client)
                await check_claim_check(client, tenant)
            finally:
                # Only the account just onboarded is fenced. The leased worker owns convergence;
                # an accepted cleanup request is not proof that erasure has completed.
                erased = await http.post(
                    "/v1/me/erasure-requests",
                    json={"request_id": str(uuid.uuid4()), "confirmation": "DELETE MY ACCOUNT"},
                    headers=headers,
                )
                erased.raise_for_status()
                receipt = erased.json()
                print("Synthetic tenant erasure:", receipt["request_id"], receipt["status"])
        finally:
            await dispose_engine()


async def repeated():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repeat", type=int, default=1, choices=range(1, 21))
    args = parser.parse_args()
    for index in range(args.repeat):
        await main()
        if index + 1 < args.repeat:
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(repeated())
