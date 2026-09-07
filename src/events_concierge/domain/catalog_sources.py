"""Reviewed public-catalog source and durable refresh value objects (FR-3.1/FR-10.3/10.4).

Catalog sources are tenant-neutral editorial/configuration records. They deliberately describe only
read-only, handoff-only public discovery surfaces; source adapters and registration policies remain
separate concerns.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256
from urllib.parse import urlsplit
from uuid import UUID

from .enums import (
    CatalogRefreshClaimOutcome,
    CatalogRefreshRunStatus,
    CatalogSourceMode,
    PriceStatus,
    Source,
)
from .events import CandidateEvent, event_entity_profiles_payload

_SOURCE_KEY = re.compile(r"[a-z0-9][a-z0-9-]{1,79}")
_MAX_PAGED_CATALOG_RECORDS = 100
_PAGED_LEGISTAR_PROFILES = frozenset(
    {
        ("san-jose-legistar-meetings", CatalogSourceMode.SAN_JOSE_LEGISTAR),
        ("sunnyvale-legistar-meetings", CatalogSourceMode.SUNNYVALE_LEGISTAR),
        ("alameda-legistar-meetings", CatalogSourceMode.ALAMEDA_LEGISTAR),
        ("oakland-legistar-meetings", CatalogSourceMode.OAKLAND_LEGISTAR),
    }
)


def _https_origin(url: str) -> str | None:
    """Return a normalized HTTPS origin, or ``None`` for an unsafe/malformed URL."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if parsed.scheme.lower() != "https" or not hostname:
        return None
    suffix = f":{port}" if port is not None else ""
    return f"https://{hostname.lower()}{suffix}"

# The slowest cadence a reviewed source may be scheduled on. It bounds how long a shared Pacer
# bucket can sit idle between two ordinary uses, which is what
# ``Settings.pacer_state_retention_seconds`` has to outlast: a bucket that expires between one
# refresh and the next is indistinguishable from lost state and is deferred before any provider
# call (ADR-005).
MAX_SOURCE_REFRESH_INTERVAL_MINUTES = 1_440

@dataclass(frozen=True, slots=True)
class CatalogSource:
    """One owner-reviewed, public, handoff-only discovery seed (FR-3.1/FR-10.3)."""

    source_key: str
    display_name: str
    publisher: str
    seed_url: str
    approved_origins: tuple[str, ...]
    region: str
    mode: CatalogSourceMode
    enabled: bool
    reviewed_at: datetime | None
    review_expires_at: datetime | None
    refresh_interval_minutes: int
    min_interval_ms: int
    page_limit: int = 1
    handoff_only: bool = True
    source_revision: int = 1

    def __post_init__(self) -> None:
        """Reject unsafe registry records before an adapter can be called (FR-10.3)."""
        if _SOURCE_KEY.fullmatch(self.source_key) is None:
            raise ValueError("catalog source_key must be lowercase slug text")
        if not self.display_name.strip() or not self.publisher.strip() or not self.region.strip():
            raise ValueError("catalog source display_name, publisher, and region are required")
        if self.refresh_interval_minutes <= 0 or self.min_interval_ms <= 0 or self.page_limit <= 0:
            raise ValueError("catalog source refresh and pacing intervals must be positive")
        if self.refresh_interval_minutes > MAX_SOURCE_REFRESH_INTERVAL_MINUTES:
            raise ValueError(
                "catalog source refresh_interval_minutes exceeds the supported cadence ceiling"
            )
        if self.source_revision <= 0:
            raise ValueError("catalog source_revision must be positive")
        seed_origin = _https_origin(self.seed_url)
        if seed_origin is None:
            raise ValueError("catalog source seed_url must use HTTPS and include a host")
        origins = tuple(_https_origin(origin) for origin in self.approved_origins)
        if not origins or any(origin is None for origin in origins):
            raise ValueError("catalog source approved_origins must contain HTTPS origins")
        normalized_origins = tuple(origin for origin in origins if origin is not None)
        if len(set(normalized_origins)) != len(normalized_origins):
            raise ValueError("catalog source approved_origins must be unique")
        if seed_origin not in normalized_origins:
            raise ValueError("catalog source seed_url origin must be owner-approved")
        object.__setattr__(self, "approved_origins", normalized_origins)

    def allows_url(self, url: str) -> bool:
        """Permit only an HTTPS final URL on one explicitly approved origin (FR-10.3)."""
        origin = _https_origin(url)
        return origin is not None and origin in self.approved_origins

    def is_refreshable_at(self, now: datetime) -> bool:
        """Enabled but unreviewed or expired records make zero external requests (FR-10.3)."""
        return (
            self.enabled
            and self.reviewed_at is not None
            and (self.review_expires_at is None or self.review_expires_at > now)
        )

    @property
    def has_single_http_get(self) -> bool:
        """Whether this reviewed adapter contract has one closed request and no cursor (NFR-8).

        P15a intentionally recognizes only the closed LibCal document profile.  Every paginated,
        redirecting, count-plus-page, or detail-follow-up mode remains outside the durable-timer
        path until P15b persists a guarded request cursor and staging state (ADR-003/005).
        """
        return self.mode is CatalogSourceMode.LIBCAL_ICS and self.page_limit == 1

    @property
    def has_paged_http_get(self) -> bool:
        """Whether this source must use the reviewed durable per-page execution spine (NFR-8).

        P15b/P15c/P15d/P15e deliberately admit only the separately reviewed San Jose, Sunnyvale,
        Alameda, and Oakland Legistar Events profiles. The exact key/mode fence prevents a registry
        mode edit from sending another city's pagination through a partially reviewed cursor
        implementation; every other Legistar source stays on the legacy bounded adapter path until it
        has its own reviewed durable profile (FR-10.3/10.4, ADR-003/005).
        """
        return (self.source_key, self.mode) in _PAGED_LEGISTAR_PROFILES


