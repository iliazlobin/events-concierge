"""Atomic promotion of a P15b normalized Legistar stage into the public catalog.

The stage itself is readable only through a fixed database capability.  Canonical-event merge,
source provenance, and final run completion share one tenant-neutral transaction so a crash leaves
either the complete stage for retry or one committed catalog effect -- never a partial observation
ledger marked successful (FR-3.8/FR-10.3, NFR-1/NFR-8, ADR-001/003).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.catalog_sources import (
    CatalogPagedRefreshPromotion,
    catalog_candidate_content_hash,
    observation_for,
)
from ...domain.enums import PriceStatus, Source
from ...domain.events import CandidateEvent
from ...infra.db import system_session_scope
from .catalog import PostgresCatalogRepository
from .catalog_observations import PostgresCatalogObservationRepository


class PostgresCatalogPagedRefreshPromoter:
    """Promote only a terminal P15b stage through the same catalog merge implementation (NFR-8)."""

    def __init__(
        self,
        catalog: PostgresCatalogRepository,
        observations: PostgresCatalogObservationRepository,
    ) -> None:
        self._catalog = catalog
        self._observations = observations

    async def promote_paged_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        source_revision: int,
    ) -> CatalogPagedRefreshPromotion:
        """Commit a complete stage or raise so every catalog/provenance write rolls back (NFR-8)."""
        staged = await self._read_stage(source_key, run_key, lease_token, source_revision)
        vectors = await self._catalog.embed_candidates(staged)
        async with system_session_scope() as session:
            current = await self._read_stage_in_session(
                session, source_key, run_key, lease_token, source_revision
            )
            if _stage_fingerprint(staged) != _stage_fingerprint(current):
                raise RuntimeError("catalog paged refresh stage changed before promotion")
            canonical_events = await self._catalog.upsert_candidates_in_session(
                session, current, vectors
            )
            observations = [
                observation_for(source_key, candidate, canonical.canonical_event_id)
                for candidate, canonical in zip(current, canonical_events, strict=True)
            ]
            await self._observations.record_in_session(session, observations, run_key)
            canonical_count = len({event.canonical_event_id for event in canonical_events})
            promoted = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_promote_paged_catalog_refresh(
                            :source_key,
                            :run_key,
                            :lease_token,
                            :source_revision,
                            :candidate_count,
                            :canonical_count
                        ) AS promoted
                        """
                    ),
                    {
                        "source_key": source_key,
                        "run_key": run_key,
                        "lease_token": lease_token,
                        "source_revision": source_revision,
                        "candidate_count": len(current),
                        "canonical_count": canonical_count,
                    },
                )
            ).scalar_one()
            if not promoted:
                raise RuntimeError("catalog paged refresh stage could not be promoted")
        return CatalogPagedRefreshPromotion(len(current), canonical_count)

    async def _read_stage(
        self,
        source_key: str,
        run_key: str,
        lease_token: UUID,
        source_revision: int,
    ) -> list[CandidateEvent]:
        """Read terminal normalized rows before model work without exposing table privileges."""
        async with system_session_scope() as session:
            return await self._read_stage_in_session(
                session, source_key, run_key, lease_token, source_revision
            )

    @staticmethod
    async def _read_stage_in_session(
        session: AsyncSession,
        source_key: str,
        run_key: str,
        lease_token: UUID,
        source_revision: int,
    ) -> list[CandidateEvent]:
        """Read only a complete current-lease stage via the security-definer projection."""
        rows = (
            await session.execute(
                text(
                    """
                    SELECT * FROM public.fn_read_paged_catalog_refresh_stage(
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
        ).all()
        return [_candidate_from_stage_row(row) for row in rows]


def _candidate_from_stage_row(row: Row[Any]) -> CandidateEvent:
    """Recreate a raw-free candidate and verify its stored normalization hash (NFR-1)."""
    candidate = CandidateEvent(
        source=Source(row.source),
        source_event_id=row.source_event_id,
        title=row.title,
        start_at=row.start_at,
        end_at=row.end_at,
        registration_url=row.registration_url,
        venue_name=row.venue_name,
        city=row.city,
        description=row.description,
        price_status=PriceStatus(row.price_status),
    )
    if catalog_candidate_content_hash(candidate) != row.content_hash:
        raise RuntimeError(
            "catalog paged refresh stage content hash did not match its normalized row"
        )
    return candidate


def _stage_fingerprint(candidates: list[CandidateEvent]) -> tuple[tuple[str, str], ...]:
    """Compare re-read stage content without retaining provider raw data between transactions."""
    return tuple(
        (candidate.source_event_id, catalog_candidate_content_hash(candidate))
        for candidate in candidates
    )
