"""Provider-neutral entity enrichment vocabulary with evidence-first identity fences.

This module intentionally models orchestration and review state only.  It does not know how to
call a provider, does not accept provider payloads, and never treats a display name as identity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

_SOURCE_KEY = re.compile(r"[a-z0-9][a-z0-9-]{1,79}")
_SAFE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}")
_PROVIDER_KEY = re.compile(r"[a-z][a-z0-9_]{1,79}")
_LINKEDIN_HOSTS = frozenset({"linkedin.com", "www.linkedin.com"})
_LINKEDIN_PERSON_PATH = re.compile(r"/in/[A-Za-z0-9][A-Za-z0-9_%.-]*/?")
_LINKEDIN_ORGANIZATION_PATH = re.compile(r"/company/[A-Za-z0-9][A-Za-z0-9_%.-]*/?")
_CONTROL_CODEPOINT_MAX = 31
_DELETE_CODEPOINT = 127
_MAX_URL_LENGTH = 2_048
_MAX_CONFIDENCE_BPS = 10_000
_MAX_NON_URL_VALUE_LENGTH = 500


class EntityKind(StrEnum):
    """Public entity kind supported by event role facts."""

    PERSON = "person"
    ORGANIZATION = "organization"


class EntityRole(StrEnum):
    """Bounded public role asserted by an exact source event."""

    ORGANIZER = "organizer"
    HOST = "host"
    SPEAKER = "speaker"
    PARTNER = "partner"


class EntityIdentityBasis(StrEnum):
    """Stable source evidence accepted as identity; names are deliberately absent."""

    SOURCE_ENTITY_ID = "source_entity_id"
    SOURCE_PROFILE_URL = "source_profile_url"


class EntityEnrichmentJobStatus(StrEnum):
    """Durable lifecycle of one revision-bound provider job."""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"


class EntityEnrichmentEnqueueOutcome(StrEnum):
    """Safe enqueue result without provider or credential detail."""

    ENQUEUED = "enqueued"
    REPLAYED = "replayed"
    NOT_FOUND = "not_found"
    PROVIDER_DISABLED = "provider_disabled"
    KIND_UNSUPPORTED = "kind_unsupported"
    CONFLICT = "conflict"


class EntityEnrichmentField(StrEnum):
    """Typed, bounded enrichment fields; arbitrary provider payload keys are not retained."""

    PROFILE_URL = "profile_url"
    HEADLINE = "headline"
    JOB_TITLE = "job_title"
    ORGANIZATION_NAME = "organization_name"
    ORGANIZATION_PROFILE_URL = "organization_profile_url"
    SENIORITY = "seniority"
    FUNCTION = "function"
    INDUSTRY = "industry"
    COMPENSATION_MARKET = "compensation_market"


class EntityEnrichmentMatchBasis(StrEnum):
    """Evidence used to crosswalk a provider record; name-only matching is impossible."""

    SOURCE_ENTITY_ID = "source_entity_id"
    SOURCE_PROFILE_URL = "source_profile_url"
    PROVIDER_CROSSWALK = "provider_crosswalk"
    MANUAL_REVIEW = "manual_review"


class EntityObservationReviewStatus(StrEnum):
    """Human review state controlling later materialization eligibility."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class EntityObservationReviewDecision(StrEnum):
    """Allowed human decision; pending is not a review action."""

    APPROVED = "approved"
    REJECTED = "rejected"


class EntityObservationWriteOutcome(StrEnum):
    """Idempotent observation-write result."""

    RECORDED = "recorded"
    REPLAYED = "replayed"
    CONFLICT = "conflict"
    LEASE_LOST = "lease_lost"
    STALE = "stale"


class EntityObservationReviewOutcome(StrEnum):
    """Idempotent review result."""

    REVIEWED = "reviewed"
    REPLAYED = "replayed"
    NOT_FOUND = "not_found"
    NOT_READY = "not_ready"
    CONFLICT = "conflict"


class EntityEnrichmentFailureCode(StrEnum):
    """Normalized terminal/retry classifications; raw provider errors are never persisted."""

    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RATE_LIMITED = "rate_limited"
    TRANSIENT = "transient"
    INVALID_RESPONSE = "invalid_response"
    IDENTITY_UNRESOLVED = "identity_unresolved"
    POLICY_BLOCKED = "policy_blocked"