@dataclass(frozen=True, slots=True)
class CatalogRefreshClaim:
    """The atomically granted (or denied) lease for one durable refresh run (NFR-8)."""

    outcome: CatalogRefreshClaimOutcome
    lease_token: UUID | None = None

    @property
    def acquired(self) -> bool:
        return self.outcome is CatalogRefreshClaimOutcome.ACQUIRED and self.lease_token is not None


@dataclass(frozen=True, slots=True)
class CatalogRefreshRun:
    """Operational result/provenance for a source-keyed refresh run (NFR-1/NFR-8)."""

    source_key: str
    run_key: str
    status: CatalogRefreshRunStatus
    started_at: datetime
    lease_expires_at: datetime | None
    completed_at: datetime | None
    candidate_count: int | None
    canonical_count: int | None
    error: str | None
    attempt_count: int


@dataclass(frozen=True, slots=True)
class CatalogRunExecutionEvidence:
    """One bounded worker-entry measurement; never a claim of host-wide attribution.

    Process CPU/RSS fields are populated only by the sequential direct worker.  Shared Temporal
    activity workers record wall time while leaving process measurements null because concurrent
    activities make process deltas non-attributable to one run.
    """

    wall_time_ms: int
    process_cpu_time_ms: int | None
    rss_before_bytes: int | None
    rss_after_bytes: int | None
    boundary_observed_peak_rss_bytes: int | None
    process_lifetime_peak_rss_bytes: int | None
    measurement_source: str
    measurement_scope: str
    measurement_quality: str
    outcome_code: str


@dataclass(frozen=True, slots=True)
class CatalogRunStageEvidence:
    """One monotonic timing for a closed, payload-free refresh stage."""

    stage: str
    duration_ms: int
    outcome_code: str


@dataclass(frozen=True, slots=True)
class CatalogPagedRefreshProgress:
    """A frozen P15b Legistar query window and the next durable page cursor (NFR-8).

    The source revision and local calendar day are persisted before the first page.  A retry can
    therefore re-enter the same bounded OData query without trusting a worker clock or carrying
    event payloads in Temporal history (FR-10.3/10.4, ADR-003/005).
    """

    source_key: str
    run_key: str
    source_revision: int
    window_start_day: date
    next_page: int
    page_limit: int
    terminal_page: int | None
    staged_raw_count: int
    staged_candidate_count: int

    def __post_init__(self) -> None:
        """Reject a malformed database capability response before it can shape egress."""
        if self.source_revision <= 0 or self.page_limit <= 0 or self.next_page < 0:
            raise ValueError("catalog paged refresh progress contains an invalid cursor")
        if self.next_page > self.page_limit:
            raise ValueError("catalog paged refresh cursor exceeds its reviewed page cap")
        if self.terminal_page is not None and not 0 <= self.terminal_page < self.page_limit:
            raise ValueError("catalog paged refresh terminal page is outside its cap")
        if self.staged_raw_count < 0 or self.staged_candidate_count < 0:
            raise ValueError("catalog paged refresh staged counts must be non-negative")
        if self.staged_candidate_count > self.staged_raw_count:
            raise ValueError("catalog paged refresh candidates cannot exceed raw source records")

    @property
    def is_terminal(self) -> bool:
        """Return whether all source pages are staged and promotion is the only remaining step."""
        return self.terminal_page is not None


