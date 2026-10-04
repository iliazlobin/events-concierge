"""Public entity explorer values projected from exact event-role evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

CatalogEntityKind = Literal["person", "organization", "unknown"]
CatalogEntityIdentityStatus = Literal["profile_verified", "source_scoped"]
CatalogEntityResearchStatus = Literal[
    "identity_required",
    "researchable",
    "queued",
    "researching",
    "review_pending",
]
CatalogEntityExternalSourceStatus = Literal["linked", "fresh", "failed", "blocked"]
CatalogEntityFactKey = Literal[
    "description",
    "website",
    "location",
    "country",
    "founded",
    "entity_type",
    "focus",
    "industry",
    "profile",
    "job_title",
    "organization",
    "known_for",
    "public_repositories",
    "followers",
    "avatar",
]


@dataclass(frozen=True, slots=True)
class CatalogEntity:
    entity_id: UUID
    display_name: str
    kind: CatalogEntityKind
    identity_status: CatalogEntityIdentityStatus
    canonical_profile_url: str | None
    summary: str | None
    website_url: str | None
    logo_url: str | None
    city: str | None
    country: str | None
    event_count: int
    roles: tuple[str, ...]
    source_count: int
    research_status: CatalogEntityResearchStatus


@dataclass(frozen=True, slots=True)
class CatalogEntityEvent:
    canonical_event_id: UUID
    title: str
    start_at: datetime
    end_at: datetime | None
    venue_name: str | None
    city: str | None
    description: str
    roles: tuple[str, ...]
    source_labels: tuple[str, ...]
    registration_url: str
    is_past: bool = False


@dataclass(frozen=True, slots=True)
class CatalogEntityCollaborator:
    """Another catalog entity that recurs on this entity's events."""

    entity_id: UUID
    display_name: str
    kind: CatalogEntityKind
    shared_event_count: int


@dataclass(frozen=True, slots=True)
class CatalogEntityInsights:
    """Insights derived from our own admitted catalog, never from a provider.

    These cover source-scoped (name-only) entities too, which the enrichment plane deliberately
    never researches.  ``events_per_month`` is measured over a bounded recent-activity window
    rather than the whole observed span, so one far-future outlier cannot flatten the rate.
    """

    event_count: int
    upcoming_count: int
    past_count: int
    first_event_at: datetime | None
    last_event_at: datetime | None
    recent_event_count: int
    active_months: int
    events_per_month: float | None
    typical_attendance: int | None
    free_count: int
    paid_count: int
    top_topics: tuple[str, ...]
    top_venues: tuple[str, ...]
    top_cities: tuple[str, ...]
    source_labels: tuple[str, ...]
    collaborators: tuple[CatalogEntityCollaborator, ...]


@dataclass(frozen=True, slots=True)
class CatalogEntityExternalFact:
    provider_key: str
    source_url: str
    fact_key: CatalogEntityFactKey
    value: str
    value_url: str | None
    sort_order: int
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class CatalogEntityExternalSource:
    provider_key: str
    external_id: str
    source_url: str
    display_name: str
    status: CatalogEntityExternalSourceStatus
    fetched_at: datetime | None
    next_refresh_at: datetime
    error_code: str | None


@dataclass(frozen=True, slots=True)
class CatalogEntityExternalFactDraft:
    fact_key: CatalogEntityFactKey
    value: str
    value_url: str | None = None
    sort_order: int = 0


@dataclass(frozen=True, slots=True)
class CatalogEntityExternalSourceSnapshot:
    entity_id: UUID
    provider_key: str
    external_id: str
    source_url: str
    display_name: str
    status: CatalogEntityExternalSourceStatus
    fetched_at: datetime | None
    next_refresh_at: datetime
    error_code: str | None
    facts: tuple[CatalogEntityExternalFactDraft, ...] = ()


@dataclass(frozen=True, slots=True)
class CatalogEntityDetail:
    entity: CatalogEntity
    events: tuple[CatalogEntityEvent, ...]
    external_sources: tuple[CatalogEntityExternalSource, ...] = ()
    external_facts: tuple[CatalogEntityExternalFact, ...] = ()
    refresh_due: bool = False
    insights: CatalogEntityInsights | None = None