def _bounded_text(value: str, label: str, *, limit: int) -> str:
    normalized = " ".join(value.split())
    if (
        not normalized
        or len(normalized) > limit
        or any(
            ord(character) <= _CONTROL_CODEPOINT_MAX
            or ord(character) == _DELETE_CODEPOINT
            for character in normalized
        )
    ):
        raise ValueError(f"{label} is invalid")
    return normalized


def _opaque_key(value: str, label: str) -> str:
    normalized = value.strip()
    if not _SAFE_KEY.fullmatch(normalized):
        raise ValueError(f"{label} is invalid")
    return normalized


def _https_url(value: str, label: str, *, allow_query: bool) -> str:
    candidate = value.strip()
    if (
        not candidate
        or len(candidate) > _MAX_URL_LENGTH
        or any(
            character.isspace() or ord(character) <= _CONTROL_CODEPOINT_MAX
            for character in candidate
        )
    ):
        raise ValueError(f"{label} is invalid")
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as error:
        raise ValueError(f"{label} is invalid") from error
    if (
        parsed.scheme.lower() != "https"
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or (parsed.query and not allow_query)
    ):
        raise ValueError(f"{label} must be a direct HTTPS URL")
    return urlunsplit(parsed)


def direct_entity_profile_url(value: str, kind: EntityKind) -> str:
    """Validate a direct profile without accepting search/result or tracking URLs."""

    normalized = _https_url(value, "entity profile URL", allow_query=False)
    parsed = urlsplit(normalized)
    hostname = parsed.hostname.casefold() if parsed.hostname else ""
    if hostname in _LINKEDIN_HOSTS:
        expected = (
            _LINKEDIN_PERSON_PATH
            if kind is EntityKind.PERSON
            else _LINKEDIN_ORGANIZATION_PATH
        )
        if expected.fullmatch(parsed.path) is None:
            raise ValueError("entity LinkedIn URL must identify a direct profile")
    elif kind is EntityKind.PERSON:
        raise ValueError("person profiles require a direct LinkedIn profile URL")
    elif "/search" in parsed.path.casefold() or "/results" in parsed.path.casefold():
        raise ValueError("organization profile URL cannot be a search result")
    return normalized


