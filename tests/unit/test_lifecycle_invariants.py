"""Offline lifecycle-divergence scanner tests (ADR-007/ADR-008)."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import cast

import pytest

from events_concierge.adapters.mock.invariants import MockLifecycleInvariantRepository
from events_concierge.adapters.mock.liveness import MockWorkflowLivenessInspector
from events_concierge.application.lifecycle_invariants import LifecycleInvariantScanner
from events_concierge.domain.invariants import (
    LifecycleDatabaseInvariantSnapshot,
    LifecycleInvariantReport,
)
from events_concierge.ports.invariants import LifecycleInvariantRepository
from events_concierge.ports.workflows import WorkflowLivenessInspector


async def _no_sleep(_seconds: float) -> None:
    """Keep count-only unit fixtures fast when cadence itself is not under test."""


def _scanner(
    repository: MockLifecycleInvariantRepository,
    liveness: MockWorkflowLivenessInspector,
) -> LifecycleInvariantScanner:
    return LifecycleInvariantScanner(
        cast(LifecycleInvariantRepository, repository),
        cast(WorkflowLivenessInspector, liveness),
        sleep=_no_sleep,
    )


async def test_scanner_pages_opaque_ids_and_counts_closed_and_uncertain_workflows() -> None:
    """Closed is authoritative, while a Temporal failure stays observable uncertainty (ADR-007)."""
    workflow_ids = ("tenant-a:event-1", "tenant-b:event-2", "tenant-c:event-3")
    repository = MockLifecycleInvariantRepository(
        LifecycleDatabaseInvariantSnapshot(
            invalid_lifecycle_workflow_identity_count=1,
            terminal_watch_subscriptions=1,
        ),
        workflow_ids,
    )
    liveness = MockWorkflowLivenessInspector(
        {
            workflow_ids[0]: True,
            workflow_ids[1]: False,
            workflow_ids[2]: RuntimeError("Temporal unavailable"),
        }
    )

    report = await _scanner(repository, liveness).scan_once(batch_size=2)

    assert repository.calls == [(None, 2), (workflow_ids[1], 2)]
    assert liveness.calls == list(workflow_ids)
    assert report.scanned_nonterminal_workflows == 3
    assert report.open_nonterminal_workflows == 1
    assert report.closed_nonterminal_workflows == 1
    assert report.uninspectable_nonterminal_workflows == 1
    assert report.database.invalid_lifecycle_workflow_identity_count == 1
    assert report.database.terminal_watch_subscriptions == 1
    assert report.has_divergence is True
    serialized = asdict(report)
    assert all(workflow_id not in str(serialized) for workflow_id in workflow_ids)


async def test_scanner_returns_a_clean_count_only_report_without_temporal_calls() -> None:
    """A clean scan has no raw identities and does not invent a repair action (ADR-007)."""
    repository = MockLifecycleInvariantRepository()
    liveness = MockWorkflowLivenessInspector()

    report = await _scanner(repository, liveness).scan_once()

    assert report == _clean_report()
    assert report.has_divergence is False
    assert repository.calls == [(None, 500)]
    assert liveness.calls == []


async def test_scanner_separates_a_durable_projection_backlog_from_clean_liveness() -> None:
    """An undelivered projection is visible for operations without inventing a repair (ADR-008)."""
    repository = MockLifecycleInvariantRepository(
        LifecycleDatabaseInvariantSnapshot(pending_watch_register_projections=1)
    )

    report = await _scanner(repository, MockWorkflowLivenessInspector()).scan_once()

    assert report.has_divergence is False
    assert report.has_pending_projection_backlog is True
    assert report.requires_attention is True


async def test_scanner_rejects_an_unordered_repository_page_before_repeating_work() -> None:
    """A malformed keyset page must fail loudly rather than loop or silently skip lifecycle rows."""
    repository = MockLifecycleInvariantRepository(workflow_ids=("workflow-b", "workflow-a"))
    liveness = MockWorkflowLivenessInspector()

    with pytest.raises(RuntimeError, match="strictly ordered"):
        await _scanner(repository, liveness).scan_once(batch_size=2)

    assert liveness.calls == []


async def test_scanner_rejects_a_nonpositive_batch_size() -> None:
    """The fixed bounded scan contract cannot accept an unbounded or empty page."""
    with pytest.raises(ValueError, match="between 1 and 1000"):
        await _scanner(
            MockLifecycleInvariantRepository(), MockWorkflowLivenessInspector()
        ).scan_once(batch_size=0)
    with pytest.raises(ValueError, match="between 1 and 1000"):
        await _scanner(
            MockLifecycleInvariantRepository(), MockWorkflowLivenessInspector()
        ).scan_once(batch_size=1001)


async def test_scanner_spaces_liveness_calls_at_the_configured_start_cadence() -> None:
    """A large inventory cannot turn into a burst of back-to-back Temporal describes."""
    workflow_ids = ("workflow-a", "workflow-b", "workflow-c")
    repository = MockLifecycleInvariantRepository(workflow_ids=workflow_ids)
    liveness = MockWorkflowLivenessInspector(dict.fromkeys(workflow_ids, True))
    now = 100.0
    sleeps: list[float] = []

    def monotonic() -> float:
        return now

    async def sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    report = await LifecycleInvariantScanner(
        cast(LifecycleInvariantRepository, repository),
        cast(WorkflowLivenessInspector, liveness),
        liveness_calls_per_second=10.0,
        monotonic=monotonic,
        sleep=sleep,
    ).scan_once(batch_size=2)

    assert sleeps == pytest.approx([0.1, 0.1])
    assert liveness.calls == list(workflow_ids)
    assert report.open_nonterminal_workflows == 3


async def test_scanner_stops_liveness_calls_after_first_error_but_counts_full_inventory() -> None:
    """One transport failure fails closed without abandoning the full nightly DB inventory."""
    workflow_ids = tuple(f"workflow-{index}" for index in range(5))
    repository = MockLifecycleInvariantRepository(workflow_ids=workflow_ids)
    liveness = MockWorkflowLivenessInspector(
        {
            workflow_ids[0]: True,
            workflow_ids[1]: RuntimeError("Temporal unavailable"),
        }
    )

    report = await _scanner(repository, liveness).scan_once(batch_size=2)

    assert repository.calls == [
        (None, 2),
        (workflow_ids[1], 2),
        (workflow_ids[3], 2),
    ]
    assert liveness.calls == list(workflow_ids[:2])
    assert report.scanned_nonterminal_workflows == 5
    assert report.open_nonterminal_workflows == 1
    assert report.closed_nonterminal_workflows == 0
    assert report.uninspectable_nonterminal_workflows == 4


async def test_scanner_cancellation_interrupts_cadence_sleep_without_another_rpc() -> None:
    """Worker shutdown remains prompt while a scan is waiting for its next liveness slot."""
    workflow_ids = ("workflow-a", "workflow-b")
    repository = MockLifecycleInvariantRepository(workflow_ids=workflow_ids)
    liveness = MockWorkflowLivenessInspector(dict.fromkeys(workflow_ids, True))

    async def cancelling_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    scanner = LifecycleInvariantScanner(
        cast(LifecycleInvariantRepository, repository),
        cast(WorkflowLivenessInspector, liveness),
        monotonic=lambda: 0.0,
        sleep=cancelling_sleep,
    )

    with pytest.raises(asyncio.CancelledError):
        await scanner.scan_once(batch_size=2)

    assert liveness.calls == [workflow_ids[0]]


def test_scanner_rejects_an_unsafe_liveness_cadence() -> None:
    """Direct callers cannot bypass the same conservative per-process safety envelope."""
    repository = cast(LifecycleInvariantRepository, MockLifecycleInvariantRepository())
    liveness = cast(WorkflowLivenessInspector, MockWorkflowLivenessInspector())

    with pytest.raises(ValueError, match="between 1 and 20"):
        LifecycleInvariantScanner(repository, liveness, liveness_calls_per_second=0.9)
    with pytest.raises(ValueError, match="between 1 and 20"):
        LifecycleInvariantScanner(repository, liveness, liveness_calls_per_second=20.1)


def _clean_report() -> LifecycleInvariantReport:
    """Keep the expected report legible without carrying identities into the fixture."""
    return LifecycleInvariantReport(database=LifecycleDatabaseInvariantSnapshot())
