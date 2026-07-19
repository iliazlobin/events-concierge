"""Pure, conservative calendar duplicate matching (FR-9.3).

Calendar writes prefer the canonical private-property key.  This module supplies only the
secondary match used when a person created the calendar event by hand, so it deliberately refuses
to score approximate titles or venues: one normalized title, a start within 15 minutes, and one
shared venue token are all required.  Callers must treat more than one result as ambiguous rather
than selecting an arbitrary calendar event (FR-9.3; ADR-003's no-blind-mutation posture).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from .dedup import normalize_title

FUZZY_CALENDAR_START_DELTA = timedelta(minutes=15)
_VENUE_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class CalendarFuzzyCandidate:
    """The minimum non-provider-specific shape needed for FR-9.3's secondary match."""

    provider_event_id: str
    title: str
    start_at: datetime
    location: str | None

    def __post_init__(self) -> None:
        if not self.provider_event_id.strip():
            raise ValueError("calendar fuzzy candidate provider_event_id must not be empty")
        if self.start_at.tzinfo is None or self.start_at.utcoffset() is None:
            raise ValueError("calendar fuzzy candidate start_at must be timezone-aware")


def fuzzy_calendar_matches(
    *,
    title: str,
    start_at: datetime,
    location: str | None,
    candidates: Iterable[CalendarFuzzyCandidate],
) -> tuple[CalendarFuzzyCandidate, ...]:
    """Return every conservative fuzzy match, leaving ambiguity resolution to the caller.

    The caller cannot safely choose one of multiple matches because that would patch or delete a
    user-authored event without a canonical key.  Returning every match makes fail-closed handling
    explicit at the adapter boundary (FR-9.3).
    """
    if start_at.tzinfo is None or start_at.utcoffset() is None:
        raise ValueError("calendar fuzzy match start_at must be timezone-aware")
    normalized_title = normalize_title(title)
    venue_tokens = _venue_tokens(location)
    if not normalized_title or not venue_tokens:
        return ()

    return tuple(
        candidate
        for candidate in candidates
        if normalize_title(candidate.title) == normalized_title
        and abs(candidate.start_at - start_at) <= FUZZY_CALENDAR_START_DELTA
        and bool(venue_tokens & _venue_tokens(candidate.location))
    )


def _venue_tokens(location: str | None) -> frozenset[str]:
    if location is None:
        return frozenset()
    return frozenset(_VENUE_TOKEN.findall(location.lower()))
