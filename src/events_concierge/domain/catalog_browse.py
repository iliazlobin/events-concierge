"""Chronological, source-grounded catalog browse value objects.

This read model is intentionally separate from the personalized feed.  Requested windows use
half-open event-interval overlap, so an event remains live until its recorded end.  Ongoing and
future items are attached only to a reviewed source's latest successful refresh.  Explicit past
windows may also use a retained last-known observation after a fully ended identity rolls off that
projection.  The canonical record remains latest known state; this model is not version history.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal
from uuid import UUID

from .enums import Source
from .events import CanonicalEvent

CatalogBrowseSort = Literal["soonest", "latest"]


@dataclass(frozen=True, slots=True)
class CatalogBrowseCursor:
    """The exclusive time/id key after which the selected browse order continues."""

    start_at: datetime
    canonical_event_id: UUID

    def __post_init__(self) -> None:
        if self.start_at.tzinfo is None or self.start_at.utcoffset() is None:
            raise ValueError("catalog browse cursor timestamp must be timezone-aware")


@dataclass(frozen=True, slots=True)
class CatalogBrowseSource:
    """One admitted current or retained last-known observation attached to an event."""

    source_key: str
    label: str
    publisher: str
    provider: str
    seed_url: str
    source: Source
    source_event_id: str
    registration_url: str
    last_seen_at: datetime
    refresh_run_key: str


@dataclass(frozen=True, slots=True)
class CatalogBrowseEvent:
    """Latest-known canonical state plus eligible non-fixture source observations."""

    canonical_event: CanonicalEvent
    sources: tuple[CatalogBrowseSource, ...]


@dataclass(frozen=True, slots=True)
class CatalogBrowseProvider:
    """One admitted source facet and its in-range count, which may be zero."""

    source_key: str
    display_name: str
    publisher: str
    provider: str
    seed_url: str
    event_count: int

    def __post_init__(self) -> None:
        if self.event_count < 0:
            raise ValueError("catalog browse provider event_count cannot be negative")


@dataclass(frozen=True, slots=True)
class CatalogBrowseCity:
    """One normalized city in the admitted upcoming catalog."""

    city: str
    event_count: int

    def __post_init__(self) -> None:
        if not self.city.strip():
            raise ValueError("catalog browse city cannot be blank")
        if self.event_count < 1:
            raise ValueError("catalog browse city event_count must be positive")


@dataclass(frozen=True, slots=True)
class CatalogBrowseTopic:
    """One stable, non-sensitive topic facet and its pre-topic-filter result count."""

    topic: str
    label: str
    event_count: int

    def __post_init__(self) -> None:
        if self.event_count < 0:
            raise ValueError("catalog browse topic event_count cannot be negative")


@dataclass(frozen=True, slots=True)
class CatalogBrowseDayTopic:
    """One topic bucket inside a local calendar day, counted in distinct events."""

    topic: str
    label: str
    event_count: int

    def __post_init__(self) -> None:
        if not self.topic.strip():
            raise ValueError("catalog browse day topic cannot be blank")
        if self.event_count < 1:
            raise ValueError("catalog browse day topic event_count must be positive")


@dataclass(frozen=True, slots=True)
class CatalogBrowseDay:
    """One local calendar day of the filtered catalog, as counts rather than events.

    ``event_count`` counts distinct events, so it is not the sum of ``topics``: an event carrying
    several topics is counted once here and once per topic there.  Events with no topics are
    counted under the synthetic ``other`` bucket, matching the consumer calendar's taxonomy.
    """

    start_day: date
    event_count: int
    topics: tuple[CatalogBrowseDayTopic, ...]

    def __post_init__(self) -> None:
        if self.event_count < 1:
            raise ValueError("catalog browse day event_count must be positive")
        if any(topic.event_count > self.event_count for topic in self.topics):
            raise ValueError("catalog browse day topic cannot exceed the day event_count")
