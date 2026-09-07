"""Event value objects: the raw CandidateEvent, the de-duplicated CanonicalEvent, and provenance."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, cast
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from .enums import EventStatus, PriceStatus, RegistrationStatus, Source
from .event_semantics import CATALOG_TOPICS, MAX_EXTRACTION_EVIDENCE, ExtractionEvidence
from .social_profiles import social_profile_from_url

_MAX_PUBLIC_ROLE_NAMES = 32
_MAX_PUBLIC_ROLE_NAME_LENGTH = 160
MAX_EVENT_ENTITY_PROFILES = 64
MAX_EVENT_ENTITY_SOCIAL_LINKS = 128
MAX_EVENT_ENTITY_PROFILE_URL_LENGTH = 2_048
_MAX_PUBLIC_ATTENDANCE = 10_000_000
_ISO_CURRENCY_CODE_LENGTH = 3
MAX_PUBLIC_PRICE_CENTS = 100_000_000
_LINKEDIN_HOSTS = frozenset({"linkedin.com", "www.linkedin.com"})
_LINKEDIN_PERSON_PATH = re.compile(r"/in/[A-Za-z0-9][A-Za-z0-9_%.-]*/?")
_LINKEDIN_COMPANY_PATH = re.compile(r"/company/[A-Za-z0-9][A-Za-z0-9_%.-]*/?")
_MIN_PRINTABLE_CODEPOINT = 0x20
_DELETE_CODEPOINT = 0x7F

type EventEntityRole = Literal["host", "organizer", "speaker", "partner"]
type EventEntityKind = Literal["person", "organization"]
_EVENT_ENTITY_ROLES = frozenset({"host", "organizer", "speaker", "partner"})
_EVENT_ENTITY_KINDS = frozenset({"person", "organization"})


def _public_name(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split())
    if not normalized or len(normalized) > _MAX_PUBLIC_ROLE_NAME_LENGTH:
        raise ValueError(f"event {field_name} is invalid")
    return normalized


def _public_names(values: tuple[str, ...], field_name: str) -> tuple[str, ...]:
    if len(values) > _MAX_PUBLIC_ROLE_NAMES:
        raise ValueError(f"event {field_name} exceeds its public role limit")
    normalized = tuple(_public_name(value, field_name) for value in values)
    names = tuple(value for value in normalized if value is not None)
    if len({name.casefold() for name in names}) != len(names):
        raise ValueError(f"event {field_name} contains duplicate names")
    return names


def _direct_entity_profile_url(value: str, kind: EventEntityKind) -> str:
    """Return one browser-safe direct public URL without inventing a lookup target."""
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > MAX_EVENT_ENTITY_PROFILE_URL_LENGTH
        or any(
            character.isspace()
            or ord(character) < _MIN_PRINTABLE_CODEPOINT
            or ord(character) == _DELETE_CODEPOINT
            for character in candidate
        )
    ):
        raise ValueError("event entity profile URL is invalid")
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        # Force validation of a supplied port even though preserving it is safe.
        _ = parsed.port
    except ValueError as exc:
        raise ValueError("event entity profile URL is invalid") from exc
    if (
        parsed.scheme.lower() != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("event entity profile URL must be a direct HTTPS URL")

    if hostname.casefold() in _LINKEDIN_HOSTS:
        expected_path = (
            _LINKEDIN_PERSON_PATH
            if kind == "person"
            else _LINKEDIN_COMPANY_PATH
        )
        if expected_path.fullmatch(parsed.path) is None:
            raise ValueError("event LinkedIn profile URL must identify a direct profile")
        parsed = parsed._replace(query="", fragment="")
    else:
        if kind != "organization":
            raise ValueError("event person profiles must use a direct LinkedIn URL")
        parsed = parsed._replace(fragment="")
    return urlunsplit(parsed)


@dataclass(frozen=True, slots=True)
class EventEntityProfile:
    """One verified public entity URL attached to a displayed event role."""

    name: str
    role: EventEntityRole
    kind: EventEntityKind
    profile_url: str

    def __post_init__(self) -> None:
        normalized_name = _public_name(self.name, "entity profile name")
        if normalized_name is None:
            raise ValueError("event entity profile name is invalid")
        if self.role not in _EVENT_ENTITY_ROLES:
            raise ValueError("event entity profile role is invalid")
        if self.kind not in _EVENT_ENTITY_KINDS:
            raise ValueError("event entity profile kind is invalid")
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(
            self,
            "profile_url",
            _direct_entity_profile_url(self.profile_url, self.kind),
        )

    def as_payload(self) -> dict[str, str]:
        """Serialize the fixed public representation used by JSONB and the API."""
        return {
            "name": self.name,
            "role": self.role,
            "kind": self.kind,
            "profile_url": self.profile_url,
        }


@dataclass(frozen=True, slots=True)
class EventEntitySocialLink:
    """One public social profile a source published about a displayed event role.

    Deliberately **not** an ``EventEntityProfile``.  FR-19.13 admits only a validated LinkedIn
    ``/in/`` URL for a person and a LinkedIn ``/company/`` URL or an explicit HTTPS website for an
    organization into producer-verified entity-profile metadata, so a social URL never reaches
    ``canonical_events.entity_profiles`` and never reaches ``fn_event_entity_profiles_valid``.  This
    value is carried alongside the profiles, on the candidate only, and is written to the separate
    enrichment plane (``catalog_entity_external_sources``) against the entity the catalog has
    already resolved for this ``(source, event, role, name)`` mention.

    ``CanonicalEvent`` deliberately has no counterpart field: the link is entity enrichment, not
    event metadata, and must not be persisted onto the event row.
    """

    name: str
    role: EventEntityRole
    provider_key: str
    profile_url: str

    def __post_init__(self) -> None:
        normalized_name = _public_name(self.name, "entity social link name")
        if normalized_name is None:
            raise ValueError("event entity social link name is invalid")
        if self.role not in _EVENT_ENTITY_ROLES:
            raise ValueError("event entity social link role is invalid")
        profile = social_profile_from_url(self.profile_url)
        if profile is None or profile.provider_key != self.provider_key:
            raise ValueError("event entity social link URL is not a canonical social profile")
        object.__setattr__(self, "name", normalized_name)
        object.__setattr__(self, "profile_url", profile.url)


def _public_entity_social_links(
    links: tuple[EventEntitySocialLink, ...],
    *,
    organizer_name: str | None,
    host_names: tuple[str, ...],
    speaker_names: tuple[str, ...],
    partner_names: tuple[str, ...],
) -> tuple[EventEntitySocialLink, ...]:
    """Keep only links attached to a displayed public role, bounded and deduplicated.

    The same gate ``_public_entity_profiles`` applies: a name that is not displayed in a public role
    has no link here.  Guests and attendees are never displayed roles, so this is the second place
    -- after the adapter's own exclusion -- where an attendee record cannot contribute a profile.
    """
    if len(links) > MAX_EVENT_ENTITY_SOCIAL_LINKS:
        raise ValueError("event entity_social_links exceeds its public link limit")
    allowed_names = {
        "organizer": (
            frozenset({organizer_name.casefold()})
            if organizer_name is not None
            else frozenset()
        ),
        "host": frozenset(name.casefold() for name in host_names),
        "speaker": frozenset(name.casefold() for name in speaker_names),
        "partner": frozenset(name.casefold() for name in partner_names),
    }
    seen: set[tuple[str, str, str]] = set()
    normalized: list[EventEntitySocialLink] = []
    for link in links:
        if not isinstance(link, EventEntitySocialLink):
            raise ValueError("event entity_social_links contains an invalid link")
        identity = (link.role, link.name.casefold(), link.provider_key)
        if identity in seen:
            raise ValueError("event entity_social_links contains duplicate role identities")
        if link.name.casefold() not in allowed_names[link.role]:
            raise ValueError("event entity social link is not attached to a displayed role")
        seen.add(identity)
        normalized.append(link)
    return tuple(normalized)


def event_entity_profiles_from_payload(value: object) -> tuple[EventEntityProfile, ...]:
    """Recreate a bounded typed profile tuple from a database JSON value."""
    if not isinstance(value, list) or len(value) > MAX_EVENT_ENTITY_PROFILES:
        raise ValueError("event entity profiles payload is invalid")
    profiles: list[EventEntityProfile] = []
    for raw_profile in value:
        if (
            not isinstance(raw_profile, Mapping)
            or set(raw_profile) != {"name", "role", "kind", "profile_url"}
        ):
            raise ValueError("event entity profile payload is invalid")
        name = raw_profile.get("name")
        role = raw_profile.get("role")
        kind = raw_profile.get("kind")
        profile_url = raw_profile.get("profile_url")
        if (
            not isinstance(name, str)
            or not isinstance(role, str)
            or not isinstance(kind, str)
            or not isinstance(profile_url, str)
        ):
            raise ValueError("event entity profile payload is invalid")
        profiles.append(
            EventEntityProfile(
                name=name,
                role=cast(EventEntityRole, role),
                kind=cast(EventEntityKind, kind),
                profile_url=profile_url,
            )
        )
    return tuple(profiles)


def event_entity_profiles_payload(
    profiles: tuple[EventEntityProfile, ...],
) -> list[dict[str, str]]:
    """Serialize profiles after the event-level role/name boundary has validated them."""
    return [profile.as_payload() for profile in profiles]


def _public_entity_profiles(
    profiles: tuple[EventEntityProfile, ...],
    *,
    organizer_name: str | None,
    host_names: tuple[str, ...],
    speaker_names: tuple[str, ...],
    partner_names: tuple[str, ...],
) -> tuple[EventEntityProfile, ...]:
    if len(profiles) > MAX_EVENT_ENTITY_PROFILES:
        raise ValueError("event entity_profiles exceeds its public profile limit")
    allowed_names = {
        "organizer": (
            frozenset({organizer_name.casefold()})
            if organizer_name is not None
            else frozenset()
        ),
        "host": frozenset(name.casefold() for name in host_names),
        "speaker": frozenset(name.casefold() for name in speaker_names),
        "partner": frozenset(name.casefold() for name in partner_names),
    }
    seen: set[tuple[str, str]] = set()
    normalized: list[EventEntityProfile] = []
    for profile in profiles:
        if not isinstance(profile, EventEntityProfile):
            raise ValueError("event entity_profiles contains an invalid profile")
        identity = (profile.role, profile.name.casefold())
        if identity in seen:
            raise ValueError("event entity_profiles contains duplicate role identities")
        if profile.name.casefold() not in allowed_names[profile.role]:
            raise ValueError("event entity profile is not attached to a displayed role")
        seen.add(identity)
        normalized.append(profile)
    return tuple(normalized)


def _public_price_range(
    price_status: PriceStatus,
    price_min_cents: int | None,
    price_max_cents: int | None,
    price_currency: str | None,
) -> tuple[int | None, int | None, str | None]:
    """Normalize one complete paid offer without inventing a partial public price."""
    values = (price_min_cents, price_max_cents, price_currency)
    if all(value is None for value in values):
        return None, None, None
    if any(value is None for value in values):
        raise ValueError("event price range must be complete")
    if price_status is not PriceStatus.PAID:
        raise ValueError("only a paid event may expose a price range")
    if (
        isinstance(price_min_cents, bool)
        or not isinstance(price_min_cents, int)
        or isinstance(price_max_cents, bool)
        or not isinstance(price_max_cents, int)
        or not 1 <= price_min_cents <= price_max_cents <= MAX_PUBLIC_PRICE_CENTS
    ):
        raise ValueError("event price range is invalid")
    if not isinstance(price_currency, str):
        raise ValueError("event price currency is invalid")
    normalized_currency = price_currency.strip().upper()
    if (
        len(normalized_currency) != _ISO_CURRENCY_CODE_LENGTH
        or not normalized_currency.isascii()
        or not normalized_currency.isalpha()
    ):
        raise ValueError("event price currency is invalid")
    return price_min_cents, price_max_cents, normalized_currency


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
    price_min_cents: int | None = None
    price_max_cents: int | None = None
    price_currency: str | None = None


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
    price_min_cents: int | None = None
    price_max_cents: int | None = None
    price_currency: str | None = None
    organizer_name: str | None = None
    host_names: tuple[str, ...] = ()
    speaker_names: tuple[str, ...] = ()
    partner_names: tuple[str, ...] = ()
    entity_profiles: tuple[EventEntityProfile, ...] = ()
    entity_social_links: tuple[EventEntitySocialLink, ...] = ()
    attendance_count: int | None = None
    registration_status: RegistrationStatus = RegistrationStatus.UNKNOWN

    def __post_init__(self) -> None:
        """Reconcile legacy fields and bound public role metadata before persistence."""
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
        price_min_cents, price_max_cents, price_currency = _public_price_range(
            resolved,
            self.price_min_cents,
            self.price_max_cents,
            self.price_currency,
        )
        object.__setattr__(self, "price_min_cents", price_min_cents)
        object.__setattr__(self, "price_max_cents", price_max_cents)
        object.__setattr__(self, "price_currency", price_currency)
        object.__setattr__(
            self,
            "organizer_name",
            _public_name(self.organizer_name, "organizer_name"),
        )
        for field_name in ("host_names", "speaker_names", "partner_names"):
            object.__setattr__(
                self,
                field_name,
                _public_names(tuple(getattr(self, field_name)), field_name),
            )
        object.__setattr__(
            self,
            "entity_profiles",
            _public_entity_profiles(
                tuple(self.entity_profiles),
                organizer_name=self.organizer_name,
                host_names=self.host_names,
                speaker_names=self.speaker_names,
                partner_names=self.partner_names,
            ),
        )
        object.__setattr__(
            self,
            "entity_social_links",
            _public_entity_social_links(
                tuple(self.entity_social_links),
                organizer_name=self.organizer_name,
                host_names=self.host_names,
                speaker_names=self.speaker_names,
                partner_names=self.partner_names,
            ),
        )
        if (
            self.attendance_count is not None
            and (
                isinstance(self.attendance_count, bool)
                or not 0 <= self.attendance_count <= _MAX_PUBLIC_ATTENDANCE
            )
        ):
            raise ValueError("event attendance_count is invalid")
        object.__setattr__(
            self,
            "registration_status",
            RegistrationStatus(self.registration_status),
        )


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
    price_min_cents: int | None = None
    price_max_cents: int | None = None
    price_currency: str | None = None
    embedding: list[float] | None = None
    source_links: list[EventSourceLink] = field(default_factory=list)
    normalizer_version: int = 1
    merge_version: int = 1
    organizer_name: str | None = None
    host_names: tuple[str, ...] = ()
    speaker_names: tuple[str, ...] = ()
    partner_names: tuple[str, ...] = ()
    entity_profiles: tuple[EventEntityProfile, ...] = ()
    attendance_count: int | None = None
    registration_status: RegistrationStatus = RegistrationStatus.UNKNOWN
    topics: tuple[str, ...] = ()
    extraction_evidence: tuple[ExtractionEvidence, ...] = ()

    def __post_init__(self) -> None:
        """Reject a database/API profile that is detached from its displayed public role."""
        self.entity_profiles = _public_entity_profiles(
            tuple(self.entity_profiles),
            organizer_name=self.organizer_name,
            host_names=tuple(self.host_names),
            speaker_names=tuple(self.speaker_names),
            partner_names=tuple(self.partner_names),
        )
        if len(self.topics) > len(CATALOG_TOPICS) or any(
            topic not in CATALOG_TOPICS for topic in self.topics
        ):
            raise ValueError("event topics contain an unsupported value")
        self.topics = tuple(dict.fromkeys(self.topics))
        if len(self.extraction_evidence) > MAX_EXTRACTION_EVIDENCE or any(
            not isinstance(item, ExtractionEvidence) for item in self.extraction_evidence
        ):
            raise ValueError("event extraction evidence is invalid")

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


def aggregate_price_range(
    observations: Iterable[tuple[int | None, int | None, str | None]],
) -> tuple[int | None, int | None, str | None]:
    """Expose an exact range only when every retained source reports the same complete offer."""
    ranges = frozenset(observations)
    if len(ranges) != 1:
        return None, None, None
    price_min_cents, price_max_cents, price_currency = next(iter(ranges))
    if (
        price_min_cents is None
        or price_max_cents is None
        or price_currency is None
    ):
        return None, None, None
    return price_min_cents, price_max_cents, price_currency
