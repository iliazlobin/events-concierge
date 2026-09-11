"""The shared anonymous Luma event-detail lane (FR-3.1/FR-10.3/10.4).

A Luma *listing* -- a Discover page or a calendar cursor page -- carries identity, timing, place,
and price, and nothing else.  Description, speakers, partners, attendance, and registration status
live only on the per-event detail record, and both listing contracts address that record the same
way: one anonymous ``GET https://api2.luma.com/event/get?event_api_id=evt-...``.

This module is that lane.  It was extracted from the Discover adapter when host calendars became a
source class of their own, because a calendar that publishes a host's whole programme and a
Discover shelf that samples across hosts must describe an event identically -- otherwise the same
event arrives rich through one source and bare through the other, and whichever ran last decides
what the reader sees.

Every function here is fail-closed: the detail record must re-assert the identity the listing
already validated (``api_id``, ``calendar_api_id``, ``url``, ``name``, and public visibility), or
the caller is told the identity was lost rather than handed a record for some other event.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit

import httpx

from ..domain.enums import PriceStatus, RegistrationStatus
from ..domain.events import CandidateEvent, EventEntityProfile, EventEntitySocialLink
from .luma_common import (
    MAX_PUBLIC_ROLE_RECORDS,
    json_object,
    luma_price_details,
    luma_public_entity_profiles,
    luma_public_entity_social_links,
)

DETAIL_ORIGIN = "https://api2.luma.com"
_DETAIL_HOST = urlsplit(DETAIL_ORIGIN).hostname
MAX_DETAIL_RESPONSE_BYTES = 2_000_000

_MAX_DESCRIPTION_CHARS = 12_000
_MAX_DESCRIPTION_SOURCE_CHARS = 100_000
_MAX_DESCRIPTION_NODES = 2_500
_MAX_DESCRIPTION_DEPTH = 32
_MAX_DESCRIPTION_NODE_TYPE_LENGTH = 80
_MAX_PUBLIC_GUEST_COUNT = 10_000_000
_MAX_PUBLIC_ROLE_NAMES = 32
_MAX_PUBLIC_SESSIONS = 100
_MAX_PUBLIC_NAME_WORDS = 8
_MIN_PUBLIC_PERSON_NAME_WORDS = 2
_MIN_PRINTABLE_CODEPOINT = 0x20
_DELETE_CODEPOINT = 0x7F
_BLOCK_NODE_TYPES = frozenset(
    {
        "blockquote",
        "bullet_list",
        "code_block",
        "heading",
        "list_item",
        "ordered_list",
        "paragraph",
    }
)


class LumaDetailValidationError(ValueError):
    """A detail record could not be proven to describe the listed event."""


@dataclass(frozen=True, slots=True)
class LumaDetailEnrichment:
    description: str
    organizer_name: str | None
    host_names: tuple[str, ...]
    speaker_names: tuple[str, ...]
    partner_names: tuple[str, ...]
    entity_profiles: tuple[EventEntityProfile, ...]
    entity_social_links: tuple[EventEntitySocialLink, ...]
    attendance_count: int | None
    registration_status: RegistrationStatus
    price_status: PriceStatus
    price_min_cents: int | None
    price_max_cents: int | None
    price_currency: str | None


def resolved_price(
    candidate: CandidateEvent,
    enrichment: LumaDetailEnrichment,
) -> tuple[PriceStatus, int | None, int | None, str | None]:
    if enrichment.price_status is PriceStatus.UNKNOWN:
        return (
            candidate.price_status,
            candidate.price_min_cents,
            candidate.price_max_cents,
            candidate.price_currency,
        )
    return (
        enrichment.price_status,
        enrichment.price_min_cents,
        enrichment.price_max_cents,
        enrichment.price_currency,
    )



def listed_identity(
    entry: dict[str, object],
    candidate: CandidateEvent,
    source_key: str,
) -> tuple[str, str, str]:
    """Recover the already-validated list identity needed for the exact detail request."""
    event = json_object(entry.get("event"))
    calendar = json_object(entry.get("calendar"))
    if event is None or calendar is None:
        raise LumaDetailValidationError(
            f"Luma listing {source_key} lost a validated detail identity"
        )
    event_api_id = event.get("api_id")
    calendar_api_id = event.get("calendar_api_id")
    slug = event.get("url")
    if (
        not isinstance(event_api_id, str)
        or not isinstance(calendar_api_id, str)
        or not isinstance(slug, str)
        or calendar.get("api_id") != calendar_api_id
        or candidate.registration_url != f"https://luma.com/{slug}"
    ):
        raise LumaDetailValidationError(
            f"Luma listing {source_key} lost a validated detail identity"
        )
    return event_api_id, calendar_api_id, slug


def detail_url(event_api_id: str) -> str:
    return f"{DETAIL_ORIGIN}/event/get?{urlencode({'event_api_id': event_api_id})}"


def is_detail_url(value: str, event_api_id: str) -> bool:
    """Accept only one anonymous event-detail identity on the reviewed API host."""
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.hostname != _DETAIL_HOST
        or port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path != "/event/get"
        or parsed.fragment
    ):
        return False
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    return pairs == [("event_api_id", event_api_id)]


def enrichment_from_detail(
    response: httpx.Response,
    *,
    entry: dict[str, object],
    source_key: str,
    event_api_id: str,
    calendar_api_id: str,
    slug: str,
    title: str,
    detail_number: int,
) -> LumaDetailEnrichment:
    """Validate list/detail identity and produce bounded public event enrichment."""
    try:
        payload = json_object(response.json())
    except ValueError as exc:
        raise LumaDetailValidationError(
            f"Luma listing detail {detail_number} returned invalid JSON for {source_key}"
        ) from exc
    event = json_object(payload.get("event")) if payload is not None else None
    calendar = json_object(payload.get("calendar")) if payload is not None else None
    if (
        payload is None
        or payload.get("api_id") != event_api_id
        or event is None
        or event.get("api_id") != event_api_id
        or event.get("calendar_api_id") != calendar_api_id
        or event.get("url") != slug
        or event.get("name") != title
        or event.get("visibility") != "public"
        or calendar is None
        or calendar.get("api_id") != calendar_api_id
    ):
        raise LumaDetailValidationError(
            f"Luma listing detail {detail_number} returned a mismatched public identity "
            f"for {source_key}"
        )
    description = _prosemirror_plain_text(
        payload.get("description_mirror"),
        title=title,
        source_key=source_key,
    )
    listed_calendar = json_object(entry.get("calendar"))
    hosts = _public_role_names(
        payload.get("hosts", entry.get("hosts")),
        "hosts",
        source_key,
    )
    organizer = _short_text(calendar.get("name"), 160)
    if organizer is None and listed_calendar is not None:
        organizer = _short_text(listed_calendar.get("name"), 160)
    if organizer is None or organizer.casefold() == "personal":
        organizer = hosts[0] if hosts else None
    speakers = _dedupe_names(
        (
            *_session_speakers(payload.get("sessions"), source_key),
            *_speaker_names_from_description(description),
        )
    )
    partners = _dedupe_names(
        (
            *_partner_names_from_description(description),
            *_host_organization_names_from_description(
                description,
                excluded_people=(*hosts, *speakers),
            ),
        )
    )
    price_status, price_min_cents, price_max_cents, price_currency = luma_price_details(
        payload.get("ticket_info"),
        payload.get("ticket_types"),
    )
    entity_profiles = luma_public_entity_profiles(
        calendar_value=calendar,
        hosts_value=payload.get("hosts", entry.get("hosts")),
        sessions_value=payload.get("sessions"),
        organizer_name=organizer,
        host_names=hosts,
        speaker_names=speakers,
    )
    # Read from the same records, under the same role gate, and carried beside the profiles rather
    # than inside them: FR-19.13 keeps entity_profiles to LinkedIn and explicit websites only.
    entity_social_links = luma_public_entity_social_links(
        calendar_value=calendar,
        hosts_value=payload.get("hosts", entry.get("hosts")),
        sessions_value=payload.get("sessions"),
        organizer_name=organizer,
        host_names=hosts,
        speaker_names=speakers,
    )
    return LumaDetailEnrichment(
        description=description,
        organizer_name=organizer,
        host_names=hosts,
        speaker_names=speakers,
        partner_names=partners,
        entity_profiles=entity_profiles,
        entity_social_links=entity_social_links,
        attendance_count=_public_guest_count(
            payload.get("guest_count", entry.get("guest_count")),
            source_key,
        ),
        registration_status=_registration_status(
            payload.get(
                "registration_availability",
                entry.get("registration_availability"),
            ),
            source_key,
        ),
        price_status=price_status,
        price_min_cents=price_min_cents,
        price_max_cents=price_max_cents,
        price_currency=price_currency,
    )


def _registration_status(value: object, source_key: str) -> RegistrationStatus:
    """Map Luma's public availability onto the four postures this catalog reports.

    ``coming-soon`` is a real Luma state -- registration has not opened yet -- and there is no
    posture for it here, so it reports ``UNKNOWN``: the honest answer is that we cannot say
    registration is open. It is mapped explicitly rather than left to the raise below, because an
    unmapped state fails the *entire* refresh: five ``coming-soon`` events on one New York calendar
    published nothing at all for it, and the same record reaching a Discover page would have taken
    that whole city feed down with it.
    """
    if value is None or value == "coming-soon":
        return RegistrationStatus.UNKNOWN
    if value == "open":
        return RegistrationStatus.OPEN
    if value == "waitlist":
        return RegistrationStatus.WAITLIST
    if value == "sold-out":
        return RegistrationStatus.SOLD_OUT
    raise LumaDetailValidationError(
        f"Luma listing {source_key} returned an unknown registration state"
    )


def _public_guest_count(value: object, source_key: str) -> int | None:
    if value is None:
        return None
    if (
        not isinstance(value, bool)
        and isinstance(value, int)
        and 0 <= value <= _MAX_PUBLIC_GUEST_COUNT
    ):
        # Luma uses zero for both a real empty guest list and an undisclosed count.  The public
        # payload does not carry enough evidence to distinguish those states, so keep zero as
        # unknown instead of publishing the misleading consumer claim “0 going”.
        return value or None
    raise LumaDetailValidationError(
        f"Luma listing {source_key} returned an invalid public guest count"
    )


def _public_role_names(value: object, field_name: str, source_key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_PUBLIC_ROLE_RECORDS:
        raise LumaDetailValidationError(
            f"Luma listing {source_key} returned invalid public {field_name}"
        )
    names: list[str] = []
    for raw_item in value:
        item = json_object(raw_item)
        if item is None:
            raise LumaDetailValidationError(
                f"Luma listing {source_key} returned invalid public {field_name}"
            )
        name = _short_text(item.get("name"), 160)
        if name is None:
            first_name = _short_text(item.get("first_name"), 80)
            last_name = _short_text(item.get("last_name"), 80)
            name = _short_text(
                " ".join(part for part in (first_name, last_name) if part),
                160,
            )
        # Luma can retain a deleted or incomplete public host record with no display identity.
        # It contributes no useful public fact, so omit it without retaining the profile payload.
        if name is None:
            continue
        names.append(name)
    return _dedupe_names(names)


def _session_speakers(value: object, source_key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_PUBLIC_SESSIONS:
        raise LumaDetailValidationError(
            f"Luma listing {source_key} returned invalid public sessions"
        )
    names: list[str] = []
    for raw_session in value:
        session = json_object(raw_session)
        if session is None:
            raise LumaDetailValidationError(
                f"Luma listing {source_key} returned invalid public sessions"
            )
        for field_name in ("speakers", "hosts"):
            raw_people = session.get(field_name)
            if raw_people is not None:
                names.extend(_public_role_names(raw_people, field_name, source_key))
    return _dedupe_names(names)


def _speaker_names_from_description(value: str) -> tuple[str, ...]:
    names: list[str] = []
    for line in value.splitlines():
        normalized = line.strip()
        bullet_match = re.match(r"^[•*-]\s*([^(\n]{2,160}?)\s*,?\s*\(", normalized)
        if bullet_match is not None:
            name = _plausible_public_person_name(bullet_match.group(1))
            if name is not None:
                names.append(name)
            continue

        # Some Luma organizers publish a schedule instead of structured sessions:
        # "Eric Simons CEO at Bolt". Require a capitalized multi-token name immediately
        # followed by a bounded role marker so prose and arbitrary attendee names do not match.
        role_match = re.match(
            r"^("
            r"[A-Z][\w'\u2019.-]*(?:\s+[A-Z][\w'\u2019.-]*){1,3}?"
            r")\s+(?:(?i:"
            r"CEO|CTO|COO|CFO|CMO|CPO|VP|"
            r"(?:Co[- ]?)?Founder|"
            r"Head|Director|Manager|Partner|Principal|Principle|"
            r"Engineer|Developer|Designer|Researcher|Professor|Investor|"
            r"Dev(?:eloper)?(?:\s+Rel(?:ations)?)?"
            r"))\b",
            normalized,
        )
        if role_match is not None:
            name = _plausible_public_person_name(role_match.group(1))
            if name is not None:
                names.append(name)

    # Some event pages introduce a featured person in prose rather than a session object, for
    # example “hear a lecture by Marko Jukic” or “Ada Lovelace is a researcher at …”.  Keep the
    # patterns bounded to an explicit speaking cue or professional-role assertion; arbitrary
    # capitalized attendee names are not enough evidence.
    prose_patterns = (
        re.compile(
            r"\b(?:lecture|talk|keynote|presentation|fireside chat)\s+"
            r"(?:by|from|with)\s+"
            r"([A-Z][\w'\u2019.-]*(?:\s+[A-Z][\w'\u2019.-]*){1,3})\b"
        ),
        re.compile(
            r"(?:^|[.!?]\s+)"
            r"([A-Z][\w'\u2019.-]*(?:\s+[A-Z][\w'\u2019.-]*){1,3})\s+"
            r"is\s+(?:an?|the)\s+"
            r"(?:(?:Senior|Lead|Principal|Staff|Managing|Research)\s+)?"
            r"(?:Analyst|CEO|CTO|COO|CFO|CMO|CPO|VP|Founder|Co[- ]?Founder|"
            r"Head|Director|Manager|Partner|Principal|Engineer|Developer|Designer|"
            r"Researcher|Professor|Investor)\b"
        ),
    )
    prose = " ".join(value.split())
    for pattern in prose_patterns:
        for match in pattern.finditer(prose):
            name = _plausible_public_person_name(match.group(1))
            if name is not None:
                names.append(name)
    return _dedupe_names(names)


def _plausible_public_person_name(value: str) -> str | None:
    """Return a conservative public person name, excluding schedules and instructions."""
    name = _short_text(value.strip(" ,:;-"), 160)
    if name is None:
        return None
    words = name.split()
    if not 1 < len(words) <= _MAX_PUBLIC_NAME_WORDS or any(character.isdigit() for character in name):
        return None
    particles = {"al", "bin", "da", "de", "del", "der", "di", "la", "van", "von"}
    capitalized = 0
    for word in words:
        token = word.strip(".'\u2019-")
        if not token or not any(character.isalpha() for character in token):
            return None
        if token.casefold() in particles:
            continue
        if token[0].isupper():
            capitalized += 1
            continue
        return None
    return name if capitalized >= _MIN_PUBLIC_PERSON_NAME_WORDS else None


def _partner_names_from_description(value: str) -> tuple[str, ...]:
    names: list[str] = []
    prefixes = (
        "hosted with ",
        "presented with ",
        "in partnership with ",
        "in collaboration with ",
        "sponsored by ",
        "supported by ",
        "brought to you by ",
        "vendors: ",
        "partners: ",
    )
    for line in value.splitlines():
        normalized = " ".join(line.split()).strip(" .")
        lowered = normalized.casefold()
        prefix = next((candidate for candidate in prefixes if lowered.startswith(candidate)), None)
        if prefix is None:
            continue
        remainder = normalized[len(prefix) :]
        for candidate in re.split(r",|\s+and\s+|\s+&\s+", remainder):
            name = _short_text(candidate.strip(" .,:;"), 160)
            if name is not None:
                names.append(name)
    return _dedupe_names(names)


def _host_organization_names_from_description(
    value: str,
    *,
    excluded_people: tuple[str, ...],
) -> tuple[str, ...]:
    """Extract only explicitly headed host organizations such as ``Corgi — …``.

    This intentionally does not infer a company from arbitrary prose or a featured Luma calendar.
    The latter is a discovery attribution, not evidence that the community is a partner or vendor.
    """
    paragraphs = [paragraph.strip() for paragraph in value.split("\n\n") if paragraph.strip()]
    excluded = {name.casefold() for name in excluded_people}
    names: list[str] = []
    section_headers = {
        "host organizations",
        "hosted by",
        "our hosts",
        "your hosts",
    }
    for index, paragraph in enumerate(paragraphs):
        if _comparison_text(paragraph).rstrip(":") not in section_headers:
            continue
        for candidate_line in paragraphs[index + 1 : index + 9]:
            match = re.match(r"^(.{1,80}?)\s+[\u2013\u2014]\s+\S", candidate_line)
            if match is None:
                break
            name = _short_text(match.group(1).strip(" .,:;"), 80)
            if (
                name is not None
                and len(name.split()) <= _MAX_PUBLIC_NAME_WORDS
                and name.casefold() not in excluded
            ):
                names.append(name)
    return _dedupe_names(names)


def _dedupe_names(values: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    names: list[str] = []
    seen: set[str] = set()
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        names.append(value)
        if len(names) >= _MAX_PUBLIC_ROLE_NAMES:
            break
    return tuple(names)


def _prosemirror_plain_text(
    value: object,
    *,
    title: str,
    source_key: str,
) -> str:
    """Flatten Luma's bounded ProseMirror JSON without retaining links or rich markup."""
    if value is None:
        return ""
    root = json_object(value)
    if root is None or root.get("type") != "doc":
        raise LumaDetailValidationError(
            f"Luma listing {source_key} returned a malformed event description"
        )
    budget = [0, 0]
    rendered = _render_description_node(
        root,
        source_key=source_key,
        depth=0,
        budget=budget,
    )
    normalized = _normalize_description_text(rendered)
    paragraphs = normalized.split("\n\n") if normalized else []
    if paragraphs and _comparison_text(paragraphs[0]) == _comparison_text(title):
        normalized = "\n\n".join(paragraphs[1:]).strip()
    if len(normalized) <= _MAX_DESCRIPTION_CHARS:
        return normalized
    clipped = normalized[: _MAX_DESCRIPTION_CHARS + 1]
    clipped = clipped.rsplit(" ", 1)[0] or normalized[:_MAX_DESCRIPTION_CHARS]
    return f"{clipped.rstrip()}…"


