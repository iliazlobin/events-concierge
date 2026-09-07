"""Typed run evidence and reviewed execution descriptor contracts."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from events_concierge.application.catalog_execution_descriptors import (
    CatalogExecutionDescriptorRegistry,
)
from events_concierge.application.catalog_run_evidence import CatalogRunEvidenceSession
from events_concierge.domain.catalog_sources import (
    CatalogRunExecutionEvidence,
    CatalogRunStageEvidence,
)


@dataclass
class _Recorder:
    executions: list[tuple[str, str, CatalogRunExecutionEvidence]] = field(default_factory=list)
    stages: list[tuple[str, str, CatalogRunStageEvidence]] = field(default_factory=list)

    async def record_run_execution(
        self,
        source_key: str,
        run_key: str,
        evidence: CatalogRunExecutionEvidence,
    ) -> bool:
        self.executions.append((source_key, run_key, evidence))
        return True

    async def record_run_stage(
        self,
        source_key: str,
        run_key: str,
        evidence: CatalogRunStageEvidence,
    ) -> bool:
        self.stages.append((source_key, run_key, evidence))
        return True


@pytest.mark.asyncio
async def test_shared_activity_records_wall_time_but_omits_unattributable_process_metrics() -> None:
    recorder = _Recorder()
    session = CatalogRunEvidenceSession(
        recorder,
        "libcal-events",
        "cadence:libcal-events:20260731T120000Z",
        include_process_metrics=False,
    )
    stage = session.start_stage("collect")

    await stage.finish("succeeded")
    await session.finish("succeeded")

    assert recorder.stages[0][2].stage == "collect"
    execution = recorder.executions[0][2]
    assert execution.wall_time_ms >= 0
    assert execution.process_cpu_time_ms is None
    assert execution.rss_before_bytes is None
    assert execution.rss_after_bytes is None
    assert execution.measurement_scope == "activity_wall_clock_only"
    assert execution.measurement_quality == "process_metrics_omitted_shared_worker"


@pytest.mark.asyncio
async def test_direct_worker_labels_process_samples_as_best_effort_boundary_evidence() -> None:
    recorder = _Recorder()
    session = CatalogRunEvidenceSession(
        recorder,
        "city-events",
        "admin:11111111-1111-4111-8111-111111111111",
        include_process_metrics=True,
    )

    await session.finish("succeeded")

    execution = recorder.executions[0][2]
    assert execution.process_cpu_time_ms is not None
    assert execution.process_cpu_time_ms >= 0
    assert execution.measurement_scope == "worker_process_boundary_samples"
    assert execution.measurement_quality == "best_effort_process_delta_sequential_worker"
    assert execution.measurement_source.startswith("python_monotonic+process_time")


def test_descriptor_registry_resolves_exact_temporal_and_direct_ownership() -> None:
    registry = CatalogExecutionDescriptorRegistry("events-concierge-test")

    single_get = registry.describe(
        source_key="library-events",
        mode="libcal_ics",
        page_limit=1,
        trigger="cadence_or_manual",
    )
    assert single_get is not None
    assert single_get.execution_path == "temporal_single_get"
    assert single_get.task_queue == "events-concierge-test"
    assert single_get.worker_symbol == "refresh_catalog_single_get"

    paged = registry.describe(
        source_key="oakland-legistar-meetings",
        mode="oakland_legistar",
        page_limit=5,
        trigger="cadence_or_manual",
    )
    assert paged is not None
    assert paged.execution_path == "temporal_paged"
    assert paged.adapter_symbol == "LegistarCatalogFetcher.fetch_page"

    direct = registry.describe(
        source_key="city-events",
        mode="public_jsonld",
        page_limit=1,
        trigger="admin_source",
    )
    assert direct is not None
    assert direct.execution_path == "guarded_direct"
    assert direct.worker_service == "ingestion-command-worker"
    assert direct.task_queue is None
    assert direct.worker_module.endswith("workers/ingestion_commands.py")


def test_descriptor_registry_returns_none_for_fixture_or_unknown_mode() -> None:
    registry = CatalogExecutionDescriptorRegistry("events-concierge-test")
    assert (
        registry.describe(
            source_key="fixture-events",
            mode=None,
            page_limit=None,
            trigger="cadence_or_manual",
        )
        is None
    )
