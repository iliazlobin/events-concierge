"""Anti-Corruption Layer: schema.org/Event JSON-LD -> CandidateEvent (FR-3.x, FR-8.7a).

Defensive by contract: every malformed <script>/object is skipped and logged, never raised, so one
bad block on a page can't sink the whole crawl."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from selectolax.parser import HTMLParser

from ...domain.enums import Source
from ...domain.events import (
    CandidateEvent,
    GeoPoint,
    aggregate_price_range,
    aggregate_price_status,
)
from ...infra.logging import get_logger

_log = get_logger("crawl.acl")

_FREE_PRICES = frozenset({"0", "0.0", "0.00", "free"})


def parse_jsonld(html: str, base_url: str) -> list[CandidateEvent]:
    """Extract every JSON-LD Event on the page and map it to a CandidateEvent.

    Tolerates a top-level list, a single object, or a `@graph` wrapper. Objects whose `@type` is not
    (or does not contain) "Event" are ignored."""
    out: list[CandidateEvent] = []
    tree = HTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(deep=True, strip=False)
        if not raw or not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except (ValueError, TypeError) as exc:
            _log.warning("jsonld_parse_failed", base_url=base_url, error=str(exc))
            continue
        for obj in _iter_objects(payload):
            candidate = _map_event(obj, base_url)
            if candidate is not None:
                out.append(candidate)
    return _deduplicate_source_ids(_disambiguate_source_ids(out))


def _disambiguate_source_ids(events: list[CandidateEvent]) -> list[CandidateEvent]:
    """Make reused listing-page URLs unique per event occurrence (FR-3.7/FR-3.8).

    Some publisher calendars legally expose the calendar homepage as the JSON-LD URL for many
    different events. A raw URL-only source ID would then overwrite unrelated source links and
    provenance rows. Normal event detail URLs remain untouched; only a repeated ID receives the
    deterministic name/start suffix.
    """
    counts = Counter(event.source_event_id for event in events)
    if all(count == 1 for count in counts.values()):
        return events
    return [
        (
            replace(
                event,
                source_event_id=f"{event.source_event_id}|{_occurrence_id(event)}",
            )
            if counts[event.source_event_id] > 1
            else event
        )
        for event in events
    ]


def _deduplicate_source_ids(events: list[CandidateEvent]) -> list[CandidateEvent]:
    """Collapse only exact repeated source occurrences, preserving the richest safe observation (FR-3.8)."""
    out: list[CandidateEvent] = []
    index_by_id: dict[str, int] = {}
    for event in events:
        index = index_by_id.get(event.source_event_id)
        if index is None:
            index_by_id[event.source_event_id] = len(out)
            out.append(event)
            continue
        existing = out[index]
        preferred = event if _metadata_score(event) > _metadata_score(existing) else existing
        price_status = aggregate_price_status((existing.price_status, event.price_status))
        price_range = (
            aggregate_price_range(
                (
                    (
                        existing.price_min_cents,
                        existing.price_max_cents,
                        existing.price_currency,
                    ),
                    (
                        event.price_min_cents,
                        event.price_max_cents,
                        event.price_currency,
                    ),
                )
            )
            if price_status.value == "paid"
            else (None, None, None)
        )
        out[index] = replace(
            preferred,
            is_free=price_status.is_free,
            price_status=price_status,
            price_min_cents=price_range[0],
            price_max_cents=price_range[1],
            price_currency=price_range[2],
        )
    return out


def _occurrence_id(event: CandidateEvent) -> str:
    """Hash fields that distinguish separately scheduled cards sharing a generic listing URL."""
    value = "|".join(
        (
            event.title,
            event.start_at.isoformat(),
            event.venue_name or "",
            event.city or "",
            event.registration_url,
        )
    )
    return f"jsonld:{hashlib.sha256(value.encode()).hexdigest()[:32]}"


def _metadata_score(event: CandidateEvent) -> tuple[int, bool, bool, bool, bool]:
    """Favor the normalized duplicate that carries the most useful non-price catalog fields."""
    return (
        len(event.description),
        event.end_at is not None,
        event.venue_name is not None,
        event.geo is not None,
        event.city is not None,
    )


def _iter_objects(payload: Any) -> list[dict[str, Any]]:
    """Flatten JSON-LD list/single/graph and schema.org ``ItemList`` event payloads.

    Public calendar pages commonly expose their upcoming events as
    ``ItemList.itemListElement[].item`` rather than a top-level ``Event``.  Descending through
    that explicit schema.org shape preserves the generic public-crawl boundary without treating
    arbitrary nested JSON as a candidate (FR-3.1/3.8).
    """
    objects: list[dict[str, Any]] = []
    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, dict):
            objects.append(item)
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)
            if _is_item_list(item):
                elements = item.get("itemListElement")
                if isinstance(elements, dict):
                    elements = [elements]
                if isinstance(elements, list):
                    for element in elements:
                        if isinstance(element, dict) and "item" in element:
                            stack.append(element["item"])
    return objects


def _is_event(obj: dict[str, Any]) -> bool:
    return _has_schema_type(obj, "Event")


def _is_item_list(obj: dict[str, Any]) -> bool:
    return _has_schema_type(obj, "ItemList")


def _has_schema_type(obj: dict[str, Any], expected: str) -> bool:
    type_val = obj.get("@type")
    if isinstance(type_val, str):
        return expected in type_val
    if isinstance(type_val, list):
        return any(isinstance(t, str) and expected in t for t in type_val)
    return False


def _map_event(obj: dict[str, Any], base_url: str) -> CandidateEvent | None:
    """Map one schema.org/Event dict to a CandidateEvent, or None if unusable."""
    try:
        if not _is_event(obj):
            return None
        name = obj.get("name")
        start_raw = obj.get("startDate")
        if not isinstance(name, str) or not name or not isinstance(start_raw, str):
            _log.warning("jsonld_event_incomplete", base_url=base_url)
            return None
        start_at = _parse_iso(start_raw)
        if start_at is None:
            _log.warning("jsonld_bad_start_date", base_url=base_url, value=start_raw)
            return None

        end_raw = obj.get("endDate")
        end_at = _parse_iso(end_raw) if isinstance(end_raw, str) else None

        url = obj.get("url")
        registration_url = url if isinstance(url, str) and url else base_url
        source_event_id = url if isinstance(url, str) and url else _stable_id(name, start_raw)

        venue_name, geo, city = _parse_location(obj.get("location"))
        description = obj.get("description")
        if not isinstance(description, str):
            description = ""

        return CandidateEvent(
            source=Source.PUBLIC_JSONLD,
            source_event_id=source_event_id,
            title=name,
            start_at=start_at,
            registration_url=registration_url,
            end_at=end_at,
            venue_name=venue_name,
            geo=geo,
            city=city,
            description=description,
            is_free=_parse_is_free(obj.get("offers")),
        )
    except Exception as exc:  # never let one object abort the crawl
        _log.warning("jsonld_map_failed", base_url=base_url, error=str(exc))
        return None


def _parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 timestamp to an aware datetime (assume UTC when naive)."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def _parse_location(
    location: Any,
) -> tuple[str | None, GeoPoint | None, str | None]:
    """Extract (venue_name, geo, city) from a schema.org location (first entry if a list)."""
    if isinstance(location, list):
        location = location[0] if location else None
    if not isinstance(location, dict):
        return None, None, None

    name = location.get("name")
    venue_name = name if isinstance(name, str) and name else None

    geo = None
    geo_obj = location.get("geo")
    if isinstance(geo_obj, dict):
        lat = _as_float(geo_obj.get("latitude"))
        lon = _as_float(geo_obj.get("longitude"))
        if lat is not None and lon is not None:
            geo = GeoPoint(lat=lat, lon=lon)

    city = None
    address = location.get("address")
    if isinstance(address, dict):
        locality = address.get("addressLocality")
        if isinstance(locality, str) and locality:
            city = locality

    return venue_name, geo, city


def _parse_is_free(offers: Any) -> bool | None:
    """Return a single verified price, otherwise ``None`` for a mixed or unknown offer (FR-5.10)."""
    if isinstance(offers, list):
        results = [_parse_is_free(o) for o in offers]
        if results and all(r is True for r in results):
            return True
        if results and all(r is False for r in results):
            return False
        return None
    if not isinstance(offers, dict):
        return None

    price = offers.get("price")
    if isinstance(price, (int, float)) and not isinstance(price, bool):
        return price == 0
    if isinstance(price, str):
        normalized_price = price.strip().lower()
        if normalized_price in _FREE_PRICES:
            return True
        try:
            return float(normalized_price) == 0
        except ValueError:
            pass

    availability = offers.get("availability")
    if isinstance(availability, str) and "free" in availability.lower():
        return True
    return None


def _as_float(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _stable_id(name: str, start_raw: str) -> str:
    digest = hashlib.sha256(f"{name}|{start_raw}".encode()).hexdigest()
    return f"jsonld:{digest[:32]}"
