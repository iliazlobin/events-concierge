"""PostgreSQL implementation of the reviewed-source registry and refresh-run ledger.

Both tables are tenant-neutral catalog control-plane data. A unique ``(source_key, run_key)`` plus
a lease token prevents a restarted/manual worker from treating a completed source bucket as new
work (NFR-8).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import Row, text

from ...domain.catalog_sources import (
    CatalogPagedRefreshPreparation,
    CatalogPagedRefreshProgress,
    CatalogPagedStageResult,
    CatalogRefreshClaim,
    CatalogRefreshDue,
    CatalogRefreshRun,
    CatalogRunExecutionEvidence,
    CatalogRunStageEvidence,
    CatalogSource,
    CatalogSourcePage,
    catalog_candidate_content_hash,
)
from ...domain.enums import (
    CatalogRefreshClaimOutcome,
    CatalogRefreshRunStatus,
    CatalogSourceMode,
)
from ...domain.events import CandidateEvent, event_entity_profiles_payload
from ...infra.db import system_session_scope


class PostgresCatalogSourceRepository:
    """Read the reviewed registry and invoke its fixed refresh-run capabilities (FR-10.3/NFR-8)."""

    async def get(self, source_key: str) -> CatalogSource | None:
        async with system_session_scope() as session:
            row = (
                await session.execute(
                    text("SELECT * FROM catalog_sources WHERE source_key = :source_key"),
                    {"source_key": source_key},
                )
            ).first()
        return _source_from_row(row) if row is not None else None

    async def list_refreshable(self, now: datetime) -> list[CatalogSource]:
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT * FROM catalog_sources
                        WHERE enabled
                          AND reviewed_at IS NOT NULL
                          AND (review_expires_at IS NULL OR review_expires_at > :now)
                        ORDER BY source_key
                        """
                    ),
                    {"now": now},
                )
            ).all()
        return [_source_from_row(row) for row in rows]

    async def list_due_refreshes(self, now: datetime, *, limit: int) -> list[CatalogRefreshDue]:
        """Select only a bounded, deterministic set of elapsed registry schedule slots.

        A successful manual run is intentionally equivalent to a scheduled success: it resets the
        source's next due time. Failed/running rows do not advance that cursor, so their stable
        source/slot key remains reclaimable by the existing lease service (NFR-1/NFR-8).
        """
        if limit <= 0:
            raise ValueError("catalog refresh due-source limit must be positive")
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT *
                        FROM public.fn_list_due_catalog_refreshes(:now, :limit)
                        """
                    ),
                    {"now": now, "limit": limit},
                )
            ).all()
        return [
            CatalogRefreshDue(
                source=_source_from_row(row),
                due_at=row.due_at,
                last_succeeded_at=row.last_succeeded_at,
            )
            for row in rows
        ]

    async def claim_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_seconds: int,
    ) -> CatalogRefreshClaim:
        if lease_seconds <= 0:
            raise ValueError("catalog refresh lease_seconds must be positive")
        lease_token = uuid4()
        async with system_session_scope() as session:
            result = await session.execute(
                text(
                    """
                    SELECT public.fn_claim_catalog_refresh(
                        :source_key, :run_key, :lease_seconds, :lease_token
                    ) AS outcome
                    """
                ),
                {
                    "source_key": source_key,
                    "run_key": run_key,
                    "lease_token": lease_token,
                    "lease_seconds": lease_seconds,
                },
            )
            outcome = str(result.scalar_one())
        if outcome == CatalogRefreshClaimOutcome.ACQUIRED.value:
            return CatalogRefreshClaim(CatalogRefreshClaimOutcome.ACQUIRED, lease_token)
        if outcome == CatalogRefreshClaimOutcome.SUCCEEDED.value:
            return CatalogRefreshClaim(CatalogRefreshClaimOutcome.SUCCEEDED)
        return CatalogRefreshClaim(CatalogRefreshClaimOutcome.BUSY)

    async def record_run_execution(
        self,
        source_key: str,
        run_key: str,
        evidence: CatalogRunExecutionEvidence,
    ) -> bool:
        """Record bounded process/wall evidence through its fixed capability."""
        async with system_session_scope() as session:
            recorded = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_record_catalog_refresh_run_execution_v1(
                            :source_key, :run_key, :wall_time_ms, :process_cpu_time_ms,
                            :rss_before_bytes, :rss_after_bytes,
                            :boundary_observed_peak_rss_bytes,
                            :process_lifetime_peak_rss_bytes, :measurement_source,
                            :measurement_scope, :measurement_quality, :outcome_code
                        )
                        """
                    ),
                    {
                        "source_key": source_key,
                        "run_key": run_key,
                        "wall_time_ms": evidence.wall_time_ms,
                        "process_cpu_time_ms": evidence.process_cpu_time_ms,
                        "rss_before_bytes": evidence.rss_before_bytes,
                        "rss_after_bytes": evidence.rss_after_bytes,
                        "boundary_observed_peak_rss_bytes": (
                            evidence.boundary_observed_peak_rss_bytes
                        ),
                        "process_lifetime_peak_rss_bytes": (
                            evidence.process_lifetime_peak_rss_bytes
                        ),
                        "measurement_source": evidence.measurement_source,
                        "measurement_scope": evidence.measurement_scope,
                        "measurement_quality": evidence.measurement_quality,
                        "outcome_code": evidence.outcome_code,
                    },
                )
            ).scalar_one()
        return bool(recorded)

    async def record_run_stage(
        self,
        source_key: str,
        run_key: str,
        evidence: CatalogRunStageEvidence,
    ) -> bool:
        """Record one closed-vocabulary monotonic stage timing."""
        async with system_session_scope() as session:
            recorded = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_record_catalog_refresh_run_stage_v1(
                            :source_key, :run_key, :stage, :duration_ms, :outcome_code
                        )
                        """
                    ),
                    {
                        "source_key": source_key,
                        "run_key": run_key,
                        "stage": evidence.stage,
                        "duration_ms": evidence.duration_ms,
                        "outcome_code": evidence.outcome_code,
                    },
                )
            ).scalar_one()
        return bool(recorded)

    async def has_live_refresh_lease(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
    ) -> bool:
        """Read the final generic-fetch lease fence without exposing refresh-run rows (NFR-8)."""
        async with system_session_scope() as session:
            live = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_has_live_catalog_refresh_lease(
                            :source_key, :run_key, :lease_token
                        ) AS live
                        """
                    ),
                    {
                        "source_key": source_key,
                        "run_key": run_key,
                        "lease_token": lease_token,
                    },
                )
            ).scalar_one()
        return bool(live)

    async def complete_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        candidate_count: int,
        canonical_count: int,
    ) -> bool:
        async with system_session_scope() as session:
            completed = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_complete_catalog_refresh(
                            :source_key, :run_key, :lease_token, :candidate_count, :canonical_count
                        ) AS completed
                        """
                    ),
                    {
                        "source_key": source_key,
                        "run_key": run_key,
                        "lease_token": lease_token,
                        "candidate_count": candidate_count,
                        "canonical_count": canonical_count,
                    },
                )
            ).scalar_one()
        return bool(completed)

    async def fail_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        async with system_session_scope() as session:
            failed = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_fail_catalog_refresh(
                            :source_key, :run_key, :lease_token, :error
                        ) AS failed
                        """
                    ),
                    {
                        "source_key": source_key,
                        "run_key": run_key,
                        "lease_token": lease_token,
                        "error": error[:2_000],
                    },
                )
            ).scalar_one()
        return bool(failed)

    async def pause_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        """Release a generic Pacer defer through the existing lease-fenced pause capability."""
        async with system_session_scope() as session:
            paused = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_pause_paged_catalog_refresh(
                            :source_key, :run_key, :lease_token, :error
                        ) AS paused
                        """
                    ),
                    {
                        "source_key": source_key,
                        "run_key": run_key,
                        "lease_token": lease_token,
                        "error": error[:2_000],
                    },
                )
            ).scalar_one()
        return bool(paused)

    async def get_refresh_run(self, source_key: str, run_key: str) -> CatalogRefreshRun | None:
        async with system_session_scope() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT *
                        FROM public.fn_get_catalog_refresh_run(:source_key, :run_key)
                        """
                    ),
                    {"source_key": source_key, "run_key": run_key},
                )
            ).first()
        return _run_from_row(row) if row is not None else None

    async def prepare_paged_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        source_revision: int,
    ) -> CatalogPagedRefreshPreparation:
        """Initialize/read P15b's frozen Legistar cursor through its owner capability (NFR-8)."""
        async with system_session_scope() as session:
            row = (
                await session.execute(
                    text(
                        """
                        SELECT * FROM public.fn_prepare_paged_catalog_refresh(
                            :source_key, :run_key, :lease_token, :source_revision
                        )
                        """
                    ),
                    {
                        "source_key": source_key,
                        "run_key": run_key,
                        "lease_token": lease_token,
                        "source_revision": source_revision,
                    },
                )
            ).first()
        if row is None:
            return CatalogPagedRefreshPreparation("invalid")
        outcome = str(row.outcome)
        if outcome != "ready":
            return CatalogPagedRefreshPreparation(outcome)
        return CatalogPagedRefreshPreparation(
            outcome,
            CatalogPagedRefreshProgress(
                source_key=source_key,
                run_key=run_key,
                source_revision=row.progress_source_revision,
                window_start_day=row.window_start_day,
                next_page=row.next_page,
                page_limit=row.page_limit,
                terminal_page=row.terminal_page,
                staged_raw_count=row.staged_raw_count,
                staged_candidate_count=row.staged_candidate_count,
            ),
        )

    async def stage_paged_page(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        source_revision: int,
        page: CatalogSourcePage,
    ) -> CatalogPagedStageResult:
        """Persist one P15b normalized page without giving the app role table DML (NFR-1/NFR-8)."""
        candidates = [_staged_candidate_payload(candidate) for candidate in page.candidates]
        async with system_session_scope() as session:
            outcome = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_stage_paged_catalog_refresh_page_v4(
                            :source_key,
                            :run_key,
                            :lease_token,
                            :source_revision,
                            :page_number,
                            :raw_count,
                            CAST(:candidates AS jsonb),
                            CAST(:source_event_ids AS jsonb)
                        ) AS outcome
                        """
                    ),
                    {
                        "source_key": source_key,
                        "run_key": run_key,
                        "lease_token": lease_token,
                        "source_revision": source_revision,
                        "page_number": page.page_number,
                        "raw_count": page.raw_count,
                        "candidates": json.dumps(candidates, separators=(",", ":")),
                        "source_event_ids": json.dumps(
                            page.source_event_ids, separators=(",", ":")
                        ),
                    },
                )
            ).scalar_one()
        return CatalogPagedStageResult(str(outcome))

    async def pause_paged_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        """Release a P15b lease while retaining its cursor for Temporal re-entry (ADR-003/005)."""
        async with system_session_scope() as session:
            paused = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_pause_paged_catalog_refresh(
                            :source_key, :run_key, :lease_token, :error
                        ) AS paused
                        """
                    ),
                    {
                        "source_key": source_key,
                        "run_key": run_key,
                        "lease_token": lease_token,
                        "error": error[:2_000],
                    },
                )
            ).scalar_one()
        return bool(paused)

    async def abort_paged_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        """Discard a nonterminal P15b stage only through a lease-fenced abort capability (NFR-8)."""
        async with system_session_scope() as session:
            aborted = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_abort_paged_catalog_refresh(
                            :source_key, :run_key, :lease_token, :error
                        ) AS aborted
                        """
                    ),
                    {
                        "source_key": source_key,
                        "run_key": run_key,
                        "lease_token": lease_token,
                        "error": error[:2_000],
                    },
                )
            ).scalar_one()
        return bool(aborted)


def _source_from_row(row: Row[Any]) -> CatalogSource:
    return CatalogSource(
        source_key=row.source_key,
        display_name=row.display_name,
        publisher=row.publisher,
        seed_url=row.seed_url,
        approved_origins=tuple(str(origin) for origin in row.approved_origins),
        region=row.region,
        mode=CatalogSourceMode(row.mode),
        enabled=row.enabled,
        reviewed_at=row.reviewed_at,
        review_expires_at=row.review_expires_at,
        refresh_interval_minutes=row.refresh_interval_minutes,
        min_interval_ms=row.min_interval_ms,
        page_limit=row.page_limit,
        handoff_only=row.handoff_only,
        # ``fn_list_due_catalog_refreshes`` predates source revisions.  A due record is routed
        # through a fresh ``get`` before P15b egress, so its historic projection safely defaults
        # to one instead of requiring a signature-changing capability migration (FR-10.3/NFR-8).
        source_revision=getattr(row, "source_revision", 1),
    )


def _run_from_row(row: Row[Any]) -> CatalogRefreshRun:
    return CatalogRefreshRun(
        source_key=row.source_key,
        run_key=row.run_key,
        status=CatalogRefreshRunStatus(row.status),
        started_at=row.started_at,
        lease_expires_at=row.lease_expires_at,
        completed_at=row.completed_at,
        candidate_count=row.candidate_count,
        canonical_count=row.canonical_count,
        error=row.error,
        attempt_count=row.attempt_count,
    )


def _staged_candidate_payload(candidate: CandidateEvent) -> dict[str, object]:
    """Serialize only P15b's normalized candidate fields, deliberately excluding ``raw`` (NFR-1)."""
    return {
        "source": candidate.source.value,
        "source_event_id": candidate.source_event_id,
        "title": candidate.title,
        "start_at": candidate.start_at.isoformat(),
        "end_at": candidate.end_at.isoformat() if candidate.end_at is not None else None,
        "registration_url": candidate.registration_url,
        "venue_name": candidate.venue_name,
        "city": candidate.city,
        "description": candidate.description,
        "price_status": candidate.price_status.value,
        "price_min_cents": candidate.price_min_cents,
        "price_max_cents": candidate.price_max_cents,
        "price_currency": candidate.price_currency,
        "organizer_name": candidate.organizer_name,
        "host_names": list(candidate.host_names),
        "speaker_names": list(candidate.speaker_names),
        "partner_names": list(candidate.partner_names),
        "entity_profiles": event_entity_profiles_payload(candidate.entity_profiles),
        "attendance_count": candidate.attendance_count,
        "registration_status": candidate.registration_status.value,
        "content_hash": catalog_candidate_content_hash(candidate),
    }
