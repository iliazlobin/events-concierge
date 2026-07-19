"""Event value objects: the raw CandidateEvent, the de-duplicated CanonicalEvent, and provenance."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from .enums import EventStatus, PriceStatus, Source


@dataclass(frozen=True, slots=True)
class GeoPoint:
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class EventSourceLink:
    """One source's registration surface for a CanonicalEvent. ALL links are retained on merge (FR-3.8)."""

    source: Source
    source_event_id: str
    registration_url: str
    last_seen_at: datetime | None = None
    price_status: PriceStatus = PriceStatus.UNKNOWN


@dataclass(frozen=True, slots=True)
class CandidateEvent:
    """One source's raw advertisement before normalization (FR-3.7).

    ``is_free`` remains the tri-state ACL compatibility field. ``price_status`` is the explicit
    free/paid/unknown value that is persisted to the canonical catalog, so an absent price is never
    silently treated as free.
    """

    source: Source
    source_event_id: str
    title: str
    start_at: datetime
    registration_url: str
    end_at: datetime | None = None
    venue_name: str | None = None
    geo: GeoPoint | None = None
    city: str | None = None
    description: str = ""
    is_free: bool | None = None
    raw: dict[str, object] = field(default_factory=dict)
    price_status: PriceStatus = PriceStatus.UNKNOWN

    def __post_init__(self) -> None:
        """Reconcile legacy ACL output with the explicit pricing invariant (FR-3.7/FR-4.6)."""
        inferred = PriceStatus.from_is_free(self.is_free)
        explicit = PriceStatus(self.price_status)
        if (
            self.is_free is not None
            and explicit is not PriceStatus.UNKNOWN
            and explicit is not inferred
        ):
            raise ValueError("is_free and price_status disagree")
        resolved = inferred if self.is_free is not None else explicit
        object.__setattr__(self, "price_status", resolved)
        object.__setattr__(self, "is_free", resolved.is_free)


@dataclass(slots=True)
class CanonicalEvent:
    """De-duplicated schema.org/Event record. Ranking, registration selection, and the calendar
    idempotency key all operate on this (definition, section 3; FR-3.8)."""

    canonical_event_id: UUID
    title: str
    start_at: datetime
    end_at: datetime | None = None
    venue_name: str | None = None
    geo: GeoPoint | None = None
    city_norm: str | None = None
    event_status: EventStatus = EventStatus.SCHEDULED
    description: str = ""
    price_status: PriceStatus = PriceStatus.UNKNOWN
    embedding: list[float] | None = None
    source_links: list[EventSourceLink] = field(default_factory=list)
    normalizer_version: int = 1
    merge_version: int = 1

    def registration_urls(self) -> list[str]:
        return [link.registration_url for link in self.source_links]

    def has_source(self, source: Source) -> bool:
        return any(link.source is source for link in self.source_links)

    @property
    def is_free(self) -> bool | None:
        """Compatibility view; catalog filtering must use ``price_status`` (FR-4.6)."""
        return self.price_status.is_free


def aggregate_price_status(observations: Iterable[PriceStatus]) -> PriceStatus:
    """Return a price only when every current source observation agrees (FR-3.7/FR-5.10).

    A free/paid conflict, a missing offer alongside a known offer, or no observations is
    ``UNKNOWN``.  This makes the aggregate safe for the execution-time free-only guard: only a
    fully verified set of source links can become ``FREE``.
    """
    statuses = frozenset(observations)
    if len(statuses) == 1:
        return next(iter(statuses))
    return PriceStatus.UNKNOWN


def merge_price_status(existing: PriceStatus, observed: PriceStatus) -> PriceStatus:
    """Conservative pairwise compatibility helper (FR-3.7/FR-5.10).

    Catalog persistence aggregates all retained source links through
    :func:`aggregate_price_status`; this helper intentionally never promotes an unknown partial
    observation to free or paid.
    """
    return aggregate_price_status((existing, observed))
