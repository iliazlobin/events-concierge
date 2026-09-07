"""Safe run-evidence projection from the v4 capability into the admin domain."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from events_concierge.adapters.postgres.ingestion_admin import _run_status_from_row
from events_concierge.application.catalog_execution_descriptors import (
    CatalogExecutionDescriptorRegistry,
)

_NOW = datetime(2026, 7, 31, 20, 30, tzinfo=UTC)


def _row() -> dict[str, object]:
    return {
        "source_key": "city-events",
        "display_name": "City events",
        "run_key": "admin:11111111-1111-4111-8111-111111111111",
        "status": "succeeded",
        "started_at": _NOW,
        "completed_at": _NOW,
        "candidate_count": 20,
        "canonical_count": 19,
        "error": None,
        "attempt_count": 1,
        "duration_ms": 100,
        "source_revision": 3,
        "release_revision": "development",
        "image_digest": None,
        "provenance_status": "claim_recorded",
        "trigger": "admin_source",
        "is_latest_for_source": True,
        "resolved_by_newer_success": False,
        "mode": "public_jsonld",
        "reviewed_at": _NOW,
        "review_expires_at": None,
        "refresh_interval_minutes": 60,
        "min_interval_ms": 1_500,
        "page_limit": 1,
        "command_id": UUID("11111111-1111-4111-8111-111111111111"),
        "command_action": "refresh_source",
        "command_requested_at": _NOW,
        "command_started_at": _NOW,
        "command_completed_at": _NOW,
        "execution_count": 1,
        "execution_wall_time_ms": 90,
        "process_cpu_time_ms": 30,
        "rss_before_bytes": 1_000,
        "rss_after_bytes": 1_200,
        "boundary_observed_peak_rss_bytes": 1_200,
        "process_lifetime_peak_rss_bytes": 2_000,
        "measurement_source": "python_monotonic+process_time+linux_procfs+getrusage",
        "measurement_scope": "worker_process_boundary_samples",
        "measurement_quality": "best_effort_process_delta_sequential_worker",
        "execution_last_outcome_code": "succeeded",
        "execution_first_observed_at": _NOW,
        "execution_last_observed_at": _NOW,
        "stage_metrics": [
            {
                "stage": "admission",
                "observation_count": 1,
                "duration_ms": 2,
                "last_outcome_code": "succeeded",
                "first_observed_at": _NOW,
                "last_observed_at": _NOW,
            },
            {
                "stage": "collect",
                "observation_count": 1,
                "duration_ms": 60,
                "last_outcome_code": "succeeded",
                "first_observed_at": _NOW,
                "last_observed_at": _NOW,
            },
            {
                "stage": "catalog_publish",
                "observation_count": 1,
                "duration_ms": 25,
                "last_outcome_code": "succeeded",
                "first_observed_at": _NOW,
                "last_observed_at": _NOW,
            },
        ],
    }


def test_v4_projection_exposes_command_resources_descriptor_and_fixed_stage_trace() -> None:
    run = _run_status_from_row(_row(), CatalogExecutionDescriptorRegistry("events-concierge-test"))

    assert run.command is not None
    assert run.command.command_id == UUID("11111111-1111-4111-8111-111111111111")
    assert run.execution is not None
    assert run.execution.execution_path == "guarded_direct"
    assert run.execution.adapter_module.endswith("adapters/crawl/source.py")
    assert run.resources is not None
    assert run.resources.cpu_utilization_percent == 33.33
    assert [item.stage for item in run.stage_trace] == [
        "admission",
        "collect",
        "extract_enrich",
        "normalize_dedupe",
        "catalog_publish",
    ]
    assert run.stage_trace[1].evidence_status == "measured"
    assert run.stage_trace[2].evidence_status == "not_separately_instrumented"
    assert run.stage_trace[2].duration_ms is None
    assert run.stage_trace[3].note_code == "included_in_catalog_commit_boundary"
    assert run.timeline.evidence_scope == "lifecycle_and_aggregate_observations"
    assert run.timeline.complete is False
    assert [entry.event_code for entry in run.timeline.entries] == [
        "command_requested",
        "command_started",
        "run_attempt_started",
        "stage_observed",
        "stage_observed",
        "stage_observed",
        "execution_observed",
        "run_status_observed",
        "command_completed",
    ]
    assert [
        entry.stage for entry in run.timeline.entries if entry.event_code == "stage_observed"
    ] == ["admission", "collect", "catalog_publish"]
    assert all(
        entry.timestamp_basis == "evidence_recorded"
        for entry in run.timeline.entries
        if entry.event_code in {"stage_observed", "execution_observed"}
    )


def test_v4_projection_decodes_iso_timestamps_nested_by_postgres_jsonb() -> None:
    row = _row()
    row["stage_metrics"] = [
        {
            "stage": "collect",
            "observation_count": 1,
            "duration_ms": 60,
            "last_outcome_code": "succeeded",
            "first_observed_at": _NOW.isoformat(),
            "last_observed_at": _NOW.isoformat().replace("+00:00", "Z"),
        }
    ]

    run = _run_status_from_row(row, CatalogExecutionDescriptorRegistry("events-concierge-test"))

    collect = next(item for item in run.stage_trace if item.stage == "collect")
    assert collect.first_observed_at == _NOW
    assert collect.last_observed_at == _NOW


def test_v4_projection_rejects_timezone_naive_nested_stage_timestamp() -> None:
    row = _row()
    row["stage_metrics"] = [
        {
            "stage": "collect",
            "observation_count": 1,
            "duration_ms": 60,
            "last_outcome_code": "succeeded",
            "first_observed_at": "2026-07-31T20:30:00",
            "last_observed_at": _NOW.isoformat(),
        }
    ]

    with pytest.raises(RuntimeError, match="timezone-naive"):
        _run_status_from_row(row, CatalogExecutionDescriptorRegistry("events-concierge-test"))


def test_fixture_projection_survives_without_reviewed_descriptor_or_metrics() -> None:
    row = _row()
    row.update(
        {
            "source_key": "fixture-events",
            "run_key": "fixture:run-1",
            "trigger": "cadence_or_manual",
            "mode": None,
            "reviewed_at": None,
            "refresh_interval_minutes": None,
            "min_interval_ms": None,
            "page_limit": None,
            "command_id": None,
            "command_action": None,
            "command_requested_at": None,
            "command_started_at": None,
            "command_completed_at": None,
            "execution_count": None,
            "execution_wall_time_ms": None,
            "process_cpu_time_ms": None,
            "measurement_source": None,
            "measurement_scope": None,
            "measurement_quality": None,
            "execution_last_outcome_code": None,
            "execution_first_observed_at": None,
            "execution_last_observed_at": None,
            "stage_metrics": [],
        }
    )

    run = _run_status_from_row(row, CatalogExecutionDescriptorRegistry("events-concierge-test"))

    assert run.source_key == "fixture-events"
    assert run.source_configuration is None
    assert run.execution is None
    assert run.resources is None
    assert all(item.evidence_status == "legacy_unavailable" for item in run.stage_trace)
    assert run.timeline.evidence_scope == "lifecycle_only"
    assert run.timeline.complete is False
    assert [entry.event_code for entry in run.timeline.entries] == [
        "run_attempt_started",
        "run_status_observed",
    ]
    assert all(entry.stage is None for entry in run.timeline.entries)
    assert run.timeline.entries[-1].timestamp_basis == "run_projection"


def test_timeline_projects_one_summary_point_for_aggregate_retry_evidence() -> None:
    row = _row()
    row.update(
        {
            "command_id": None,
            "command_action": None,
            "command_requested_at": None,
            "command_started_at": None,
            "command_completed_at": None,
            "execution_count": None,
            "execution_wall_time_ms": None,
            "process_cpu_time_ms": None,
            "measurement_source": None,
            "measurement_scope": None,
            "measurement_quality": None,
            "execution_last_outcome_code": None,
            "execution_first_observed_at": None,
            "execution_last_observed_at": None,
            "stage_metrics": [
                {
                    "stage": "collect",
                    "observation_count": 3,
                    "duration_ms": 180,
                    "last_outcome_code": "progressed",
                    "first_observed_at": "2026-07-31T20:30:01+00:00",
                    "last_observed_at": "2026-07-31T20:35:00+00:00",
                }
            ],
        }
    )

    run = _run_status_from_row(row)

    observed = [entry for entry in run.timeline.entries if entry.event_code == "stage_observed"]
    assert len(observed) == 1
    assert observed[0].observed_at == datetime(2026, 7, 31, 20, 35, tzinfo=UTC)
    assert observed[0].observation_count == 3
    assert observed[0].duration_ms == 180
    assert observed[0].outcome_code == "progressed"
