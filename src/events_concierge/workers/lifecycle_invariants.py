"""Read-only nightly lifecycle/watch/handoff divergence scanner (ADR-007/ADR-008).

The scanner observes PostgreSQL hygiene and Temporal execution liveness, then emits only
aggregate counts. It owns no repair path, lifecycle transition, queue lease, or provider call:
the guarded workflow and orphan-only repair workers remain the authoritative writers.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import cast

from temporalio.client import Client

from ..application.lifecycle_invariants import LifecycleInvariantScanner
from ..composition import build_container
from ..config import Settings, get_settings
from ..domain.invariants import LifecycleInvariantReport
from ..infra.logging import configure_logging, get_logger
from ..ports.object_store import ObjectStorePort
from ..ports.workflows import WorkflowLivenessInspector
from ..workflows.start import TemporalWorkflowLivenessInspector
from ..workflows.temporal_client import connect_temporal

_log = get_logger(__name__)


class _TemporalLivenessUnavailableError(RuntimeError):
    """Signal read-only uncertainty when the authoritative Temporal endpoint is unavailable."""


class _UnavailableWorkflowLivenessInspector:
    """Fail every liveness read rather than guessing closure during a Temporal outage (ADR-007)."""

    async def is_open(self, workflow_id: str) -> bool:
        """Keep the opaque identity out of errors and report it as scanner uncertainty."""
        del workflow_id
        raise _TemporalLivenessUnavailableError("Temporal liveness is unavailable")


async def run_lifecycle_invariants() -> None:
    """Continuously emit nightly aggregate integrity evidence without repairing detected drift."""
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")
    container = build_container(settings)
    client: Client | None = None
    _log.info(
        "lifecycle invariant scanner started",
        batch_size=settings.lifecycle_invariant_batch_size,
        poll_seconds=settings.lifecycle_invariant_poll_seconds,
    )
    while True:
        if client is None:
            client = await _try_connect_temporal(settings, container.object_store)
        liveness: WorkflowLivenessInspector = (
            TemporalWorkflowLivenessInspector(client, settings)
            if client is not None
            else _UnavailableWorkflowLivenessInspector()
        )
        try:
            report = await LifecycleInvariantScanner(
                container.lifecycle_invariant_repo,
                liveness,
            ).scan_once(batch_size=settings.lifecycle_invariant_batch_size)
        except Exception as error:
            # Error text can contain provider/transport details. Operational logging remains
            # PII-free even on a failed scan, and no repair is attempted (NFR-10, ADR-007).
            _log.warning(
                "lifecycle invariant scan failed",
                error_type=type(error).__name__,
            )
        else:
            _log_report(report)
            # A non-definitive liveness result can indicate a dropped Temporal connection. Drop
            # the client so the next scheduled observation establishes a fresh authoritative view.
            if report.uninspectable_nonterminal_workflows > 0:
                client = None
        await asyncio.sleep(settings.lifecycle_invariant_poll_seconds)


async def _try_connect_temporal(settings: Settings, object_store: ObjectStorePort) -> Client | None:
    """Connect opportunistically; DB-only hygiene still runs when liveness cannot be read."""
    try:
        return await connect_temporal(settings, object_store)
    except Exception as error:
        _log.warning(
            "temporal unavailable for lifecycle invariant scan",
            error_type=type(error).__name__,
        )
        return None


def _log_report(report: LifecycleInvariantReport) -> None:
    """Warn for divergence or durable projection backlog; otherwise emit a clean count-only scan."""
    fields = _report_fields(report)
    if report.requires_attention:
        _log.warning("lifecycle invariant attention required", **fields)
    else:
        _log.info("lifecycle invariant scan clean", **fields)


def _report_fields(report: LifecycleInvariantReport) -> dict[str, int | bool | dict[str, int]]:
    """Serialize only the aggregate domain projection, independent of individual metric names."""
    database_counts = cast(dict[str, int], asdict(report.database))
    return {
        "has_divergence": report.has_divergence,
        "has_pending_projection_backlog": report.has_pending_projection_backlog,
        "requires_attention": report.requires_attention,
        "scanned_nonterminal_workflows": report.scanned_nonterminal_workflows,
        "open_nonterminal_workflows": report.open_nonterminal_workflows,
        "closed_nonterminal_workflows": report.closed_nonterminal_workflows,
        "uninspectable_nonterminal_workflows": report.uninspectable_nonterminal_workflows,
        "database_invariants": database_counts,
    }


def main() -> None:
    asyncio.run(run_lifecycle_invariants())


if __name__ == "__main__":
    main()