def _render_description_node(
    value: object,
    *,
    source_key: str,
    depth: int,
    budget: list[int],
) -> str:
    if depth > _MAX_DESCRIPTION_DEPTH:
        raise LumaDetailValidationError(
            f"Luma listing {source_key} event description exceeded its depth limit"
        )
    node = json_object(value)
    if node is None:
        raise LumaDetailValidationError(
            f"Luma listing {source_key} returned a malformed event description node"
        )
    budget[0] += 1
    if budget[0] > _MAX_DESCRIPTION_NODES:
        raise LumaDetailValidationError(
            f"Luma listing {source_key} event description exceeded its node limit"
        )
    node_type = node.get("type")
    if (
        not isinstance(node_type, str)
        or not 1 <= len(node_type) <= _MAX_DESCRIPTION_NODE_TYPE_LENGTH
    ):
        raise LumaDetailValidationError(
            f"Luma listing {source_key} returned an invalid event description node"
        )
    if node_type == "text":
        text_value = node.get("text")
        if not isinstance(text_value, str):
            raise LumaDetailValidationError(
                f"Luma listing {source_key} returned an invalid event description text"
            )
        budget[1] += len(text_value)
        if budget[1] > _MAX_DESCRIPTION_SOURCE_CHARS:
            raise LumaDetailValidationError(
                f"Luma listing {source_key} event description exceeded its text limit"
            )
        return text_value
    if node_type == "hard_break":
        return "\n"

    raw_content = node.get("content", [])
    if raw_content is None:
        raw_content = []
    if not isinstance(raw_content, list):
        raise LumaDetailValidationError(
            f"Luma listing {source_key} returned invalid event description content"
        )
    rendered = "".join(
        _render_description_node(
            child,
            source_key=source_key,
            depth=depth + 1,
            budget=budget,
        )
        for child in raw_content
    )
    if node_type == "list_item" and rendered.strip():
        return f"• {rendered.strip()}\n"
    if node_type in _BLOCK_NODE_TYPES and rendered.strip():
        return f"{rendered.strip()}\n\n"
    return rendered


def _normalize_description_text(value: str) -> str:
    safe = "".join(
        character
        if character in {"\n", "\t"}
        or (
            ord(character) >= _MIN_PRINTABLE_CODEPOINT
            and ord(character) != _DELETE_CODEPOINT
        )
        else " "
        for character in value
    )
    lines: list[str] = []
    for raw_line in safe.splitlines():
        line = " ".join(raw_line.split())
        if line:
            lines.append(line)
        elif lines and lines[-1]:
            lines.append("")
    return "\n".join(lines).strip()


def _comparison_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    searchable = "".join(
        character if character.isalnum() else " "
        for character in normalized
    )
    return " ".join(searchable.split())


def _short_text(value: object, max_length: int) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    return normalized if normalized and len(normalized) <= max_length else None
