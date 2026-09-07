"""PostgreSQL write adapter for source-published social links in the entity enrichment plane.

A social link never enters ``canonical_events.entity_profiles``: FR-19.13 keeps producer-verified
entity-profile metadata to a validated LinkedIn URL or an explicit organization website.  It is
written here instead, to ``catalog_entity_external_sources``, against the entity the catalog itself
already resolved for the mention.

``fn_record_catalog_entity_social_source_v1`` (``0178``) owns both halves of that: it validates the
URL and it resolves the entity from ``catalog_entity_event_mentions``.  This adapter therefore never
looks an entity up by name, and never falls back to creating one.  A link whose mention the catalog
has not resolved yet is simply not recorded on this pass -- see ``record`` for the ordering that
implies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.events import EventEntitySocialLink

# A social row is a link, not a fetched page, so it ages on the same 30-day cadence as a LinkedIn
# link rather than the 7-day cadence of a page we actually read.
_LINK_TTL = timedelta(days=30)
_MAX_LINKS_PER_EVENT = 128

_RECORD_SQL = text(
    """
    SELECT public.fn_record_catalog_entity_social_source_v1(
        :source_key, :source_event_id, :role, :observed_name, :social_url, :next_refresh_at
    ) AS recorded
    """
)


class CatalogEntitySocialLinkWriter(Protocol):
    async def record_in_session(
        self,
        session: AsyncSession,
        *,
        source_key: str,
        source_event_id: str,
        links: tuple[EventEntitySocialLink, ...],
    ) -> int:
        ...


class PostgresCatalogEntitySocialLinks:
    """Record the social profiles a source published about the roles it displayed."""

    async def record_in_session(
        self,
        session: AsyncSession,
        *,
        source_key: str,
        source_event_id: str,
        links: tuple[EventEntitySocialLink, ...],
    ) -> int:
        """Write every link whose mention the catalog has resolved; return how many landed.

        Ordering matters and is deliberate.  This runs inside the publication transaction, *after*
        ``fn_refresh_catalog_entity_index_v3`` has rebuilt this source's mentions, so the mention a
        link hangs on is already visible to this session and a link lands on the same pass that
        first sees the event.  A link whose mention the index did not resolve is simply not recorded
        -- recording nothing is always preferred to inventing an entity to hang a link on.
        """
        if not links:
            return 0
        next_refresh_at = datetime.now(UTC) + _LINK_TTL
        recorded = 0
        for link in links[:_MAX_LINKS_PER_EVENT]:
            row = (
                await session.execute(
                    _RECORD_SQL,
                    {
                        "source_key": source_key,
                        "source_event_id": source_event_id,
                        "role": link.role,
                        "observed_name": link.name,
                        "social_url": link.profile_url,
                        "next_refresh_at": next_refresh_at,
                    },
                )
            ).first()
            if row is not None and row.recorded:
                recorded += 1
        return recorded