@dataclass(frozen=True, slots=True)
class CatalogSourcePage:
    """One normalized Legistar HTTP page, stripped of its raw provider document (FR-3.7/NFR-1).

    ``source_event_ids`` retain every valid remote ``EventId`` -- including rows that normalize to
    no candidate -- so the durable stage still detects a repeated remote record across pages.  Raw
    JSON never crosses this value object or reaches the staging ledger (FR-10.3, NFR-8).
    """

    page_number: int
    raw_count: int
    candidates: tuple[CandidateEvent, ...]
    source_event_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        """Keep the fixed Legistar page shape and duplicate guard bounded locally."""
        if self.page_number < 0 or not 0 <= self.raw_count <= _MAX_PAGED_CATALOG_RECORDS:
            raise ValueError("catalog source page has an invalid page number or raw count")
        if len(self.candidates) > self.raw_count:
            raise ValueError("catalog source page candidates cannot exceed raw records")
        if len(self.source_event_ids) > self.raw_count:
            raise ValueError("catalog source page source identities cannot exceed raw records")
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("catalog source page repeated a source event identity")
        if any(
            not source_event_id.isdecimal() or int(source_event_id) <= 0
            for source_event_id in self.source_event_ids
        ):
            raise ValueError("catalog source page source identities must be positive decimal ids")


@dataclass(frozen=True, slots=True)
class CatalogPagedRefreshPreparation:
    """One capability-fenced page plan returned after a P15b lease claim (NFR-8)."""

    status: str
    progress: CatalogPagedRefreshProgress | None = None

    def __post_init__(self) -> None:
        """Permit only explicit capability projections; a ready response needs a cursor."""
        if self.status not in {"ready", "lease_lost", "source_changed", "invalid"}:
            raise ValueError("catalog paged refresh preparation has an unknown status")
        if (self.status == "ready") != (self.progress is not None):
            raise ValueError("only a ready paged refresh preparation may carry progress")


@dataclass(frozen=True, slots=True)
class CatalogPagedStageResult:
    """Atomic page-stage outcome returned without exposing the staged candidate rows (NFR-8)."""

    status: str

    def __post_init__(self) -> None:
        """Keep all cursor/error outcomes closed and explicit at the application boundary."""
        if self.status not in {
            "more",
            "terminal",
            "cap_exceeded",
            "conflict",
            "stale_cursor",
            "lease_lost",
            "invalid",
        }:
            raise ValueError("catalog paged stage has an unknown status")


@dataclass(frozen=True, slots=True)
class CatalogPagedRefreshPromotion:
    """Counts committed by P15b's one-transaction stage-to-catalog promotion (NFR-8)."""

    candidate_count: int
    canonical_count: int

    def __post_init__(self) -> None:
        """Reject a malformed promotion projection before reporting a successful refresh."""
        if self.candidate_count < 0 or self.canonical_count < 0:
            raise ValueError("catalog paged promotion counts must be non-negative")
        if self.canonical_count > self.candidate_count:
            raise ValueError("catalog paged promotion canonical count exceeds candidate count")


@dataclass(frozen=True, slots=True)
class CatalogRefreshCommit:
    """Counts committed by a generic refresh's one-transaction catalog publication (NFR-8).

    A ``None`` result at the port boundary means the final guarded completion was rejected and the
    caller's transaction committed no publication. This value therefore represents only a completed
    canonical merge, provenance write, and guarded run success together (FR-3.8/FR-10.3, ADR-001/003).
    """

    candidate_count: int
    canonical_count: int

    def __post_init__(self) -> None:
        """Reject malformed committed counters before a worker reports success."""
        if self.candidate_count < 0 or self.canonical_count < 0:
            raise ValueError("catalog refresh commit counts must be non-negative")
        if self.canonical_count > self.candidate_count:
            raise ValueError("catalog refresh commit canonical count exceeds candidate count")


