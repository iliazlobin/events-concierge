"""Temporal worker: binds the container to the activities and serves the workflows + activities on the
configured task queue. Run with `python -m events_concierge.workflows.worker`."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Literal

from temporalio.client import Client
from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

from ..composition import build_container
from ..config import Settings, get_settings
from ..infra.logging import configure_logging, get_logger
from .activities import (
    await_confirmation,
    close_failed_candidate,
    compensate_calendar_write,
    complete_lifecycle,
    dedupe_calendar,
    discover_and_rank,
    enqueue_handoff_reminder,
    expire_handoff,
    finalize_no_candidate,
    link_request_outcome,
    policy_gate,
    reconcile_organizer_change,
    refresh_catalog_paged_legistar,
    refresh_catalog_single_get,
    register_erasure_workflow_targets,
    register_or_rsvp,
    resolve_membership,
    route_to_handoff,
    set_container,
    unrsvp,
    write_to_calendar,
)
from .temporal_client import connect_temporal, validate_temporal_settings
from .workflows import (
    CatalogPagedRefreshWorkflow,
    CatalogRefreshWorkflow,
    EventRequestWorkflow,
    RegistrationWorkflow,
)

_log = get_logger("worker")
TemporalWorkerWorkload = Literal["combined", "transactional", "catalog"]


@dataclass(frozen=True)
class _TemporalWorkerSpec:
    workload: TemporalWorkerWorkload
    task_queue: str
    max_concurrent_workflow_tasks: int
    max_concurrent_activities: int


def build_temporal_worker(
    client: Client,
    *,
    task_queue: str,
    max_concurrent_workflow_tasks: int,
    max_concurrent_activities: int,
    workflow_task_executor: ThreadPoolExecutor,
    workload: TemporalWorkerWorkload = "combined",
    build_id: str | None = None,
    deployment_config: WorkerDeploymentConfig | None = None,
) -> Worker:
    """Construct the worker with explicit execution limits.

    The Temporal SDK otherwise defaults to hundreds of workflow threads and one hundred activity
    slots. That is unsafe for the small application container and its bounded database pool: a
    restart can drain a durable backlog fast enough to starve both workflow tasks and SQL clients.
    Keeping this constructor parameterized also makes the deployment limits directly testable.
    """
    workflows: list[type[Any]]
    activities: list[Callable[..., Any]]
    if workload == "catalog":
        workflows = [CatalogRefreshWorkflow, CatalogPagedRefreshWorkflow]
        activities = [refresh_catalog_single_get, refresh_catalog_paged_legistar]
    elif workload == "transactional":
        workflows = [EventRequestWorkflow, RegistrationWorkflow]
        activities = [
            discover_and_rank,
            register_erasure_workflow_targets,
            finalize_no_candidate,
            link_request_outcome,
            resolve_membership,
            policy_gate,
            close_failed_candidate,
            register_or_rsvp,
            await_confirmation,
            compensate_calendar_write,
            dedupe_calendar,
            write_to_calendar,
            reconcile_organizer_change,
            unrsvp,
            route_to_handoff,
            complete_lifecycle,
            expire_handoff,
            enqueue_handoff_reminder,
        ]
    else:
        workflows = [
            CatalogRefreshWorkflow,
            CatalogPagedRefreshWorkflow,
            EventRequestWorkflow,
            RegistrationWorkflow,
        ]
        activities = [
            refresh_catalog_single_get,
            refresh_catalog_paged_legistar,
            discover_and_rank,
            register_erasure_workflow_targets,
            finalize_no_candidate,
            link_request_outcome,
            resolve_membership,
            policy_gate,
            close_failed_candidate,
            register_or_rsvp,
            await_confirmation,
            compensate_calendar_write,
            dedupe_calendar,
            write_to_calendar,
            reconcile_organizer_change,
            unrsvp,
            route_to_handoff,
            complete_lifecycle,
            expire_handoff,
            enqueue_handoff_reminder,
        ]

    return Worker(
        client,
        task_queue=task_queue,
        workflows=workflows,
        activities=activities,
        workflow_task_executor=workflow_task_executor,
        max_concurrent_workflow_tasks=max_concurrent_workflow_tasks,
        max_concurrent_activities=max_concurrent_activities,
        build_id=build_id,
        deployment_config=deployment_config,
    )


def build_worker_deployment_config(
    settings: Settings,
    workload: TemporalWorkerWorkload,
) -> WorkerDeploymentConfig | None:
    """Build the installed SDK's pinned Worker Deployment version when enabled.

    A role suffix gives catalog and transactional worker deployments independent promotion state.
    The server-side current/ramping-version transition remains an operator/release-controller act;
    constructing a worker must never silently promote its own build.
    """
    if not settings.temporal_worker_versioning_enabled:
        return None
    deployment_name = settings.temporal_worker_deployment_name
    if workload != "combined":
        deployment_name = f"{deployment_name}-{workload}"
    return WorkerDeploymentConfig(
        version=WorkerDeploymentVersion(
            deployment_name=deployment_name,
            build_id=settings.temporal_effective_worker_build_id,
        ),
        use_worker_versioning=True,
        default_versioning_behavior=VersioningBehavior.PINNED,
    )


def _temporal_worker_specs(settings: Settings) -> tuple[_TemporalWorkerSpec, ...]:
    role = settings.temporal_worker_role
    if role == "transactional":
        return (
            _TemporalWorkerSpec(
                workload="transactional",
                task_queue=settings.temporal_transactional_queue,
                max_concurrent_workflow_tasks=(
                    settings.temporal_worker_max_concurrent_workflow_tasks
                ),
                max_concurrent_activities=settings.temporal_worker_max_concurrent_activities,
            ),
        )
    if role == "catalog":
        return (
            _TemporalWorkerSpec(
                workload="catalog",
                task_queue=settings.temporal_catalog_queue,
                max_concurrent_workflow_tasks=(
                    settings.temporal_worker_max_concurrent_workflow_tasks
                ),
                max_concurrent_activities=settings.temporal_worker_max_concurrent_activities,
            ),
        )
    if settings.temporal_transactional_queue == settings.temporal_catalog_queue:
        return (
            _TemporalWorkerSpec(
                workload="combined",
                task_queue=settings.temporal_transactional_queue,
                max_concurrent_workflow_tasks=(
                    settings.temporal_worker_max_concurrent_workflow_tasks
                ),
                max_concurrent_activities=settings.temporal_worker_max_concurrent_activities,
            ),
        )

    transactional_workflows, catalog_workflows = _split_worker_capacity(
        settings.temporal_worker_max_concurrent_workflow_tasks,
        minimum_per_workload=2,
    )
    transactional_activities, catalog_activities = _split_worker_capacity(
        settings.temporal_worker_max_concurrent_activities,
        minimum_per_workload=1,
    )
    return (
        _TemporalWorkerSpec(
            workload="transactional",
            task_queue=settings.temporal_transactional_queue,
            max_concurrent_workflow_tasks=transactional_workflows,
            max_concurrent_activities=transactional_activities,
        ),
        _TemporalWorkerSpec(
            workload="catalog",
            task_queue=settings.temporal_catalog_queue,
            max_concurrent_workflow_tasks=catalog_workflows,
            max_concurrent_activities=catalog_activities,
        ),
    )


def _split_worker_capacity(total: int, *, minimum_per_workload: int) -> tuple[int, int]:
    if total < minimum_per_workload * 2:
        raise ValueError("combined split-queue worker capacity is below the SDK minimum")
    catalog = max(minimum_per_workload, total // 4)
    return total - catalog, catalog


async def _run_temporal_workers(workers: list[Worker]) -> None:
    """Run all configured queue pollers and stop siblings before surfacing one failure."""
    if len(workers) == 1:
        await workers[0].run()
        return

    tasks = [asyncio.create_task(worker.run()) for worker in workers]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        failure = next(
            (
                task.exception()
                for task in tasks
                if task in done and not task.cancelled() and task.exception() is not None
            ),
            None,
        )
        if failure is not None:
            raise failure
        await asyncio.gather(*pending)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    validate_temporal_settings(settings)
    container = build_container(settings)
    set_container(container)

    client = await connect_temporal(settings, container.object_store)
    executors: list[ThreadPoolExecutor] = []
    try:
        workers: list[Worker] = []
        for spec in _temporal_worker_specs(settings):
            thread_name_prefix = "ec-workflow"
            if spec.workload != "combined":
                thread_name_prefix = f"ec-workflow-{spec.workload}"
            workflow_task_executor = ThreadPoolExecutor(
                max_workers=spec.max_concurrent_workflow_tasks,
                thread_name_prefix=thread_name_prefix,
            )
            executors.append(workflow_task_executor)
            deployment_config = build_worker_deployment_config(settings, spec.workload)
            worker = build_temporal_worker(
                client,
                task_queue=spec.task_queue,
                max_concurrent_workflow_tasks=spec.max_concurrent_workflow_tasks,
                max_concurrent_activities=spec.max_concurrent_activities,
                workflow_task_executor=workflow_task_executor,
                workload=spec.workload,
                build_id=(
                    None
                    if deployment_config is not None
                    else settings.temporal_effective_worker_build_id
                ),
                deployment_config=deployment_config,
            )
            workers.append(worker)
            _log.info(
                "worker started",
                workload=spec.workload,
                task_queue=spec.task_queue,
                worker_build_id=settings.temporal_effective_worker_build_id,
                worker_versioning_enabled=settings.temporal_worker_versioning_enabled,
                max_concurrent_workflow_tasks=spec.max_concurrent_workflow_tasks,
                max_concurrent_activities=spec.max_concurrent_activities,
            )
        await _run_temporal_workers(workers)
    finally:
        # The SDK does not own a caller-provided executor. Cancel queued work after the worker has
        # stopped so process restarts cannot leak its bounded workflow thread pool.
        for workflow_task_executor in reversed(executors):
            workflow_task_executor.shutdown(wait=True, cancel_futures=True)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
