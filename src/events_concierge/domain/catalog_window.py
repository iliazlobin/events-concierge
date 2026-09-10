"""Use the run's frozen collection interval without changing provider authority.

The registry horizon bounds event starts, not retention. Adapters may push these dates into an
already-reviewed provider query; every publication path still applies the same half-open interval.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, tzinfo

from .catalog_sources import CatalogSource
from .events import CandidateEvent


def collection_reference_time(source: CatalogSource, now: datetime) -> datetime:
    """Keep normalization stable when a durable run retries after the worker clock advances."""
    return source.collection_window.start_at if source.collection_window is not None else now


def collection_end_at(source: CatalogSource, default_end: datetime) -> datetime:
    """Prefer the database-frozen upper bound over an adapter's legacy local-date calculation."""
    return source.collection_window.end_at if source.collection_window is not None else default_end


def collection_end_day(source: CatalogSource, default_end: date, zone: tzinfo) -> date:
    """Ceil an exact upper bound for APIs with an exclusive local-midnight date predicate.

    A query may collect extra records in its last day; the exact candidate guard removes them.
    Flooring a partial day would silently miss valid occurrences, especially across DST changes.
    """
    if source.collection_window is None:
        return default_end
    local_end = source.collection_window.end_at.astimezone(zone)
    return local_end.date() + timedelta(days=int(local_end.time() != time.min))


def in_collection_window(source: CatalogSource, candidate: CandidateEvent) -> bool:
    """Accept starts in [start, end); absent runtime context preserves standalone adapter behavior."""
    window = source.collection_window
    return window is None or window.start_at <= candidate.start_at < window.end_at


def filter_collection_window(
    source: CatalogSource, candidates: list[CandidateEvent],
) -> list[CandidateEvent]:
    """Filter normalized candidates without deleting old catalog observations or inventing totals."""
    return [candidate for candidate in candidates if in_collection_window(source, candidate)]
