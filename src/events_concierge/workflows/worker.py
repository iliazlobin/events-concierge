"""Temporal worker: binds the container to the activities and serves the workflows + activities on the
configured task queue. Run with `python -m events_concierge.workflows.worker`."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from ..composition import build_container
from ..config import get_settings
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
    policy_gate,
    reconcile_organizer_change,
    refresh_catalog_paged_legistar,
    refresh_catalog_single_get,
    register_or_rsvp,
    resolve_membership,
    route_to_handoff,
    set_container,
    unrsvp,
    write_to_calendar,
)
from .temporal_client import connect_temporal
from .workflows import (
    CatalogPagedRefreshWorkflow,
    CatalogRefreshWorkflow,
    EventRequestWorkflow,
    RegistrationWorkflow,
)

_log = get_logger("worker")


def build_temporal_worker(
    client: Client,
    *,
    task_queue: str,
    max_concurrent_workflow_tasks: int,
    max_concurrent_activities: int,
    workflow_task_executor: ThreadPoolExecutor,
) -> Worker:
    """Construct the worker with explicit execution limits.

    The Temporal SDK otherwise defaults to hundreds of workflow threads and one hundred activity
    slots. That is unsafe for the small application container and its bounded database pool: a
    restart can drain a durable backlog fast enough to starve both workflow tasks and SQL clients.
    Keeping this constructor parameterized also makes the deployment limits directly testable.
    """
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[
            CatalogRefreshWorkflow,
            CatalogPagedRefreshWorkflow,
            EventRequestWorkflow,
            RegistrationWorkflow,
        ],
        activities=[
            refresh_catalog_single_get,
            refresh_catalog_paged_legistar,
            discover_and_rank,
            finalize_no_candidate,
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
        ],
        workflow_task_executor=workflow_task_executor,
        max_concurrent_workflow_tasks=max_concurrent_workflow_tasks,
        max_concurrent_activities=max_concurrent_activities,
    )


async def run_worker() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    container = build_container(settings)
    set_container(container)

    client = await connect_temporal(settings, container.object_store)
    workflow_task_executor = ThreadPoolExecutor(
        max_workers=settings.temporal_worker_max_concurrent_workflow_tasks,
        thread_name_prefix="ec-workflow",
    )
    try:
        worker = build_temporal_worker(
            client,
            task_queue=settings.temporal_task_queue,
            max_concurrent_workflow_tasks=(
                settings.temporal_worker_max_concurrent_workflow_tasks
            ),
            max_concurrent_activities=settings.temporal_worker_max_concurrent_activities,
            workflow_task_executor=workflow_task_executor,
        )
        _log.info(
            "worker started",
            task_queue=settings.temporal_task_queue,
            max_concurrent_workflow_tasks=(
                settings.temporal_worker_max_concurrent_workflow_tasks
            ),
            max_concurrent_activities=settings.temporal_worker_max_concurrent_activities,
        )
        await worker.run()
    finally:
        # The SDK does not own a caller-provided executor. Cancel queued work after the worker has
        # stopped so process restarts cannot leak its bounded workflow thread pool.
        workflow_task_executor.shutdown(wait=True, cancel_futures=True)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
