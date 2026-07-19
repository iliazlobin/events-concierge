"""Operational boundaries for the read-only nightly lifecycle-invariant worker."""

from __future__ import annotations

from dataclasses import asdict

import pytest

from events_concierge.config import Settings
from events_concierge.domain.invariants import (
    LifecycleDatabaseInvariantSnapshot,
    LifecycleInvariantReport,
)
from events_concierge.workers.lifecycle_invariants import (
    _report_fields,
    _TemporalLivenessUnavailableError,
    _UnavailableWorkflowLivenessInspector,
)


def test_lifecycle_invariant_scanner_uses_a_bounded_nightly_cadence() -> None:
    """ADR-007's invariant scan is nightly, bounded, and cannot be configured unbounded."""
    settings = Settings()

    assert settings.lifecycle_invariant_batch_size == 500
    assert settings.lifecycle_invariant_poll_seconds == 86_400.0
    with pytest.raises(ValueError):
        Settings(lifecycle_invariant_batch_size=0)
    with pytest.raises(ValueError):
        Settings(lifecycle_invariant_batch_size=1_001)
    with pytest.raises(ValueError):
        Settings(lifecycle_invariant_poll_seconds=0.0)


async def test_temporal_outage_is_explicit_uncertainty_not_a_closed_workflow() -> None:
    """The worker's fallback never authorizes a repair when Temporal is unavailable."""
    with pytest.raises(_TemporalLivenessUnavailableError):
        await _UnavailableWorkflowLivenessInspector().is_open("opaque-workflow-id")


def test_log_fields_are_count_only_and_exclude_opaque_workflow_values() -> None:
    """Worker logs carry only PII-free aggregate evidence (NFR-10, ADR-007)."""
    report = LifecycleInvariantReport(
        database=LifecycleDatabaseInvariantSnapshot(),
        scanned_nonterminal_workflows=3,
        open_nonterminal_workflows=1,
        closed_nonterminal_workflows=1,
        uninspectable_nonterminal_workflows=1,
    )

    fields = _report_fields(report)

    assert fields == {
        "has_divergence": True,
        "has_pending_projection_backlog": False,
        "requires_attention": True,
        "scanned_nonterminal_workflows": 3,
        "open_nonterminal_workflows": 1,
        "closed_nonterminal_workflows": 1,
        "uninspectable_nonterminal_workflows": 1,
        "database_invariants": asdict(report.database),
    }
    assert all(
        isinstance(value, int | bool)
        for key, value in fields.items()
        if key != "database_invariants"
    )
    assert all(isinstance(value, int) for value in fields["database_invariants"].values())