@dataclass(frozen=True, slots=True)
class CatalogRefreshDue:
    """One reviewed source whose next scheduled slot is eligible for dispatch (NFR-1/NFR-8).

    ``due_at`` is derived from the last successful refresh, or from the owner-review timestamp
    before the first success.  It is deliberately stable across failed/retried attempts, so all
    dispatchers use one source/slot run key rather than creating a new effect for every poll.
    """

    source: CatalogSource
    due_at: datetime
    last_succeeded_at: datetime | None

    def __post_init__(self) -> None:
        """Require UTC-convertible schedule facts before deriving an idempotency key."""
        if self.due_at.tzinfo is None or self.due_at.utcoffset() is None:
            raise ValueError("catalog refresh due_at must be timezone-aware")
        if self.last_succeeded_at is not None:
            if self.last_succeeded_at.tzinfo is None or self.last_succeeded_at.utcoffset() is None:
                raise ValueError("catalog refresh last_succeeded_at must be timezone-aware")
            if self.last_succeeded_at > self.due_at:
                raise ValueError("catalog refresh due_at cannot precede its last successful run")

    def run_key(self) -> str:
        """Return the deterministic source/UTC-slot key shared by retrying dispatchers."""
        slot = self.due_at.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        return f"cadence:{self.source.source_key}:{slot}"


@dataclass(frozen=True, slots=True)
class CatalogSourceObservation:
    """One source-specific normalized event observation and its catalog attachment (FR-3.8/NFR-1)."""

    source_key: str
    source: Source
    source_event_id: str
    canonical_event_id: UUID
    registration_url: str
    price_status: PriceStatus
    content_hash: str
    price_min_cents: int | None = None
    price_max_cents: int | None = None
    price_currency: str | None = None
    first_seen_at: datetime | None = None
    last_seen_at: datetime | None = None
    last_run_key: str | None = None


def observation_for(
    source_key: str, candidate: CandidateEvent, canonical_event_id: UUID
) -> CatalogSourceObservation:
    """Create stable source/run provenance without persisting a potentially sensitive raw payload (NFR-1)."""
    return CatalogSourceObservation(
        source_key=source_key,
        source=candidate.source,
        source_event_id=candidate.source_event_id,
        canonical_event_id=canonical_event_id,
        registration_url=candidate.registration_url,
        price_status=candidate.price_status,
        content_hash=catalog_candidate_content_hash(candidate),
        price_min_cents=candidate.price_min_cents,
        price_max_cents=candidate.price_max_cents,
        price_currency=candidate.price_currency,
    )


def catalog_candidate_content_hash(candidate: CandidateEvent) -> str:
    """Hash the normalized public fields retained by both observations and P15b staging (NFR-1).

    The candidate's adapter-only ``raw`` mapping is intentionally absent.  That lets a paged run
    detect a changed source identity across retries while retaining the same provenance hash used
    after its one-transaction catalog promotion. Timestamps are reduced to their UTC instant
    because PostgreSQL ``timestamptz`` intentionally does not retain an input offset
    (FR-3.8/FR-10.3, NFR-8).
    """
    payload: dict[str, object] = {
        "source": candidate.source.value,
        "source_event_id": candidate.source_event_id,
        "title": candidate.title,
        "start_at": _canonical_catalog_timestamp(candidate.start_at),
        "end_at": (
            _canonical_catalog_timestamp(candidate.end_at) if candidate.end_at is not None else None
        ),
        "registration_url": candidate.registration_url,
        "venue_name": candidate.venue_name,
        "city": candidate.city,
        "description": candidate.description,
        "price_status": candidate.price_status.value,
        "organizer_name": candidate.organizer_name,
        "host_names": candidate.host_names,
        "speaker_names": candidate.speaker_names,
        "partner_names": candidate.partner_names,
        "attendance_count": candidate.attendance_count,
        "registration_status": candidate.registration_status.value,
    }
    if candidate.price_min_cents is not None:
        payload.update(
            {
                "price_min_cents": candidate.price_min_cents,
                "price_max_cents": candidate.price_max_cents,
                "price_currency": candidate.price_currency,
            }
        )
    # Preserve rolling compatibility for rows staged before 0128: their new JSONB column defaults
    # to an empty array, so an empty profile set must retain the pre-0128 content hash.
    if candidate.entity_profiles:
        payload["entity_profiles"] = event_entity_profiles_payload(candidate.entity_profiles)
    content_hash = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return content_hash


def _canonical_catalog_timestamp(value: datetime) -> str:
    """Serialize one database-bound event instant independently of its source UTC offset."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("catalog candidate content hash requires timezone-aware timestamps")
    return value.astimezone(UTC).isoformat()
