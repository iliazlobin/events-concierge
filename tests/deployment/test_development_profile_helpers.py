"""Private promotion and smoke cannot depend on capabilities deferred from discovery."""

import importlib.util
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import pytest
from temporalio.api.deployment.v1 import WorkerDeploymentOptions
from temporalio.api.enums.v1 import WorkerVersioningMode
from temporalio.api.taskqueue.v1 import PollerInfo
from temporalio.api.workflowservice.v1 import (
    DescribeTaskQueueResponse,
    DescribeWorkerDeploymentResponse,
)
from temporalio.client import WorkflowExecutionStatus

ROOT = Path(__file__).resolve().parents[2]
TENANT = "10000000-0000-4000-8000-000000000001"
REQUEST = "20000000-0000-4000-8000-000000000001"


def load_helper(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/development/{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


promotion = load_helper("promote_workers")
smoke = load_helper("smoke")


def settings(**overrides):
    return SimpleNamespace(
        **{
            "env": "development",
            "mock_cloud": True,
            "release_profile": "discovery",
            "google_calendar_enabled": False,
            "temporal_namespace": "events-development",
            "temporal_target": "temporal:7233",
            "temporal_worker_deployment_name": "events-concierge",
            "temporal_effective_worker_build_id": "abc1234",
            "temporal_worker_versioning_enabled": True,
            "temporal_catalog_queue": "catalog",
            "temporal_transactional_queue": "transactional",
            "database_url": "synthetic",
            **overrides,
        }
    )


def poller(role, *, build="abc1234", age=0, versioned=True):
    result = PollerInfo(
        deployment_options=WorkerDeploymentOptions(
            deployment_name="events-concierge-" + role,
            build_id=build,
            worker_versioning_mode=(
                WorkerVersioningMode.WORKER_VERSIONING_MODE_VERSIONED if versioned else 0
            ),
        )
    )
    result.last_access_time.FromDatetime(datetime.now(UTC) - timedelta(seconds=age))
    return result


def promotion_service(monkeypatch, profile="discovery", bad_role=None, **bad_poller):
    observed = []

    async def describe_queue(request):
        observed.append(request)
        role = request.task_queue.name
        result = poller(role, **(bad_poller if role == bad_role else {}))
        return DescribeTaskQueueResponse(pollers=[result])

    service = SimpleNamespace(
        describe_task_queue=AsyncMock(side_effect=describe_queue),
        describe_worker_deployment=AsyncMock(
            return_value=DescribeWorkerDeploymentResponse(conflict_token=b"optimistic-lock")
        ),
        set_worker_deployment_current_version=AsyncMock(),
    )
    monkeypatch.setattr(promotion, "get_settings", lambda: settings(release_profile=profile))
    monkeypatch.setattr(
        promotion.Client,
        "connect",
        AsyncMock(return_value=SimpleNamespace(workflow_service=service)),
    )
    return service, observed


@pytest.mark.parametrize(
    ("profile", "roles"), [("discovery", ["catalog"]), ("full", ["transactional", "catalog"])]
)
async def test_promotes_only_enabled_roles_after_both_candidate_pollers(
    monkeypatch, profile, roles
):
    service, observed = promotion_service(monkeypatch, profile)
    await promotion.main()
    assert [(r.task_queue.name, r.task_queue_type) for r in observed] == [
        (role, kind) for role in roles for kind in (1, 2)
    ]
    requests = [c.args[0] for c in service.set_worker_deployment_current_version.await_args_list]
    assert [r.deployment_name for r in requests] == ["events-concierge-" + role for role in roles]
    assert all(
        r.build_id == "abc1234"
        and r.conflict_token == b"optimistic-lock"
        and r.namespace == "events-development"
        for r in requests
    )


@pytest.mark.parametrize(
    "bad", [{"build": "old"}, {"age": 121}, {"age": -60}, {"versioned": False}]
)
async def test_unready_second_role_prevents_all_promotions(monkeypatch, bad):
    service, _ = promotion_service(monkeypatch, "full", "catalog", **bad)
    with pytest.raises(SystemExit, match="no recent versioned"):
        await promotion.main()
    service.set_worker_deployment_current_version.assert_not_awaited()


async def test_missing_activity_poller_prevents_promotion(monkeypatch):
    service, _ = promotion_service(monkeypatch)
    service.describe_task_queue.side_effect = [
        DescribeTaskQueueResponse(pollers=[poller("catalog")]),
        DescribeTaskQueueResponse(),
    ]
    with pytest.raises(SystemExit, match="no recent versioned"):
        await promotion.main()
    service.set_worker_deployment_current_version.assert_not_awaited()


@pytest.mark.parametrize(
    "override", [{"env": "production"}, {"mock_cloud": False}, {"temporal_namespace": "default"}]
)
@pytest.mark.parametrize("helper", [promotion, smoke], ids=["promotion", "smoke"])
async def test_development_guard_precedes_any_io(monkeypatch, override, helper):
    monkeypatch.setattr(helper, "get_settings", lambda: settings(**override))
    connect = AsyncMock()
    http = Mock(side_effect=AssertionError("must not connect"))
    monkeypatch.setattr(promotion.Client, "connect", connect)
    monkeypatch.setattr(smoke.httpx, "AsyncClient", http)
    with pytest.raises(SystemExit, match="development"):
        await helper.main()
    connect.assert_not_called()
    http.assert_not_called()


@pytest.fixture
def runtime(monkeypatch):
    calls = []
    profile = {"revision": 0}
    config = {"release_profile": "discovery", "local_demo": True, "auth_mode": "local_demo"}
    faults = {}

    def respond(request):  # noqa: PLR0911 - one explicit response per API contract
        path = request.url.path
        calls.append(request)
        if path in faults:
            return httpx.Response(faults[path], json={"detail": "synthetic failure"})
        if path in {"/healthz", "/readyz"}:
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v1/ui-config":
            return httpx.Response(200, json=config)
        if path == "/v1/onboard":
            email = json.loads(request.content)["notify_email"]
            assert email.startswith("gke-smoke-") and email.endswith("@example.invalid")
            return httpx.Response(200, json={"tenant_id": TENANT})
        assert request.headers["X-EC-Tenant-ID"] == TENANT
        if path == "/v1/me/erasure-requests":
            assert json.loads(request.content)["confirmation"] == "DELETE MY ACCOUNT"
            return httpx.Response(202, json={"request_id": REQUEST, "status": "pending"})
        if path in {"/v1/requests", "/v1/feed", "/v1/me/api-keys"}:
            if config["release_profile"] == "discovery":
                assert not request.content  # Never valid work-creation payloads in discovery.
                return httpx.Response(404)
            return httpx.Response(200, json={"request_id": REQUEST})
        if path == "/v1/me":
            return httpx.Response(200, json={"profile": profile})
        if path == "/v1/me/profile":
            profile.update(json.loads(request.content))
            return httpx.Response(200, json=profile)
        if path == "/v1/catalog/events":
            following = "cursor" in request.url.params
            return httpx.Response(
                200,
                json={
                    "items": [{"event_id": REQUEST}],
                    "next_cursor": None if following else "next",
                },
            )
        if path == "/v1/catalog/events/summary":
            assert request.url.params["time_zone"] == "America/Los_Angeles"
            return httpx.Response(200, json={"total_event_count": 2})
        raise AssertionError(f"Unexpected route {request.method} {path}")

    factory = httpx.AsyncClient

    def http_client(**kwargs):
        assert kwargs["base_url"] == "http://127.0.0.1:8000" and kwargs["trust_env"] is False
        return factory(**kwargs, transport=httpx.MockTransport(respond))

    store = SimpleNamespace(put=AsyncMock(), get=AsyncMock(), delete_tenant=AsyncMock())

    async def put(tenant, key, payload):
        store.get.return_value = payload

    store.put.side_effect = put
    handle = SimpleNamespace(
        id="synthetic-workflow",
        describe=AsyncMock(return_value=SimpleNamespace(status=WorkflowExecutionStatus.COMPLETED)),
        result=AsyncMock(return_value={"synthetic": True}),
        fetch_history=AsyncMock(return_value=SimpleNamespace(events=[None] * 6)),
    )
    client = SimpleNamespace(get_workflow_handle=Mock(return_value=handle))
    monkeypatch.setattr(
        smoke, "get_settings", lambda: settings(release_profile=config["release_profile"])
    )
    monkeypatch.setattr(smoke.httpx, "AsyncClient", http_client)
    monkeypatch.setattr(smoke, "build_runtime_ports", lambda s: SimpleNamespace(object_store=store))
    init = Mock()
    dispose = AsyncMock()
    claim = AsyncMock()
    monkeypatch.setattr(smoke, "init_engine", init)
    monkeypatch.setattr(smoke, "dispose_engine", dispose)
    monkeypatch.setattr(smoke, "connect_temporal", AsyncMock(return_value=client))
    monkeypatch.setattr(smoke, "check_claim_check", claim)
    return SimpleNamespace(**locals())


async def test_discovery_smoke_has_no_transactional_worker_dependency(runtime, capsys):
    await smoke.main()
    runtime.client.get_workflow_handle.assert_not_called()
    runtime.claim.assert_awaited_once_with(runtime.client, TENANT)
    paths = [r.url.path for r in runtime.calls]
    assert paths.count("/v1/catalog/events") == 2
    assert "/v1/me/profile" in paths and "/v1/catalog/events/summary" in paths
    assert paths[-1] == "/v1/me/erasure-requests"
    runtime.store.delete_tenant.assert_awaited_once()
    runtime.dispose.assert_awaited_once()
    assert "pending" in capsys.readouterr().out


async def test_full_smoke_preserves_request_replay_worker_completion_and_feed(runtime):
    runtime.config["release_profile"] = "full"
    await smoke.main()
    requests = [r for r in runtime.calls if r.url.path == "/v1/requests"]
    assert len(requests) == 2 and requests[0].content == requests[1].content
    runtime.handle.result.assert_awaited_once()
    runtime.handle.fetch_history.assert_awaited_once()
    assert any(r.url.path == "/v1/feed" for r in runtime.calls)
    runtime.claim.assert_awaited_once()


async def test_mismatched_server_profile_fails_before_any_fixture_write(runtime, monkeypatch):
    monkeypatch.setattr(smoke, "get_settings", lambda: settings(release_profile="full"))
    with pytest.raises(SystemExit, match="does not match"):
        await smoke.main()
    runtime.init.assert_not_called()
    runtime.store.put.assert_not_awaited()
    assert all(r.method == "GET" for r in runtime.calls)


@pytest.mark.parametrize("path", ["/v1/catalog/events", "/v1/me/profile", "/v1/requests"])
async def test_api_failure_still_fences_only_created_synthetic_account(runtime, path):
    runtime.faults[path] = 500
    with pytest.raises((httpx.HTTPStatusError, RuntimeError)):
        await smoke.main()
    assert runtime.calls[-1].url.path == "/v1/me/erasure-requests"
    runtime.claim.assert_not_awaited()
    runtime.dispose.assert_awaited_once()


async def test_failed_gcs_read_cleans_fixture_and_disposes_without_onboarding(runtime):
    runtime.store.get.side_effect = RuntimeError("read failed")
    with pytest.raises(RuntimeError, match="read failed"):
        await smoke.main()
    fixture = runtime.store.put.await_args.args[0]
    assert isinstance(fixture, UUID)
    runtime.store.delete_tenant.assert_awaited_once_with(fixture)
    runtime.dispose.assert_awaited_once()
    assert not any(r.url.path == "/v1/onboard" for r in runtime.calls)


async def test_claim_check_runs_own_worker_and_requires_small_history(monkeypatch):
    entered = []

    class Worker:
        def __init__(self, client, **kwargs):
            assert kwargs["task_queue"] == "development-claim-check-smoke"
            assert kwargs["workflows"] == [smoke.DevelopmentClaimCheckEcho]

        async def __aenter__(self):
            entered.append(True)

        async def __aexit__(self, *args):
            entered.pop()

    event = SimpleNamespace(SerializeToString=lambda: b"small-external-payload-reference")
    handle = SimpleNamespace(
        id="synthetic-claim",
        result=AsyncMock(),
        fetch_history=AsyncMock(return_value=SimpleNamespace(events=[event])),
    )

    async def start(function, payload, **kwargs):
        assert entered and len(payload) > 2 * 1024 * 1024
        assert kwargs["task_queue"] == "development-claim-check-smoke"
        assert TENANT in kwargs["id"]
        handle.result.return_value = len(payload)
        return handle

    monkeypatch.setattr(smoke, "Worker", Worker)
    client = SimpleNamespace(start_workflow=AsyncMock(side_effect=start))
    await smoke.check_claim_check(client, TENANT)
    event.SerializeToString = lambda: b"x" * (256 * 1024)
    with pytest.raises(AssertionError):
        await smoke.check_claim_check(client, TENANT)
    assert not entered


async def test_unversioned_promotion_fails_before_connect(monkeypatch):
    monkeypatch.setattr(
        promotion, "get_settings", lambda: settings(temporal_worker_versioning_enabled=False)
    )
    connect = AsyncMock()
    monkeypatch.setattr(promotion.Client, "connect", connect)
    with pytest.raises(SystemExit, match="Only versioned"):
        await promotion.main()
    connect.assert_not_awaited()


@pytest.mark.parametrize(
    "field,value", [("local_demo", False), ("auth_mode", "deployment_session")]
)
async def test_hosted_auth_cannot_create_synthetic_accounts(runtime, field, value):
    runtime.config[field] = value
    with pytest.raises(SystemExit, match="does not match"):
        await smoke.main()
    runtime.init.assert_not_called()
    assert all(r.method == "GET" for r in runtime.calls)


async def test_erasure_failure_is_not_reported_as_cleanup_success(runtime, capsys):
    runtime.faults["/v1/me/erasure-requests"] = 503
    with pytest.raises(httpx.HTTPStatusError):
        await smoke.main()
    runtime.dispose.assert_awaited_once()
    output = capsys.readouterr().out
    assert "Synthetic development tenant: " + TENANT in output
    assert "Synthetic tenant erasure:" not in output
