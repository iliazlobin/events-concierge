"""PostgreSQL source/run provenance for normalized public catalog observations (FR-3.8/NFR-1)."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.catalog_sources import CatalogSourceObservation
from ...domain.enums import PriceStatus, Source
from ...infra.db import system_session_scope


class PostgresCatalogObservationRepository:
    """Retains each reviewed source's first/last normalized observation of an event."""

    async def record(self, observations: list[CatalogSourceObservation], run_key: str) -> None:
        """Upsert source-event provenance after catalog dedup, retaining the last run key (FR-3.8)."""
        if not observations:
            return
        async with system_session_scope() as session:
            await self.record_in_session(session, observations, run_key)

    async def record_in_session(
        self,
        session: AsyncSession,
        observations: list[CatalogSourceObservation],
        run_key: str,
    ) -> None:
        """Record provenance in P15b's caller-owned promotion transaction (NFR-8)."""
        for observation in observations:
            await session.execute(
                text(
                    """
                    INSERT INTO catalog_event_observations
                        (source_key, source, source_event_id, canonical_event_id, registration_url,
                         price_status, content_hash, first_seen_at, last_seen_at, last_run_key)
                    VALUES
                        (:source_key, :source, :source_event_id, :canonical_event_id, :registration_url,
                         :price_status, :content_hash, now(), now(), :run_key)
                    ON CONFLICT (source_key, source, source_event_id) DO UPDATE SET
                        canonical_event_id = EXCLUDED.canonical_event_id,
                        registration_url = EXCLUDED.registration_url,
                        price_status = EXCLUDED.price_status,
                        content_hash = EXCLUDED.content_hash,
                        last_seen_at = now(),
                        last_run_key = EXCLUDED.last_run_key
                    """
                ),
                {
                    "source_key": observation.source_key,
                    "source": observation.source.value,
                    "source_event_id": observation.source_event_id,
                    "canonical_event_id": observation.canonical_event_id,
                    "registration_url": observation.registration_url,
                    "price_status": observation.price_status.value,
                    "content_hash": observation.content_hash,
                    "run_key": run_key,
                },
            )

    async def list_for_source(self, source_key: str) -> list[CatalogSourceObservation]:
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT * FROM catalog_event_observations
                        WHERE source_key = :source_key
                        ORDER BY last_seen_at, source_event_id
                        """
                    ),
                    {"source_key": source_key},
                )
            ).all()
        return [_observation_from_row(row) for row in rows]


def _observation_from_row(row: Row[Any]) -> CatalogSourceObservation:
    return CatalogSourceObservation(
        source_key=row.source_key,
        source=Source(row.source),
        source_event_id=row.source_event_id,
        canonical_event_id=row.canonical_event_id,
        registration_url=row.registration_url,
        price_status=PriceStatus(row.price_status),
        content_hash=row.content_hash,
        first_seen_at=row.first_seen_at,
        last_seen_at=row.last_seen_at,
        last_run_key=row.last_run_key,
    )
