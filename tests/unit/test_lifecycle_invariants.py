"""Offline lifecycle-divergence scanner tests (ADR-007/ADR-008)."""

from __future__ import annotations

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


def _scanner(
    repository: MockLifecycleInvariantRepository,
    liveness: MockWorkflowLivenessInspector,
) -> LifecycleInvariantScanner:
    return LifecycleInvariantScanner(
        cast(LifecycleInvariantRepository, repository),
        cast(WorkflowLivenessInspector, liveness),
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


def _clean_report() -> LifecycleInvariantReport:
    """Keep the expected report legible without carrying identities into the fixture."""
    return LifecycleInvariantReport(database=LifecycleDatabaseInvariantSnapshot())