def _aware(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return value


@dataclass(frozen=True, slots=True)
class SourceEntityFactInput:
    """One source-scoped entity plus its exact event-role assertion."""

    source_key: str
    source: str
    source_event_id: str
    source_run_key: str
    source_entity_key: str
    identity_basis: EntityIdentityBasis
    role: EntityRole
    entity_kind: EntityKind
    display_name: str
    direct_profile_url: str | None
    confidence_bps: int
    observed_at: datetime
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not _SOURCE_KEY.fullmatch(self.source_key):
            raise ValueError("source key is invalid")
        object.__setattr__(self, "source", _opaque_key(self.source, "source"))
        object.__setattr__(
            self,
            "source_event_id",
            _bounded_text(self.source_event_id, "source event id", limit=500),
        )
        object.__setattr__(
            self,
            "source_run_key",
            _opaque_key(self.source_run_key, "source run key"),
        )
        entity_key = _bounded_text(
            self.source_entity_key,
            "source entity key",
            limit=500,
        )
        display_name = _bounded_text(self.display_name, "entity display name", limit=160)
        if entity_key.casefold() == display_name.casefold():
            raise ValueError("source entity identity cannot be a display name")
        object.__setattr__(self, "source_entity_key", entity_key)
        object.__setattr__(self, "display_name", display_name)
        profile_url = (
            direct_entity_profile_url(self.direct_profile_url, self.entity_kind)
            if self.direct_profile_url is not None
            else None
        )
        if (
            self.identity_basis is EntityIdentityBasis.SOURCE_PROFILE_URL
            and (profile_url is None or entity_key != profile_url)
        ):
            raise ValueError("profile-based identity must equal its direct profile URL")
        object.__setattr__(self, "direct_profile_url", profile_url)
        if not 1 <= self.confidence_bps <= _MAX_CONFIDENCE_BPS:
            raise ValueError("source entity confidence must be between 1 and 10000")
        observed_at = _aware(self.observed_at, "observed_at")
        object.__setattr__(self, "observed_at", observed_at)
        if self.expires_at is not None:
            expires_at = _aware(self.expires_at, "expires_at")
            if expires_at <= observed_at:
                raise ValueError("source entity fact expiry must follow observation")
            object.__setattr__(self, "expires_at", expires_at)


@dataclass(frozen=True, slots=True)
class SourceEntityFact:
    """Persisted opaque entity and event-role fact returned after an upsert."""

    entity_id: UUID
    fact_id: UUID
    entity_revision: int
    value: SourceEntityFactInput

    def __post_init__(self) -> None:
        if self.entity_revision < 1:
            raise ValueError("entity revision must be positive")


@dataclass(frozen=True, slots=True)
class EntityEnrichmentLease:
    """Exact lease plus identity evidence required by a future approved provider adapter."""

    job_id: UUID
    entity_id: UUID
    provider_key: str
    requested_entity_revision: int
    attempt_count: int
    lease_token: UUID
    source_key: str
    source_entity_key: str
    identity_basis: EntityIdentityBasis
    entity_kind: EntityKind
    display_name: str
    direct_profile_url: str | None

    def __post_init__(self) -> None:
        if not _PROVIDER_KEY.fullmatch(self.provider_key):
            raise ValueError("entity enrichment provider key is invalid")
        if self.requested_entity_revision < 1 or self.attempt_count < 1:
            raise ValueError("entity enrichment lease revision/attempt is invalid")
        if self.source_entity_key.casefold() == self.display_name.casefold():
            raise ValueError("entity enrichment lease cannot carry name-only identity")


@dataclass(frozen=True, slots=True)
class EntityFieldObservationDraft:
    """One bounded field result; raw provider payloads and fuzzy name matches are excluded."""

    observation_id: UUID
    field: EntityEnrichmentField
    value: str
    provenance_url: str
    provider_record_id: str
    match_basis: EntityEnrichmentMatchBasis
    confidence_bps: int
    observed_at: datetime
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        value = _bounded_text(self.value, "entity observation value", limit=2_048)
        if self.field is EntityEnrichmentField.PROFILE_URL:
            # The owning entity kind is checked again inside the repository capability.
            value = _https_url(value, "profile observation", allow_query=False)
        elif self.field is EntityEnrichmentField.ORGANIZATION_PROFILE_URL:
            value = direct_entity_profile_url(value, EntityKind.ORGANIZATION)
        elif len(value) > _MAX_NON_URL_VALUE_LENGTH:
            raise ValueError("non-URL entity observation exceeds 500 characters")
        object.__setattr__(self, "value", value)
        object.__setattr__(
            self,
            "provenance_url",
            _https_url(self.provenance_url, "observation provenance URL", allow_query=True),
        )
        object.__setattr__(
            self,
            "provider_record_id",
            _bounded_text(self.provider_record_id, "provider record id", limit=500),
        )
        if not 1 <= self.confidence_bps <= _MAX_CONFIDENCE_BPS:
            raise ValueError("entity observation confidence must be between 1 and 10000")
        observed_at = _aware(self.observed_at, "observed_at")
        object.__setattr__(self, "observed_at", observed_at)
        if self.expires_at is not None:
            expires_at = _aware(self.expires_at, "expires_at")
            if expires_at <= observed_at:
                raise ValueError("entity observation expiry must follow observation")
            object.__setattr__(self, "expires_at", expires_at)


@dataclass(frozen=True, slots=True)
class MaterializableEntityObservation:
    """An approved, current field candidate returned by the guarded read capability."""

    observation_id: UUID
    fact_id: UUID
    entity_id: UUID
    role: EntityRole
    field: EntityEnrichmentField
    value: str
    provider_key: str
    provenance_url: str
    confidence_bps: int
    reviewed_at: datetime
    expires_at: datetime | None


def observation_is_materializable(
    *,
    review_status: EntityObservationReviewStatus,
    entity_kind: EntityKind,
    field: EntityEnrichmentField,
    direct_profile_url: str | None,
    expires_at: datetime | None,
    now: datetime,
) -> bool:
    """Mirror the persistence eligibility fence for pure domain/application decisions."""

    current = _aware(now, "now")
    return (
        review_status is EntityObservationReviewStatus.APPROVED
        and (expires_at is None or _aware(expires_at, "expires_at") > current)
        and not (
            direct_profile_url is not None
            and (
                field is EntityEnrichmentField.PROFILE_URL
                or (
                    entity_kind is EntityKind.ORGANIZATION
                    and field is EntityEnrichmentField.ORGANIZATION_PROFILE_URL
                )
            )
        )
    )
