"""Catalog rows -> compact model-facing cards.

Two jobs, and the second one matters more than the first.

**Cost.** A ``CatalogBrowseItemOut`` measures about 3 KB; ten of them are roughly 9.7k tokens.
The same ten events as cards are about 2 KB total. A three-tool turn goes from ~70k input
tokens to ~24k, which is the difference between a workable agent and an expensive one.

**Honesty.** About 98% of upcoming events publish no organizer, no attendance, and no
registration status -- the catalog is overwhelmingly library, civic, and university calendars
that publish a title, a time, and a place. A card therefore **omits an unpublished field
entirely** and names it in ``unknown``, rather than emitting ``"by": null``. A null invites a
model to fill it; an explicit absence plus a prompt rule does not. This is the projection half
of the grounding contract -- the prompt half cannot hold on its own.

Event ``description`` is never projected. It is scraped third-party HTML, and keeping it out
of the model's context means untrusted free text has no route into the reasoning loop at all.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ...domain.catalog_browse import CatalogBrowseDay, CatalogBrowseEvent
from ...domain.enums import PriceStatus, RegistrationStatus

DEFAULT_TIME_ZONE = ZoneInfo("America/Los_Angeles")

_SIGNUP_BY_REGISTRATION_STATUS = {
    RegistrationStatus.OPEN: "open",
    RegistrationStatus.WAITLIST: "waitlist",
    RegistrationStatus.SOLD_OUT: "sold_out",
}


def _money(cents: int | None, currency: str | None) -> str | None:
    if cents is None:
        return None
    symbol = "$" if (currency or "USD") == "USD" else f"{currency} "
    return f"{symbol}{cents / 100:.0f}" if cents % 100 == 0 else f"{symbol}{cents / 100:.2f}"


def _price_label(event: CatalogBrowseEvent) -> str:
    canonical = event.canonical_event
    if canonical.price_status is PriceStatus.FREE:
        return "free"
    if canonical.price_status is not PriceStatus.PAID:
        return "unknown"
    low = _money(canonical.price_min_cents, canonical.price_currency)
    high = _money(canonical.price_max_cents, canonical.price_currency)
    if low and high and low != high:
        return f"{low}-{high}"
    return low or high or "paid"


def render_when(start: datetime, end: datetime | None, tz: ZoneInfo = DEFAULT_TIME_ZONE) -> str:
    """One pre-rendered local-time string, so the model never formats a time itself.

    Models are unreliable at timezone arithmetic and there is no reason to make them try: the
    server knows the zone and the offset. The prompt instructs the model to quote this verbatim.
    """
    local_start = start.astimezone(tz)
    stamp = local_start.strftime("%a %b %-d, %-I:%M %p")
    if end is not None:
        local_end = end.astimezone(tz)
        if local_end.date() == local_start.date():
            return f"{stamp}-{local_end.strftime('%-I:%M %p')} {local_start.strftime('%Z')}"
    return f"{stamp} {local_start.strftime('%Z')}"


def event_card(
    event: CatalogBrowseEvent,
    ref: str,
    *,
    tz: ZoneInfo = DEFAULT_TIME_ZONE,
) -> dict[str, Any]:
    """Project one catalog row into the compact card the model reads.

    ``ref`` is the short server-minted handle ("E3") the model uses to name this event to a
    later tool. Raw UUIDs are deliberately withheld: a model that never sees an identifier
    cannot be talked into acting on one it read out of scraped listing text.
    """
    canonical = event.canonical_event
    unknown: list[str] = []
    card: dict[str, Any] = {
        "ref": ref,
        "title": canonical.title,
        "day": canonical.start_at.astimezone(tz).strftime("%Y-%m-%d"),
        "when": render_when(canonical.start_at, canonical.end_at, tz),
    }

    where = ", ".join(
        part for part in (canonical.venue_name, _city_label(canonical.city_norm)) if part
    )
    if where:
        card["where"] = where
    else:
        unknown.append("where")

    price = _price_label(event)
    if price == "unknown":
        unknown.append("price")
    else:
        card["price"] = price

    if canonical.topics:
        card["topics"] = list(canonical.topics)
    else:
        unknown.append("topics")

    host = canonical.organizer_name or (canonical.host_names[0] if canonical.host_names else None)
    if host:
        card["by"] = host
    else:
        unknown.append("by")

    if canonical.attendance_count is not None:
        # Named for what it is. A key called "attendance" was read by a model as remaining
        # capacity -- it answered "20 spots" for an event with 20 people already signed up.
        card["already_going"] = canonical.attendance_count
    else:
        unknown.append("already_going")

    signup = _SIGNUP_BY_REGISTRATION_STATUS.get(canonical.registration_status)
    if signup is not None:
        card["signup"] = signup

    if canonical.event_status.value != "scheduled":
        card["status"] = canonical.event_status.value

    if event.sources:
        card["source"] = event.sources[0].source_key

    card["unknown"] = unknown
    return card


def _city_label(city_norm: str | None) -> str | None:
    if not city_norm:
        return None
    return city_norm.replace("_", " ").replace("-", " ").title()


def coverage_block(events: list[CatalogBrowseEvent]) -> dict[str, int]:
    """Per-result-set field counts.

    Global coverage averages are misleading because coverage is strongly topic-dependent: host
    data sits near 2% catalog-wide but near 90% among upcoming AI events in San Francisco. The
    model needs the numbers for *this* result, so it can caveat the slice in front of it rather
    than reciting an average that does not apply.
    """
    total = len(events)
    return {
        "total": total,
        "by": sum(
            1 for e in events if e.canonical_event.organizer_name or e.canonical_event.host_names
        ),
        "price": sum(
            1 for e in events if e.canonical_event.price_status is not PriceStatus.UNKNOWN
        ),
        "topics": sum(1 for e in events if e.canonical_event.topics),
    }


def day_rows(days: list[CatalogBrowseDay], *, top_topics: int = 3) -> list[dict[str, Any]]:
    """Project day buckets, keeping only the leading topics per day."""
    return [
        {
            "day": day.start_day.isoformat(),
            "count": day.event_count,
            "top_topics": [t.topic for t in day.topics[:top_topics]],
        }
        for day in days
    ]


def utc_window(
    date_from: str, date_to: str, tz: ZoneInfo = DEFAULT_TIME_ZONE
) -> tuple[datetime, datetime]:
    """Turn an inclusive pair of local calendar dates into the half-open UTC interval to query.

    A calendar day is local wall clock, so the window must be built in ``tz`` and converted --
    building it in UTC would move every Pacific evening event into the following day.
    """
    start = datetime.fromisoformat(date_from).replace(tzinfo=tz)
    end_day = datetime.fromisoformat(date_to).replace(tzinfo=tz)
    end = end_day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return start.astimezone(UTC), end.astimezone(UTC)


def web_item(event: CatalogBrowseEvent) -> dict[str, Any]:
    """Project one catalog row into the consumer ``EventItem`` the web app already renders.

    Deliberately the FULL record, unlike :func:`event_card`. It never reaches the model -- it
    travels on ``ToolResult.render`` -- so the browser can reuse the same card component the
    Events, Map and Calendar views use, with its existing entity, topic and source affordances,
    instead of a second lookalike that would drift from it.
    """
    canonical = event.canonical_event
    return {
        "canonical_event_id": str(canonical.canonical_event_id),
        "title": canonical.title,
        "start_at": canonical.start_at.isoformat(),
        "end_at": canonical.end_at.isoformat() if canonical.end_at else None,
        "venue_name": canonical.venue_name,
        "city": canonical.city_norm,
        "description": canonical.description,
        "price_status": canonical.price_status.value,
        "price_min_cents": canonical.price_min_cents,
        "price_max_cents": canonical.price_max_cents,
        "price_currency": canonical.price_currency,
        "event_status": canonical.event_status.value,
        "latitude": canonical.geo.lat if canonical.geo else None,
        "longitude": canonical.geo.lon if canonical.geo else None,
        "score": None,
        "rationale": "",
        "conflict": "not_evaluated",
        "lanes": [],
        # A browse row carries its registration URL on the source OBSERVATION, not on the
        # canonical row's source links, which come back empty here. Read the observations first
        # and keep the canonical links as the fallback, or the card loses its Sign up link.
        "registration_urls": [
            url
            for url in (
                [source.registration_url for source in event.sources]
                + [link.registration_url for link in canonical.source_links]
            )
            if url
        ],
        "registerable": None,
        "organizer_name": canonical.organizer_name,
        "host_names": list(canonical.host_names),
        "speaker_names": list(canonical.speaker_names),
        "partner_names": list(canonical.partner_names),
        "entity_profiles": [
            {
                "role": profile.role,
                "name": profile.name,
                "profile_url": profile.profile_url,
                "kind": getattr(profile, "kind", None),
            }
            for profile in canonical.entity_profiles
        ],
        "attendance_count": canonical.attendance_count,
        "registration_status": canonical.registration_status.value,
        "providers": [source.provider for source in event.sources],
        "calendar_labels": [source.label for source in event.sources],
        "source_keys": [source.source_key for source in event.sources],
        "sources": [
            {
                "source_key": source.source_key,
                "label": source.label,
                "publisher": source.publisher,
                "provider": source.provider,
                "source": source.source.value,
                "registration_url": source.registration_url,
            }
            for source in event.sources
        ],
        "topics": list(canonical.topics),
        "extraction_evidence": [],
    }
