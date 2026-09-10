"""Best-effort, payload-free execution observations for catalog refresh workers."""

from __future__ import annotations

import os
import resource
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from ..domain.catalog_sources import CatalogRunExecutionEvidence, CatalogRunStageEvidence
from ..ports.catalog_sources import CatalogRunEvidenceRecorder
from .ingestion_telemetry import (
    emit_ingestion_event,
    ingestion_error_type,
    record_ingestion_collection_progress,
    start_ingestion_collection_progress,
)

_PROC_STATM = Path("/proc/self/statm")
_PROC_STATM_MIN_FIELDS = 2


class CatalogRunEvidenceSession:
    """Measure one worker entry without allowing observability to affect ingestion.

    ``include_process_metrics`` is deliberately false in shared Temporal activity workers.  Their
    process CPU/RSS deltas include other concurrent activities and therefore cannot honestly be
    attributed to one source run.  Direct command-worker executions may opt in to best-effort
    process boundary samples while retaining an explicit quality label.
    """

    def __init__(
        self,
        recorder: CatalogRunEvidenceRecorder | None,
        source_key: str,
        run_key: str,
        *,
        include_process_metrics: bool,
    ) -> None:
        self._recorder = recorder
        self._source_key = source_key
        self._run_key = run_key
        self._include_process_metrics = include_process_metrics
        self._wall_started_ns = time.perf_counter_ns()
        self._cpu_started_ns = time.process_time_ns() if include_process_metrics else None
        self._rss_before = _current_rss_bytes() if include_process_metrics else None
        self._active_stage: CatalogRunStageTimer | None = None

    async def start_stage(self, stage: str) -> CatalogRunStageTimer:
        """Record actual stage entry before the awaited provider/database work begins."""
        timer = CatalogRunStageTimer(
            self._recorder,
            self._source_key,
            self._run_key,
            stage,
        )
        self._active_stage = timer
        await emit_ingestion_event(
            "stage_started",
            stage=stage,
            outcome_code="started",
            source_key=self._source_key,
            run_key=self._run_key,
        )
        # Exclude the bounded delivery wait from the stage's actual work duration.
        timer.started_ns = time.perf_counter_ns()
        if stage == "collect":
            start_ingestion_collection_progress(source_key=self._source_key, run_key=self._run_key)
        return timer

    async def finish(self, outcome_code: str, *, error: Exception | None = None) -> None:
        """Persist a bounded aggregate if the durable run row exists."""
        if error is not None and self._active_stage is not None:
            await self._active_stage.finish(outcome_code, error=error)
        recorder = self._recorder
        if recorder is None:
            return
        wall_time_ms = _elapsed_ms(self._wall_started_ns)
        if not self._include_process_metrics:
            evidence = CatalogRunExecutionEvidence(
                wall_time_ms=wall_time_ms,
                process_cpu_time_ms=None,
                rss_before_bytes=None,
                rss_after_bytes=None,
                boundary_observed_peak_rss_bytes=None,
                process_lifetime_peak_rss_bytes=None,
                measurement_source="python_monotonic",
                measurement_scope="activity_wall_clock_only",
                measurement_quality="process_metrics_omitted_shared_worker",
                outcome_code=outcome_code,
            )
        else:
            assert self._cpu_started_ns is not None
            rss_after = _current_rss_bytes()
            lifetime_peak = _process_lifetime_peak_rss_bytes()
            observed = [value for value in (self._rss_before, rss_after) if value is not None]
            source_parts = ["python_monotonic", "process_time"]
            if self._rss_before is not None or rss_after is not None:
                source_parts.append("linux_procfs")
            if lifetime_peak is not None:
                source_parts.append("getrusage")
            evidence = CatalogRunExecutionEvidence(
                wall_time_ms=wall_time_ms,
                process_cpu_time_ms=_elapsed_ms(self._cpu_started_ns, time.process_time_ns()),
                rss_before_bytes=self._rss_before,
                rss_after_bytes=rss_after,
                boundary_observed_peak_rss_bytes=max(observed) if observed else None,
                process_lifetime_peak_rss_bytes=lifetime_peak,
                measurement_source="+".join(source_parts),
                measurement_scope="worker_process_boundary_samples",
                measurement_quality="best_effort_process_delta_sequential_worker",
                outcome_code=outcome_code,
            )
        try:
            await recorder.record_run_execution(self._source_key, self._run_key, evidence)
        except Exception:
            # Metrics are never part of the lease-fenced catalog effect.  A telemetry outage must
            # not rewrite or fail an otherwise valid refresh result.
            return


