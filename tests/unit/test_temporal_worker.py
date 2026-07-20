"""Bounded Temporal worker construction and lifecycle tests."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError
from temporalio.client import Client
from temporalio.worker import Worker

from events_concierge.composition import Container
from events_concierge.config import Settings
from events_concierge.workflows import worker as worker_module


def test_temporal_worker_limits_have_conservative_validated_defaults() -> None:
    settings = Settings()

    assert settings.temporal_worker_max_concurrent_workflow_tasks == 8
    assert settings.temporal_worker_max_concurrent_activities == 8

    for invalid in (0, 1, 65):
        with pytest.raises(ValidationError):
            Settings.model_validate(
                {"temporal_worker_max_concurrent_workflow_tasks": invalid}
            )
    minimum = Settings(temporal_worker_max_concurrent_workflow_tasks=2)
    assert minimum.temporal_worker_max_concurrent_workflow_tasks == 2

    for invalid in (0, 65):
        with pytest.raises(ValidationError):
            Settings.model_validate({"temporal_worker_max_concurrent_activities": invalid})


def test_temporal_worker_receives_explicit_execution_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    expected_worker = cast(Worker, object())
    client = cast(Client, object())

    def worker_factory(received_client: Client, **kwargs: Any) -> Worker:
        captured["client"] = received_client
        captured.update(kwargs)
        return expected_worker

    monkeypatch.setattr(worker_module, "Worker", worker_factory)

    with ThreadPoolExecutor(max_workers=3) as executor:
        result = worker_module.build_temporal_worker(
            client,
            task_queue="fixture-queue",
            max_concurrent_workflow_tasks=3,
            max_concurrent_activities=4,
            workflow_task_executor=executor,
        )

    assert result is expected_worker
    assert captured["client"] is client
    assert captured["task_queue"] == "fixture-queue"
    assert captured["max_concurrent_workflow_tasks"] == 3
    assert captured["max_concurrent_activities"] == 4
    assert captured["workflow_task_executor"] is executor
    assert len(captured["workflows"]) == 4
    assert len(captured["activities"]) == 18


async def test_worker_runtime_shuts_down_its_executor_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        temporal_worker_max_concurrent_workflow_tasks=3,
        temporal_worker_max_concurrent_activities=4,
    )
    container = cast(Container, SimpleNamespace(object_store=object()))
    client = cast(Client, object())
    captured: dict[str, Any] = {}

    class FixtureExecutor:
        def __init__(self, *, max_workers: int, thread_name_prefix: str) -> None:
            captured["executor_max_workers"] = max_workers
            captured["thread_name_prefix"] = thread_name_prefix

        def shutdown(self, *, wait: bool, cancel_futures: bool) -> None:
            captured["shutdown"] = (wait, cancel_futures)

    class FixtureWorker:
        async def run(self) -> None:
            raise RuntimeError("fixture worker failure")

    async def connect(*args: object, **kwargs: object) -> Client:
        del args, kwargs
        return client

    def build(received_client: Client, **kwargs: Any) -> Worker:
        captured["client"] = received_client
        captured.update(kwargs)
        return cast(Worker, FixtureWorker())

    monkeypatch.setattr(worker_module, "get_settings", lambda: settings)
    monkeypatch.setattr(worker_module, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(worker_module, "build_container", lambda _: container)
    monkeypatch.setattr(worker_module, "set_container", lambda _: None)
    monkeypatch.setattr(worker_module, "connect_temporal", connect)
    monkeypatch.setattr(worker_module, "build_temporal_worker", build)
    monkeypatch.setattr(worker_module, "ThreadPoolExecutor", FixtureExecutor)

    with pytest.raises(RuntimeError, match="fixture worker failure"):
        await worker_module.run_worker()

    assert captured["client"] is client
    assert captured["executor_max_workers"] == 3
    assert captured["thread_name_prefix"] == "ec-workflow"
    assert captured["max_concurrent_workflow_tasks"] == 3
    assert captured["max_concurrent_activities"] == 4
    assert captured["shutdown"] == (True, True)
