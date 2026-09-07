"""Atomic generic catalog-refresh publication behind its lease-fenced port (NFR-8).

The one-shot public-source path must never leave canonical rows or provenance committed when its
final refresh-run completion loses authority.  Model work is intentionally done before this
transaction; only durable catalog effects share the final database-clock guard (FR-3.8/FR-10.3,
ADR-001/003).
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text

from ...domain.catalog_sources import CatalogRefreshCommit, observation_for
from ...domain.events import CandidateEvent
from ...infra.db import system_session_scope
from .catalog import PostgresCatalogRepository
from .catalog_entity_social_links import CatalogEntitySocialLinkWriter
from .catalog_observations import PostgresCatalogObservationRepository


class _CatalogRefreshLeaseLostError(Exception):
    """Signal a false final capability result so the surrounding transaction rolls back."""


class PostgresCatalogRefreshCommitter:
    """Commit generic catalog effects and run success together (FR-3.8/FR-10.3, NFR-8)."""

    def __init__(
        self,
        catalog: PostgresCatalogRepository,
        observations: PostgresCatalogObservationRepository,
        social_links: CatalogEntitySocialLinkWriter | None = None,
    ) -> None:
        self._catalog = catalog
        self._observations = observations
        self._social_links = social_links

    async def commit_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        candidates: list[CandidateEvent],
    ) -> CatalogRefreshCommit | None:
        """Publish exactly one current-lease refresh or roll all writes back on a stale token.

        Embeddings can use a provisioned boundary, so they are deliberately computed before the
        catalog transaction.  The last action inside that transaction is the existing live-lease
        capability; a false result raises privately to make the context manager roll back canonical
        rows, source links, and observation provenance together (NFR-8, ADR-001/003).
        """
        vectors = await self._catalog.embed_candidates(candidates)
        try:
            async with system_session_scope() as session:
                canonical_events = await self._catalog.upsert_candidates_in_session(
                    session, candidates, vectors
                )
                observations = [
                    observation_for(source_key, candidate, event.canonical_event_id)
                    for candidate, event in zip(candidates, canonical_events, strict=True)
                ]
                await self._observations.record_in_session(session, observations, run_key)
                canonical_count = len({event.canonical_event_id for event in canonical_events})
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
                            "candidate_count": len(candidates),
                            "canonical_count": canonical_count,
                        },
                    )
                ).scalar_one()
                if not completed:
                    raise _CatalogRefreshLeaseLostError
                # Rebuild only this source's public entity mentions after the refresh becomes the
                # admitted latest-success projection.  Keeping this in the publication transaction
                # prevents the entity explorer from ever observing a newer event catalog with a
                # stale role index.
                await session.execute(
                    text("SELECT public.fn_refresh_catalog_entity_index_v3(:source_key)"),
                    {"source_key": source_key},
                )
                await session.execute(
                    text("SELECT public.fn_prune_catalog_entity_index_v1(:source_key)"),
                    {"source_key": source_key},
                )
                # Social profiles the source published about the roles it displayed enter the
                # separate enrichment plane, never `canonical_events.entity_profiles`: FR-19.13
                # keeps the renderer's producer-verified metadata to LinkedIn and explicit
                # websites.  This runs after the entity index rebuild above so every link hangs on
                # a mention this transaction has already resolved -- no entity is invented here,
                # and a link whose mention did not resolve is dropped rather than guessed at.
                if self._social_links is not None:
                    for candidate in candidates:
                        if not candidate.entity_social_links:
                            continue
                        await self._social_links.record_in_session(
                            session,
                            source_key=source_key,
                            source_event_id=candidate.source_event_id,
                            links=candidate.entity_social_links,
                        )
        except _CatalogRefreshLeaseLostError:
            return None
        return CatalogRefreshCommit(len(candidates), canonical_count)
