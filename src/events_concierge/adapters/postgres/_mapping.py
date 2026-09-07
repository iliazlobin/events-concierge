"""Row <-> domain translation helpers shared by the Postgres repositories."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Row

from ...domain.enums import EventStatus, PriceStatus, RegistrationStatus, Source
from ...domain.event_semantics import event_extraction_evidence_from_payload
from ...domain.events import (
    CanonicalEvent,
    EventSourceLink,
    GeoPoint,
    event_entity_profiles_from_payload,
)


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
        price_min_cents=getattr(row, "price_min_cents", None),
        price_max_cents=getattr(row, "price_max_cents", None),
        price_currency=getattr(row, "price_currency", None),
        normalizer_version=row.normalizer_version,
        merge_version=row.merge_version,
        organizer_name=getattr(row, "organizer_name", None),
        host_names=tuple(getattr(row, "host_names", ()) or ()),
        speaker_names=tuple(getattr(row, "speaker_names", ()) or ()),
        partner_names=tuple(getattr(row, "partner_names", ()) or ()),
        entity_profiles=event_entity_profiles_from_payload(
            getattr(row, "entity_profiles", []) or []
        ),
        attendance_count=getattr(row, "attendance_count", None),
        registration_status=RegistrationStatus(
            getattr(row, "registration_status", RegistrationStatus.UNKNOWN.value)
        ),
        topics=tuple(getattr(row, "topics", ()) or ()),
        extraction_evidence=event_extraction_evidence_from_payload(
            getattr(row, "extraction_evidence", []) or []
        ),
    )


def link_from_row(row: Row[Any]) -> EventSourceLink:
    return EventSourceLink(
        source=Source(row.source),
        source_event_id=row.source_event_id,
        registration_url=row.registration_url,
        last_seen_at=row.last_seen_at,
        price_status=PriceStatus(row.price_status),
        price_min_cents=getattr(row, "price_min_cents", None),
        price_max_cents=getattr(row, "price_max_cents", None),
        price_currency=getattr(row, "price_currency", None),
    )
