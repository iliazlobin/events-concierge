"""Bounded Temporal workflow-start and lifecycle-signal adapter contracts."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, cast
from uuid import uuid4

from temporalio.client import Client

from events_concierge.config import Settings
from events_concierge.workflows.dto import HandoffCompletionSignal, UnrsvpSignal
from events_concierge.workflows.start import (
    TemporalCatalogPagedRefreshStarter,
    TemporalCatalogRefreshStarter,
    TemporalRegistrationLifecycleSignaler,
    TemporalRequestWorkflowStarter,
)


class _RecordingStartClient:
    """Minimal client seam that captures native workflow-start RPC options."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def start_workflow(self, *args: object, **kwargs: Any) -> None:
        del args
        self.calls.append(kwargs)


class _RecordingSignalHandle:
    """Minimal workflow handle that captures signal payloads and native RPC bounds."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, timedelta | None]] = []

    async def signal(
        self,
        name: str,
        arg: object,
        *,
        rpc_timeout: timedelta | None = None,
    ) -> None:
        self.calls.append((name, arg, rpc_timeout))


class _RecordingHandleClient:
    """Return one handle while retaining each opaque execution identity."""

    def __init__(self, handle: _RecordingSignalHandle) -> None:
        self.handle = handle
        self.workflow_ids: list[str] = []

    def get_workflow_handle(self, workflow_id: str) -> _RecordingSignalHandle:
        self.workflow_ids.append(workflow_id)
        return self.handle


async def test_every_temporal_workflow_start_receives_the_configured_rpc_timeout() -> None:
    """Intake and catalog dispatch cannot wait beyond the deployment's short RPC deadline."""
    timeout = timedelta(seconds=0.125)
    settings = Settings(
        temporal_rpc_timeout_seconds=timeout.total_seconds(),
        pacer_backend="redis",
    )
    client = _RecordingStartClient()
    temporal = cast(Client, client)

    await TemporalRequestWorkflowStarter(temporal, settings).start(uuid4(), uuid4())
    await TemporalCatalogRefreshStarter(temporal, settings).start("source", "run")
    await TemporalCatalogPagedRefreshStarter(temporal, settings).start("paged-source", "run")

    assert len(client.calls) == 3
    assert [call["rpc_timeout"] for call in client.calls] == [timeout, timeout, timeout]


async def test_every_registration_signal_receives_the_configured_rpc_timeout() -> None:
    """User commands have a client deadline without changing their stable replay identities."""
    timeout = timedelta(seconds=0.25)
    handle = _RecordingSignalHandle()
    client = _RecordingHandleClient(handle)
    signaler = TemporalRegistrationLifecycleSignaler(
        cast(Client, client),
        Settings(temporal_rpc_timeout_seconds=timeout.total_seconds()),
    )

    await signaler.signal_unrsvp("workflow-1", "request-1")
    await signaler.signal_handoff_completed("workflow-1", "task-1", "completion-1")

    assert client.workflow_ids == ["workflow-1", "workflow-1"]
    assert handle.calls == [
        (
            "unrsvp_requested",
            UnrsvpSignal(request_id="request-1"),
            timeout,
        ),
        (
            "handoff_completed",
            HandoffCompletionSignal(task_id="task-1", completion_id="completion-1"),
            timeout,
        ),
    ]
