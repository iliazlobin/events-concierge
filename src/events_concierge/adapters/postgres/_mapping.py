"""Row <-> domain translation helpers shared by the Postgres repositories."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Row

from ...domain.enums import EventStatus, PriceStatus, Source
from ...domain.events import CanonicalEvent, EventSourceLink, GeoPoint


def vector_literal(vec: list[float]) -> str:
    """pgvector text form, bound as a string and cast ::vector in SQL (no type registration needed)."""
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"


def canonical_from_row(row: Row[Any], links: list[EventSourceLink]) -> CanonicalEvent:
    geo = GeoPoint(row.lat, row.lon) if row.lat is not None and row.lon is not None else None
    return CanonicalEvent(
        canonical_event_id=row.canonical_event_id,
        title=row.title,
        start_at=row.start_at,
        end_at=row.end_at,
        venue_name=row.venue_name,
        geo=geo,
        city_norm=row.city_norm,
        event_status=EventStatus(row.event_status),
        description=row.description,
        price_status=PriceStatus(row.price_status),
        normalizer_version=row.normalizer_version,
        merge_version=row.merge_version,
    )


def link_from_row(row: Row[Any]) -> EventSourceLink:
    return EventSourceLink(
        source=Source(row.source),
        source_event_id=row.source_event_id,
        registration_url=row.registration_url,
        last_seen_at=row.last_seen_at,
        price_status=PriceStatus(row.price_status),
    )