@dataclass(slots=True)
class CatalogRunStageTimer:
    """One monotonic stage stopwatch with a best-effort durable finish."""

    recorder: CatalogRunEvidenceRecorder | None
    source_key: str
    run_key: str
    stage: str
    started_ns: int = 0
    finished: bool = False

    def __post_init__(self) -> None:
        self.started_ns = time.perf_counter_ns()

    async def progress(self, *, candidate_count: int) -> None:
        """Observe completed collection work; this is independent of command lease renewal."""
        if self.finished:
            return
        if self.stage == "collect":
            await record_ingestion_collection_progress(
                source_key=self.source_key,
                run_key=self.run_key,
                candidate_count=candidate_count,
                force=True,
            )
            return
        await emit_ingestion_event(
            "progress",
            stage=self.stage,
            outcome_code="progressed",
            duration_ms=_elapsed_ms(self.started_ns),
            candidate_count=candidate_count,
            source_key=self.source_key,
            run_key=self.run_key,
        )

    async def finish(
        self,
        outcome_code: str,
        *,
        error: Exception | None = None,
        candidate_count: int | None = None,
        canonical_count: int | None = None,
    ) -> None:
        if self.finished:
            return
        self.finished = True
        duration_ms = _elapsed_ms(self.started_ns)
        if self.stage == "collect":
            await record_ingestion_collection_progress(
                source_key=self.source_key,
                run_key=self.run_key,
                force=True,
            )
        if error is not None:
            await emit_ingestion_event(
                "error",
                stage=self.stage,
                outcome_code=outcome_code,
                duration_ms=duration_ms,
                error_type=ingestion_error_type(error),
                source_key=self.source_key,
                run_key=self.run_key,
            )
        await emit_ingestion_event(
            "stage_completed",
            stage=self.stage,
            outcome_code=outcome_code,
            duration_ms=duration_ms,
            candidate_count=candidate_count,
            canonical_count=canonical_count,
            source_key=self.source_key,
            run_key=self.run_key,
        )
        recorder = self.recorder
        if recorder is None:
            return
        evidence = CatalogRunStageEvidence(
            stage=self.stage,
            duration_ms=duration_ms,
            outcome_code=outcome_code,
        )
        try:
            await recorder.record_run_stage(self.source_key, self.run_key, evidence)
        except Exception:
            return


def _elapsed_ms(started_ns: int, completed_ns: int | None = None) -> int:
    return max(0, ((completed_ns or time.perf_counter_ns()) - started_ns) // 1_000_000)


def _current_rss_bytes() -> int | None:
    """Return Linux procfs current RSS, not a process-lifetime high-water mark."""
    try:
        fields = _PROC_STATM.read_text(encoding="ascii").split()
        if len(fields) < _PROC_STATM_MIN_FIELDS:
            return None
        resident_pages = int(fields[1])
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        value = resident_pages * page_size
        return value if value >= 0 else None
    except (OSError, ValueError):
        return None


def _process_lifetime_peak_rss_bytes() -> int | None:
    """Return ``ru_maxrss`` with its process-lifetime meaning retained in the field name."""
    try:
        value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (OSError, ValueError):
        return None
    if value < 0:
        return None
    # Linux and the BSDs report KiB; macOS reports bytes.
    return value if sys.platform == "darwin" else value * 1024
