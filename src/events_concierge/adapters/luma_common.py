"""Shared normalization for reviewed public Luma catalog entries.

The calendar and Discover endpoints have different listing and pagination contracts, but their
embedded public event records use the same shape. Keeping that shape validation here prevents the
two adapters from disagreeing about event identity, timing, location, or price semantics.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from ..domain.enums import PriceStatus, Source
from ..domain.events import (
    MAX_EVENT_ENTITY_PROFILES,
    MAX_EVENT_ENTITY_SOCIAL_LINKS,
    MAX_PUBLIC_PRICE_CENTS,
    CandidateEvent,
    EventEntityKind,
    EventEntityProfile,
    EventEntityRole,
    EventEntitySocialLink,
    GeoPoint,
)
from ..domain.social_profiles import social_profile_from_published_value

_MIN_LATITUDE = -90.0
_MAX_LATITUDE = 90.0
_MIN_LONGITUDE = -180.0
_MAX_LONGITUDE = 180.0
_ISO_CURRENCY_CODE_LENGTH = 3
# A Luma custom slug may contain dots -- https://luma.com/fw.models.nyc is a live public event --
# and rejecting one failed the *whole* refresh for its calendar. The first character stays
# alphanumeric, so a slug can never begin with a dot and `..` cannot appear on its own; the slug is
# only ever interpolated into a path segment, so a dot cannot reach the host either.
_EVENT_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}")
_EVENT_API_ID = re.compile(r"evt-[A-Za-z0-9_-]{1,200}")
_CALENDAR_API_ID = re.compile(r"cal-[A-Za-z0-9_-]{1,200}")
_LINKEDIN_PERSON_HANDLE = re.compile(r"/in/[A-Za-z0-9][A-Za-z0-9_%.-]*/?")
_LINKEDIN_COMPANY_HANDLE = re.compile(r"/company/[A-Za-z0-9][A-Za-z0-9_%.-]*/?")
_MAX_PUBLIC_ROLE_NAMES = 32
# Public events can list more hosts than the catalog displays (a reviewed event had 36).
# Bound the input separately; names and their role-gated links retain the domain's 32-name cap.
MAX_PUBLIC_ROLE_RECORDS = 128
_MAX_PUBLIC_SESSIONS = 100
_UNKNOWN_PRICE_DETAILS = (PriceStatus.UNKNOWN, None, None, None)
# Luma publishes these beside ``linkedin_handle`` and ``website`` on the same structured host and
# calendar records.  Each is a bare handle -- no scheme, no host -- so each platform needs its own
# URL construction and its own validation; ``twitter_handle`` is the X platform's field name and
# maps to the ``x_profile`` provider key.  These are read only from records that already survive the
# role gate below, so a featured guest or attendee still contributes nothing.
_SOCIAL_HANDLE_FIELDS: tuple[tuple[str, str], ...] = (
    ("twitter_handle", "x_profile"),
    ("instagram_handle", "instagram_profile"),
    ("tiktok_handle", "tiktok_profile"),
    ("youtube_handle", "youtube_profile"),
)


class LumaEntryValidationError(ValueError):
    """A public Luma listing returned an event outside its reviewed record contract."""


def is_public_entry(entry: dict[str, object], source_key: str) -> bool:
    """Return explicit public visibility; never infer it from membership of a list.

    Both listing contracts need this. A calendar's cursor will happily list an ``unlisted`` event
    to anyone who asks, and only ``visibility`` says so -- appearing on the calendar does not.
    """
    event = json_object(entry.get("event"))
    if event is None:
        raise LumaEntryValidationError(f"Luma source {source_key} returned no event object")
    visibility = event.get("visibility")
    if not isinstance(visibility, str):
        raise LumaEntryValidationError(
            f"Luma source {source_key} returned an invalid event visibility"
        )
    return visibility == "public"


def normalize_luma_entry(
    entry: dict[str, object],
    source_key: str,
    *,
    contract: Literal["calendar", "discover"],
    listed_calendar_api_id: str | None = None,
) -> CandidateEvent:
    """Normalize one event after proving its listing-specific public identity."""
    event = json_object(entry.get("event"))
    if event is None:
        raise LumaEntryValidationError(f"Luma source {source_key} returned no event object")

    owner_calendar_api_id = _required_text(
        event.get("calendar_api_id"),
        "event owner calendar API id",
        source_key,
    )
    if _CALENDAR_API_ID.fullmatch(owner_calendar_api_id) is None:
        raise LumaEntryValidationError(
            f"Luma source {source_key} returned an invalid event owner calendar API id"
        )
    event_api_id = _required_text(event.get("api_id"), "event API id", source_key)
    if _EVENT_API_ID.fullmatch(event_api_id) is None:
        raise LumaEntryValidationError(f"Luma source {source_key} returned an invalid event API id")

    _validate_listing_identity(
        entry,
        event,
        source_key,
        contract=contract,
        listed_calendar_api_id=listed_calendar_api_id,
        event_api_id=event_api_id,
        owner_calendar_api_id=owner_calendar_api_id,
    )

    slug = _required_text(event.get("url"), "event URL", source_key)
    if _EVENT_SLUG.fullmatch(slug) is None:
        raise LumaEntryValidationError(f"Luma source {source_key} returned an unsafe event URL")
    registration_url = f"https://luma.com/{slug}"
    title = _required_text(event.get("name"), "event title", source_key)
    start_at = _parse_instant(event.get("start_at"))
    if start_at is None:
        raise LumaEntryValidationError(f"Luma source {source_key} returned an invalid event start")
    end_at = _parse_instant(event.get("end_at"))
    if event.get("end_at") is not None and end_at is None:
        raise LumaEntryValidationError(f"Luma source {source_key} returned an invalid event end")
    if end_at is not None and end_at <= start_at:
        raise LumaEntryValidationError(
            f"Luma source {source_key} returned an event ending before it starts"
        )
    venue_name, city = _location(event.get("geo_address_info"))
    geo = _coordinate(event.get("coordinate"), source_key)
    price_status, price_min_cents, price_max_cents, price_currency = luma_price_details(
        entry.get("ticket_info"),
    )
    organizer_name, host_names, entity_profiles, entity_social_links = _listing_entities(
        entry,
        owner_calendar_api_id,
    )
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=registration_url,
        title=title,
        start_at=start_at,
        registration_url=registration_url,
        end_at=end_at,
        venue_name=venue_name,
        geo=geo,
        city=city,
        price_status=price_status,
        price_min_cents=price_min_cents,
        price_max_cents=price_max_cents,
        price_currency=price_currency,
        organizer_name=organizer_name,
        host_names=host_names,
        entity_profiles=entity_profiles,
        entity_social_links=entity_social_links,
    )


def _validate_listing_identity(
    entry: dict[str, object],
    event: dict[str, object],
    source_key: str,
    *,
    contract: Literal["calendar", "discover"],
    listed_calendar_api_id: str | None,
    event_api_id: str,
    owner_calendar_api_id: str,
) -> None:
    if contract == "calendar":
        if listed_calendar_api_id is None or entry.get("calendar_api_id") != listed_calendar_api_id:
            raise LumaEntryValidationError(
                f"Luma source {source_key} returned an event from another calendar"
            )
        return
    if entry.get("api_id") != event_api_id:
        raise LumaEntryValidationError(
            f"Luma source {source_key} returned a mismatched Discover event identity"
        )
    calendar = json_object(entry.get("calendar"))
    if calendar is None or calendar.get("api_id") != owner_calendar_api_id:
        raise LumaEntryValidationError(
            f"Luma source {source_key} returned a mismatched Discover calendar identity"
        )
    if event.get("visibility") != "public":
        raise LumaEntryValidationError(
            f"Luma source {source_key} normalized a non-public Discover event"
        )


def json_object(value: object) -> dict[str, object] | None:
    """Narrow arbitrary decoded JSON to a string-keyed object."""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def luma_public_entity_profiles(
    *,
    calendar_value: object,
    hosts_value: object,
    sessions_value: object,
    organizer_name: str | None,
    host_names: tuple[str, ...],
    speaker_names: tuple[str, ...],
) -> tuple[EventEntityProfile, ...]:
    """Project links only from structured public calendar/host/session records.

    Description-derived people and partners deliberately have no profile source here. Callers also
    never pass guest/attendee/featured-guest records, keeping those identities outside the model.
    """
    allowed = _allowed_role_names(
        organizer_name=organizer_name,
        host_names=host_names,
        speaker_names=speaker_names,
    )
    records = _public_role_records(
        calendar_value=calendar_value,
        hosts_value=hosts_value,
        sessions_value=sessions_value,
        organizer_name=organizer_name,
    )

    profiles: list[EventEntityProfile] = []
    seen: set[tuple[str, str]] = set()
    for record, role, expected_kind in records:
        name = _entity_name(record)
        if name is None or name.casefold() not in allowed[role]:
            continue
        identity = (role, name.casefold())
        if identity in seen:
            continue
        profile = _entity_profile_from_record(
            record,
            name=name,
            role=role,
            expected_kind=expected_kind,
        )
        if profile is None:
            continue
        profiles.append(profile)
        seen.add(identity)
        if len(profiles) >= MAX_EVENT_ENTITY_PROFILES:
            break
    return tuple(profiles)


def luma_public_entity_social_links(
    *,
    calendar_value: object,
    hosts_value: object,
    sessions_value: object,
    organizer_name: str | None,
    host_names: tuple[str, ...],
    speaker_names: tuple[str, ...],
) -> tuple[EventEntitySocialLink, ...]:
    """Project the social handles Luma publishes on the *same* structured role records.

    These are not ``EventEntityProfile`` values and never become any: FR-19.13 admits only a
    validated LinkedIn URL or an explicit organization website into producer-verified entity-profile
    metadata, so ``entity_profiles`` keeps carrying exactly those two shapes.  A social handle
    belongs to the enrichment plane instead, and travels beside the profiles as its own value.

    The record set, the role gate, and the exclusions are shared with
    ``luma_public_entity_profiles``: callers never pass guest, attendee, or featured-guest records,
    and a record whose name is not displayed in its role is skipped here too.  A handle that fails
    its platform's validation is dropped silently -- never stored raw, and never guessed at.
    """
    allowed = _allowed_role_names(
        organizer_name=organizer_name,
        host_names=host_names,
        speaker_names=speaker_names,
    )
    records = _public_role_records(
        calendar_value=calendar_value,
        hosts_value=hosts_value,
        sessions_value=sessions_value,
        organizer_name=organizer_name,
    )

    links: list[EventEntitySocialLink] = []
    seen: set[tuple[str, str, str]] = set()
    for record, role, _ in records:
        name = _entity_name(record)
        if name is None or name.casefold() not in allowed[role]:
            continue
        for field_name, provider_key in _SOCIAL_HANDLE_FIELDS:
            profile = social_profile_from_published_value(provider_key, record.get(field_name))
            if profile is None:
                continue
            identity = (role, name.casefold(), provider_key)
            if identity in seen:
                continue
            seen.add(identity)
            links.append(
                EventEntitySocialLink(
                    name=name,
                    role=role,
                    provider_key=profile.provider_key,
                    profile_url=profile.url,
                )
            )
            if len(links) >= MAX_EVENT_ENTITY_SOCIAL_LINKS:
                return tuple(links)
    return tuple(links)


def _allowed_role_names(
    *,
    organizer_name: str | None,
    host_names: tuple[str, ...],
    speaker_names: tuple[str, ...],
) -> dict[str, frozenset[str]]:
    return {
        "organizer": (
            frozenset({organizer_name.casefold()})
            if organizer_name is not None
            else frozenset()
        ),
        "host": frozenset(name.casefold() for name in host_names),
        "speaker": frozenset(name.casefold() for name in speaker_names),
    }


def _public_role_records(
    *,
    calendar_value: object,
    hosts_value: object,
    sessions_value: object,
    organizer_name: str | None,
) -> list[tuple[dict[str, object], EventEntityRole, EventEntityKind | None]]:
    """Collect the structured public role records both projections read.

    Guest, attendee, and featured-guest collections are never among the inputs, so no identity from
    them can reach either projection.
    """
    records: list[
        tuple[dict[str, object], EventEntityRole, EventEntityKind | None]
    ] = []
    calendar = json_object(calendar_value)
    if calendar is not None:
        records.append((calendar, "organizer", _entity_kind_hint(calendar)))
    records.extend(
        (host, "host", _entity_kind_hint(host))
        for host in _bounded_entity_records(hosts_value, MAX_PUBLIC_ROLE_RECORDS)
    )
    for session in _bounded_entity_records(sessions_value, _MAX_PUBLIC_SESSIONS):
        for field_name in ("speakers", "hosts"):
            records.extend(
                (speaker, "speaker", _entity_kind_hint(speaker))
                for speaker in _bounded_entity_records(
                    session.get(field_name),
                    MAX_PUBLIC_ROLE_RECORDS,
                )
            )

    # A "Personal" calendar uses its first public host as the displayed organizer.
    if organizer_name is not None:
        records.extend(
            (host, "organizer", _entity_kind_hint(host))
            for host in _bounded_entity_records(hosts_value, MAX_PUBLIC_ROLE_RECORDS)
        )
    return records


def _listing_entities(
    entry: dict[str, object],
    owner_calendar_api_id: str,
) -> tuple[
    str | None,
    tuple[str, ...],
    tuple[EventEntityProfile, ...],
    tuple[EventEntitySocialLink, ...],
]:
    calendar = json_object(entry.get("calendar"))
    if calendar is not None and calendar.get("api_id") != owner_calendar_api_id:
        calendar = None
    hosts = _bounded_entity_records(entry.get("hosts"), MAX_PUBLIC_ROLE_RECORDS)
    host_names = _dedupe_entity_names(hosts)
    organizer_name = _entity_name(calendar) if calendar is not None else None
    if organizer_name is None or organizer_name.casefold() == "personal":
        organizer_name = host_names[0] if host_names else None
    return (
        organizer_name,
        host_names,
        luma_public_entity_profiles(
            calendar_value=calendar,
            hosts_value=hosts,
            sessions_value=None,
            organizer_name=organizer_name,
            host_names=host_names,
            speaker_names=(),
        ),
        luma_public_entity_social_links(
            calendar_value=calendar,
            hosts_value=hosts,
            sessions_value=None,
            organizer_name=organizer_name,
            host_names=host_names,
            speaker_names=(),
        ),
    )


def _bounded_entity_records(value: object, limit: int) -> tuple[dict[str, object], ...]:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        return ()
    records: list[dict[str, object]] = []
    for raw_record in value:
        record = json_object(raw_record)
        if record is None:
            return ()
        records.append(record)
    return tuple(records)


def _dedupe_entity_names(records: tuple[dict[str, object], ...]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for record in records:
        name = _entity_name(record)
        if name is None or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        names.append(name)
        if len(names) >= _MAX_PUBLIC_ROLE_NAMES:
            break
    return tuple(names)


def _entity_name(record: dict[str, object] | None) -> str | None:
    if record is None:
        return None
    name = _bounded_text(record.get("name"), 160)
    if name is not None:
        return name
    first_name = _bounded_text(record.get("first_name"), 80)
    last_name = _bounded_text(record.get("last_name"), 80)
    return _bounded_text(
        " ".join(part for part in (first_name, last_name) if part),
        160,
    )


def _entity_profile_from_record(
    record: dict[str, object],
    *,
    name: str,
    role: EventEntityRole,
    expected_kind: EventEntityKind | None,
) -> EventEntityProfile | None:
    linked_in = _direct_linkedin_url(record.get("linkedin_handle"))
    if linked_in is not None:
        profile_url, kind = linked_in
        # Luma occasionally publishes a syntactically valid person handle on an explicitly
        # organizational calendar.  A direct path is not verified for this entity when its kind
        # contradicts the structured public record, so fail closed and prefer an explicit site.
        if expected_kind is None or kind == expected_kind:
            return EventEntityProfile(
                name=name,
                role=role,
                kind=kind,
                profile_url=profile_url,
            )
    if expected_kind != "organization":
        return None
    website = record.get("website")
    if not isinstance(website, str):
        return None
    try:
        return EventEntityProfile(
            name=name,
            role=role,
            kind="organization",
            profile_url=website,
        )
    except ValueError:
        return None


def _entity_kind_hint(record: dict[str, object]) -> EventEntityKind | None:
    is_personal = record.get("is_personal")
    if is_personal is True:
        return "person"
    if is_personal is False:
        return "organization"
    return None


def _direct_linkedin_url(  # noqa: PLR0911
    value: object,
) -> tuple[str, EventEntityKind] | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if _LINKEDIN_PERSON_HANDLE.fullmatch(candidate) is not None:
        return f"https://www.linkedin.com{candidate}", "person"
    if _LINKEDIN_COMPANY_HANDLE.fullmatch(candidate) is not None:
        return f"https://www.linkedin.com{candidate}", "organization"
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.hostname not in {"linkedin.com", "www.linkedin.com"}:
        return None
    kind: EventEntityKind
    if _LINKEDIN_PERSON_HANDLE.fullmatch(parsed.path) is not None:
        kind = "person"
    elif _LINKEDIN_COMPANY_HANDLE.fullmatch(parsed.path) is not None:
        kind = "organization"
    else:
        return None
    try:
        profile = EventEntityProfile(
            name="Validated Profile",
            role="host",
            kind=kind,
            profile_url=candidate,
        )
    except ValueError:
        return None
    return profile.profile_url, kind


def _bounded_text(value: object, max_length: int) -> str | None:
    text = _optional_text(value)
    return text if text is not None and len(text) <= max_length else None


def _location(value: object) -> tuple[str | None, str | None]:
    address = json_object(value)
    if address is None:
        return None, None
    city = _optional_text(address.get("city"))
    venue = next(
        (
            candidate
            for candidate in (
                _optional_text(address.get("address")),
                _optional_text(address.get("short_address")),
                _optional_text(address.get("sublocality")),
                _optional_text(address.get("city_state")),
            )
            if candidate is not None
        ),
        None,
    )
    return venue, city


def _coordinate(value: object, source_key: str) -> GeoPoint | None:
    if value is None:
        return None
    coordinate = json_object(value)
    if coordinate is None:
        raise LumaEntryValidationError(f"Luma source {source_key} returned a malformed coordinate")
    latitude = _finite_number(coordinate.get("latitude"))
    longitude = _finite_number(coordinate.get("longitude"))
    if (
        latitude is None
        or longitude is None
        or not _MIN_LATITUDE <= latitude <= _MAX_LATITUDE
        or not _MIN_LONGITUDE <= longitude <= _MAX_LONGITUDE
    ):
        raise LumaEntryValidationError(f"Luma source {source_key} returned an invalid coordinate")
    return GeoPoint(latitude, longitude)


def _price_status(value: object) -> PriceStatus:
    return luma_price_details(value)[0]


def luma_price_details(
    ticket_info_value: object,
    ticket_types_value: object = None,
) -> tuple[PriceStatus, int | None, int | None, str | None]:
    """Resolve Luma's public ticket summary, preferring explicit visible ticket types.

    Discover list rows sometimes report ``ticket_info.is_free = false`` with no price even when
    the anonymous detail record contains one enabled ``type = free`` ticket. Treat the detailed
    ticket inventory as the stronger observation while remaining conservative for mixed, hidden,
    flexible, or malformed offers.
    """
    typed = _ticket_type_price_details(ticket_types_value)
    if typed is not None:
        return typed

    ticket_info = json_object(ticket_info_value)
    if ticket_info is None:
        return PriceStatus.UNKNOWN, None, None, None
    free = ticket_info.get("is_free")
    is_free = free if isinstance(free, bool) else None
    minimum = _money(ticket_info.get("price"))
    raw_maximum = ticket_info.get("max_price")
    maximum = _money(raw_maximum)
    has_positive_price = any(
        money is not None and money[0] > 0 for money in (minimum, maximum)
    )
    if is_free is True:
        status = PriceStatus.UNKNOWN if has_positive_price else PriceStatus.FREE
    elif is_free is False and has_positive_price:
        status = PriceStatus.PAID
    else:
        status = PriceStatus.UNKNOWN
    if status is not PriceStatus.PAID or minimum is None or minimum[0] <= 0:
        return status, None, None, None
    if raw_maximum is None:
        maximum = minimum
    elif maximum is None:
        return status, None, None, None
    if maximum[1] != minimum[1] or maximum[0] < minimum[0]:
        return status, None, None, None
    return status, minimum[0], maximum[0], minimum[1]


def _ticket_type_price_details(
    value: object,
) -> tuple[PriceStatus, int | None, int | None, str | None] | None:
    if value is None:
        return None
    try:
        active = _active_ticket_types(value)
    except ValueError:
        return _UNKNOWN_PRICE_DETAILS
    if not active:
        return None
    return _aggregate_ticket_type_prices(active)


def _active_ticket_types(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("ticket types must be a list")
    active: list[dict[str, object]] = []
    for raw_ticket_type in value:
        ticket_type = json_object(raw_ticket_type)
        if ticket_type is None:
            raise ValueError("ticket type must be an object")
        if ticket_type.get("is_disabled") is True or ticket_type.get("is_hidden") is True:
            continue
        active.append(ticket_type)
    return active


def _aggregate_ticket_type_prices(
    active: list[dict[str, object]],
) -> tuple[PriceStatus, int | None, int | None, str | None]:
    free_count = 0
    paid: list[tuple[int, str]] = []
    for ticket_type in active:
        offer_type = ticket_type.get("type")
        if offer_type == "free":
            free_count += 1
            continue
        if offer_type != "paid" or ticket_type.get("is_flexible") is True:
            return _UNKNOWN_PRICE_DETAILS
        money = _ticket_type_money(ticket_type)
        if money is None or money[0] <= 0:
            return _UNKNOWN_PRICE_DETAILS
        paid.append(money)

    if free_count == len(active):
        return PriceStatus.FREE, None, None, None
    if free_count or not paid:
        return _UNKNOWN_PRICE_DETAILS
    currencies = {currency for _, currency in paid}
    if len(currencies) != 1:
        return PriceStatus.PAID, None, None, None
    amounts = [amount for amount, _ in paid]
    return PriceStatus.PAID, min(amounts), max(amounts), currencies.pop()


def _ticket_type_money(value: dict[str, object]) -> tuple[int, str] | None:
    cents = value.get("cents")
    currency = value.get("currency")
    if (
        isinstance(cents, bool)
        or not isinstance(cents, int)
        or not 0 <= cents <= MAX_PUBLIC_PRICE_CENTS
        or not isinstance(currency, str)
        or len(currency.strip()) != _ISO_CURRENCY_CODE_LENGTH
        or not currency.strip().isascii()
        or not currency.strip().isalpha()
    ):
        return None
    return cents, currency.strip().upper()


def _money(value: object) -> tuple[int, str] | None:
    money = json_object(value)
    if money is None:
        return None
    cents = _money_cents(money)
    currency = money.get("currency")
    if (
        cents is None
        or not isinstance(currency, str)
        or len(currency.strip()) != _ISO_CURRENCY_CODE_LENGTH
        or not currency.strip().isascii()
        or not currency.strip().isalpha()
    ):
        return None
    return cents, currency.strip().upper()


def _money_cents(value: object) -> int | None:
    money = value if isinstance(value, dict) else json_object(value)
    if money is None:
        return None
    cents = money.get("cents")
    if (
        isinstance(cents, bool)
        or not isinstance(cents, int)
        or not 0 <= cents <= MAX_PUBLIC_PRICE_CENTS
    ):
        return None
    return cents


def _required_text(value: object, field: str, source_key: str) -> str:
    text = _optional_text(value)
    if text is None:
        raise LumaEntryValidationError(f"Luma source {source_key} returned no {field}")
    return text


def _optional_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized if normalized else None


def _parse_instant(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None
