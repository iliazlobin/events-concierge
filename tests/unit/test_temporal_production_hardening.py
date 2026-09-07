"""Hermetic contracts for Temporal queue isolation and worker deployment identity."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, cast

import pytest
from temporalio.client import (
    OutboundInterceptor,
    StartWorkflowInput,
    WorkflowHandle,
)
from temporalio.common import (
    Priority,
    VersioningBehavior,
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.worker import Worker

from events_concierge.config import Settings
from events_concierge.workflows import worker as worker_module
from events_concierge.workflows.temporal_client import (
    TemporalTaskQueueRoutingInterceptor,
    validate_temporal_settings,
)


def _deployed_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "env": "staging",
        "release_revision": "0123456789abcdef0123456789abcdef01234567",
        "temporal_task_queue": "events-catalog",
        "temporal_transactional_task_queue": "events-transactional",
        "temporal_catalog_task_queue": "events-catalog",
        "temporal_worker_versioning_enabled": True,
        "temporal_tls_enabled": True,
        "temporal_tls_domain": "namespace.tmprl.cloud",
        "temporal_api_key": "fixture-api-key",
    }
    values.update(overrides)
    return Settings(**values)


def test_local_temporal_queues_retain_the_combined_backward_compatible_default() -> None:
    settings = Settings()

    assert settings.temporal_task_queue == "events-concierge"
    assert settings.temporal_transactional_queue == "events-concierge"
    assert settings.temporal_catalog_queue == "events-concierge"
    assert settings.temporal_worker_role == "combined"
    assert settings.temporal_effective_worker_build_id == "development"


def test_local_combined_worker_polls_an_explicit_shared_queue() -> None:
    settings = Settings(
        temporal_transactional_task_queue="shared-local",
        temporal_catalog_task_queue="shared-local",
    )

    (spec,) = worker_module._temporal_worker_specs(settings)

    assert spec.workload == "combined"
    assert spec.task_queue == "shared-local"


def test_deployed_temporal_queue_and_versioning_posture_is_valid_without_reachability() -> None:
    validate_temporal_settings(_deployed_settings())


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"temporal_catalog_task_queue": None}, "explicit transactional and catalog"),
        (
            {"temporal_catalog_task_queue": "events-transactional"},
            "must be separate",
        ),
        ({"temporal_task_queue": "legacy-third-queue"}, "must alias"),
        ({"temporal_worker_versioning_enabled": False}, "require Worker Deployment"),
        ({"temporal_worker_build_id": "mutable-branch"}, "commit or semantic version"),
        (
            {"temporal_worker_max_concurrent_workflow_tasks": 3},
            "at least four workflow-task slots",
        ),
        (
            {"temporal_worker_max_concurrent_activities": 1},
            "at least two activity slots",
        ),
    ],
)
def test_deployed_temporal_posture_fails_closed(
    overrides: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        validate_temporal_settings(_deployed_settings(**overrides))


class _RecordingStartOutbound(OutboundInterceptor):
    def __init__(self) -> None:
        self.inputs: list[StartWorkflowInput] = []

    async def start_workflow(
        self,
        input: StartWorkflowInput,
    ) -> WorkflowHandle[Any, Any]:
        self.inputs.append(input)
        return cast("WorkflowHandle[Any, Any]", object())


def _start_input(workflow_name: str, *, task_queue: str = "legacy") -> StartWorkflowInput:
    return StartWorkflowInput(
        workflow=workflow_name,
        args=[],
        id="workflow-id",
        task_queue=task_queue,
        execution_timeout=None,
        run_timeout=None,
        task_timeout=None,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        id_conflict_policy=WorkflowIDConflictPolicy.UNSPECIFIED,
        retry_policy=None,
        cron_schedule="",
        memo=None,
        search_attributes=None,
        start_delay=None,
        headers={},
        start_signal=None,
        start_signal_args=[],
        static_summary=None,
        static_details=None,
        ret_type=None,
        rpc_metadata={},
        rpc_timeout=None,
        request_eager_start=False,
        priority=Priority.default,
        callbacks=[],
        links=[],
        request_id=None,
    )


async def test_client_interceptor_routes_known_starts_and_preserves_unknown_queues() -> None:
    recorder = _RecordingStartOutbound()
    router = TemporalTaskQueueRoutingInterceptor(
        transactional_queue="transactional",
        catalog_queue="catalog",
    ).intercept_client(recorder)

    for workflow_name in (
        "EventRequestWorkflow",
        "RegistrationWorkflow",
        "CatalogRefreshWorkflow",
        "CatalogPagedRefreshWorkflow",
        "DeploymentOwnedWorkflow",
    ):
        await router.start_workflow(_start_input(workflow_name))

    assert [input.task_queue for input in recorder.inputs] == [
        "transactional",
        "transactional",
        "catalog",
        "catalog",
        "legacy",
    ]


def test_combined_worker_splits_queue_registrations_and_preserves_total_capacity() -> None:
    settings = _deployed_settings()

    specs = worker_module._temporal_worker_specs(settings)

    assert [(spec.workload, spec.task_queue) for spec in specs] == [
        ("transactional", "events-transactional"),
        ("catalog", "events-catalog"),
    ]
    assert sum(spec.max_concurrent_workflow_tasks for spec in specs) == 8
    assert sum(spec.max_concurrent_activities for spec in specs) == 8
    assert all(spec.max_concurrent_workflow_tasks >= 2 for spec in specs)
    assert all(spec.max_concurrent_activities >= 1 for spec in specs)


def test_role_specific_worker_uses_the_full_per_process_capacity() -> None:
    settings = _deployed_settings(
        temporal_worker_role="catalog",
        temporal_worker_max_concurrent_workflow_tasks=5,
        temporal_worker_max_concurrent_activities=6,
    )

    assert worker_module._temporal_worker_specs(settings) == (
        worker_module._TemporalWorkerSpec(
            workload="catalog",
            task_queue="events-catalog",
            max_concurrent_workflow_tasks=5,
            max_concurrent_activities=6,
        ),
    )


def test_worker_deployment_config_uses_pinned_sdk_versioning_and_role_identity() -> None:
    settings = _deployed_settings(temporal_worker_build_id="v1.2.3")

    deployment = worker_module.build_worker_deployment_config(settings, "transactional")

    assert deployment is not None
    assert deployment.version.deployment_name == "events-concierge-transactional"
    assert deployment.version.build_id == "v1.2.3"
    assert deployment.use_worker_versioning is True
    assert deployment.default_versioning_behavior is VersioningBehavior.PINNED
    assert worker_module.build_worker_deployment_config(Settings(), "combined") is None


def test_queue_specific_workers_register_only_their_workload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[dict[str, Any]] = []
    expected_worker = cast(Worker, object())

    def worker_factory(*args: object, **kwargs: Any) -> Worker:
        del args
        captured.append(kwargs)
        return expected_worker

    monkeypatch.setattr(worker_module, "Worker", worker_factory)

    with ThreadPoolExecutor(max_workers=2) as executor:
        for workload in ("transactional", "catalog"):
            assert (
                worker_module.build_temporal_worker(
                    cast("Any", object()),
                    task_queue=f"{workload}-queue",
                    max_concurrent_workflow_tasks=2,
                    max_concurrent_activities=2,
                    workflow_task_executor=executor,
                    workload=workload,
                )
                is expected_worker
            )

    transactional, catalog = captured
    assert [workflow.__name__ for workflow in transactional["workflows"]] == [
        "EventRequestWorkflow",
        "RegistrationWorkflow",
    ]
    assert len(transactional["activities"]) == 18
    assert [workflow.__name__ for workflow in catalog["workflows"]] == [
        "CatalogRefreshWorkflow",
        "CatalogPagedRefreshWorkflow",
    ]
    assert catalog["activities"] == [
        worker_module.refresh_catalog_single_get,
        worker_module.refresh_catalog_paged_legistar,
    ]


async def test_split_worker_runtime_cancels_the_other_queue_before_surfacing_failure() -> None:
    catalog_cancelled = False

    class BlockingCatalogWorker:
        async def run(self) -> None:
            nonlocal catalog_cancelled
            try:
                await asyncio.Event().wait()
            finally:
                catalog_cancelled = True

    class FailingTransactionalWorker:
        async def run(self) -> None:
            raise RuntimeError("transactional poller failed")

    workers = cast(
        "list[Worker]",
        [BlockingCatalogWorker(), FailingTransactionalWorker()],
    )

    with pytest.raises(RuntimeError, match="transactional poller failed"):
        await worker_module._run_temporal_workers(workers)

    assert catalog_cancelled is True
