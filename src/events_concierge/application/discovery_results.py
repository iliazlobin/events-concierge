"""Request-sensitive presentation of recommendations; canonical rows remain independent.

Grouping is deliberately conservative: exact normalized title, named organizer, physical venue,
city, and source hosts must agree. This is a presentation group, never a canonical merge.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from ..domain.enums import EventStatus
from ..domain.events import CanonicalEvent
from ..domain.request import RequestConstraints

FRESHNESS_WINDOW = timedelta(days=7)


def lifecycle(event: CanonicalEvent, now: datetime) -> str:
    if event.event_status == EventStatus.CANCELLED:
        return "cancelled"
    if event.start_at.tzinfo is None or (
        event.end_at is not None and (event.end_at.tzinfo is None or event.end_at <= event.start_at)
    ):
        return "uncertain"
    if (event.end_at or event.start_at) <= now:
        return "past"
    return "ongoing" if event.start_at <= now else "upcoming"


def freshness(event: CanonicalEvent, now: datetime) -> str:
    observations = [link.last_seen_at for link in event.source_links if link.last_seen_at]
    if not observations:
        return "unknown"
    if any(value.tzinfo is None for value in observations):
        return "unknown"
    latest = max(observations)
    if latest > now:
        return "unknown"
    return "stale" if now - latest > FRESHNESS_WINDOW else "recent"


def eligible(event: CanonicalEvent, constraints: RequestConstraints, now: datetime) -> bool:
    state = lifecycle(event, now)
    if state in {"cancelled", "uncertain"}:
        return False
    window = constraints.time_window
    if window is None:
        return state != "past"
    return (event.end_at or event.start_at) > window.start and event.start_at < window.end


def _text(value: str | None) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().casefold())


def series_key(event: CanonicalEvent) -> tuple[str, ...]:
    title, organizer, venue, city = map(
        _text, (event.title, event.organizer_name, event.venue_name, event.city_norm)
    )
    if not all((title, organizer, venue, city)):
        return (str(event.canonical_event_id),)
    hosts = tuple(
        sorted({urlsplit(link.registration_url).hostname or "" for link in event.source_links})
    )
    return (
        title,
        organizer,
        venue,
        city,
        _text(event.description),
        event.price_status.value,
        *hosts,
    )


def _quality(event: CanonicalEvent, constraints: RequestConstraints, now: datetime) -> float:
    """Bounded tie-break features; never infer missing prices, coordinates or verification."""
    value = 0.0
    if event.venue_name or event.geo:
        value += 0.1
    if event.price_status.value != "unknown":
        value += 0.1
    if event.end_at:
        value += 0.1
    if event.topics:
        value += 0.1
    if freshness(event, now) == "recent":
        value += 0.2
    # Ongoing sessions and near-term choices receive a small timing preference.
    days = max(0.0, (event.start_at - now).total_seconds() / 86400)
    value += 0.1 / (1 + days)
    if constraints.geo and event.geo:
        center = constraints.geo.center
        lat1, lat2 = math.radians(center.lat), math.radians(event.geo.lat)
        dlat = lat2 - lat1
        dlon = math.radians(event.geo.lon - center.lon)
        hav = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        km = 6371 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, hav))))
        value += 0.2 / (1 + km)
    return value


def arrange(
    ranked: list[tuple[CanonicalEvent, float]],
    constraints: RequestConstraints,
    *,
    now: datetime | None = None,
) -> list[tuple[CanonicalEvent, float, tuple[CanonicalEvent, ...]]]:
    """Group sessions and gently diversify near-relevance neighbors, without category quotas.

    Explicit category requests bypass diversity penalties. Broad requests select within a
    five-position relevance neighborhood, so diversification cannot pull arbitrary tail results
    ahead of high-relevance candidates. Unknown organizers never form a shared penalty bucket.
    """
    now = now or datetime.now(UTC)
    # Adjust by at most a few percent of the observed score range. Interest relevance stays
    # dominant; zero-range scores still admit small, deterministic quality tie-breaks.
    finite = [(event, score) for event, score in ranked if math.isfinite(score)]
    scale = max(
        (max(score for _, score in finite) - min(score for _, score in finite)) if finite else 0,
        0.01,
    )
    scored = [
        (event, score + 0.05 * scale * _quality(event, constraints, now)) for event, score in finite
    ]
    scored.sort(key=lambda row: -row[1])
    groups: dict[tuple[str, ...], list[tuple[CanonicalEvent, float]]] = {}
    for event, score in scored:
        groups.setdefault(series_key(event), []).append((event, score))
    pending = []
    for members in groups.values():
        # Keep the highest ranked representative; alternatives keep their own IDs and dates.
        event, score = members[0]
        alternatives = tuple(
            sorted(
                (e for e, _ in members[1:]), key=lambda e: (e.start_at, str(e.canonical_event_id))
            )
        )
        pending.append((event, score, alternatives))
    if constraints.categories or constraints.hard_filters:
        return pending
    result = []
    organizers: Counter[str] = Counter()
    topics: Counter[str] = Counter()
    while pending:

        def cost(index: int) -> float:
            event = pending[index][0]
            organizer = _text(event.organizer_name)
            repeated = organizers[organizer] if organizer else 0
            topic_repeat = min((topics[t] for t in event.topics), default=0)
            stale = 1 if freshness(event, now) == "stale" else 0
            # Positional relevance remains the dominant signal; diversity breaks close choices.
            return index + 1.5 * repeated + 0.5 * topic_repeat + stale

        index = min(range(min(5, len(pending))), key=cost)
        item = pending.pop(index)
        result.append(item)
        if organizer := _text(item[0].organizer_name):
            organizers[organizer] += 1
        topics.update(item[0].topics)
    return result
