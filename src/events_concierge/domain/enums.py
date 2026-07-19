"""Closed enumerations that pin the domain vocabulary (requirements section 3 + FR-5/FR-8/FR-10)."""

from __future__ import annotations

from enum import StrEnum


class Source(StrEnum):
    """An external event surface. PUBLIC_JSONLD is the free-crawl foundation source."""

    PUBLIC_JSONLD = "public_jsonld"
    MEETUP = "meetup"
    TICKETMASTER = "ticketmaster"
    LUMA = "luma"
    EVENTBRITE = "eventbrite"
    PARTIFUL = "partiful"
    SERPAPI = "serpapi"


class Modality(StrEnum):
    API = "api"
    BROWSER = "browser"


class Lane(StrEnum):
    """Routing class for a (source, modality, group-condition) triple (FR-5.1, ADR-003)."""

    AUTONOMOUS_SLA = "autonomous_sla"
    BROWSER_BEST_EFFORT = "browser_best_effort"
    HANDOFF = "handoff"
    DISABLED = "disabled"
    REFUSED = "refused"  # ToS-prohibited modality; structurally unreachable


class GroupCondition(StrEnum):
    """Membership/join condition governing whether an autonomous RSVP is permitted (FR-5.2, d17)."""

    MEMBER = "member"
    NON_MEMBER = "non_member"
    OPEN_INSTANT_JOIN = "open_instant_join"  # only on-SLA if the G2 spike confirms auto-join
    APPROVAL_GATED = "approval_gated"
    DUES_REQUIRED = "dues_required"
    UNKNOWN = "unknown"


class LifecycleState(StrEnum):
    """Per-(user, event) progression (requirements section 3). Terminal states end the workflow."""

    FOUND = "found"
    HANDOFF = "handoff"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    REGISTERED = "registered"
    SCHEDULED = "scheduled"
    RECONCILED = "reconciled"
    WITHDRAWING = "withdrawing"
    # Terminals:
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED_NO_CANDIDATE = "failed_no_candidate"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_STATES


_TERMINAL_STATES = frozenset(
    {
        LifecycleState.COMPLETED,
        LifecycleState.CANCELLED,
        LifecycleState.EXPIRED,
        LifecycleState.FAILED_NO_CANDIDATE,
    }
)


class RsvpState(StrEnum):
    """Result of a read-only detect / read-before-mutate (FR-5.3, FR-5.5)."""

    CONFIRMED = "confirmed"
    PENDING_CONFIRMATION = "pending_confirmation"
    NOT_PRESENT = "not_present"
    AMBIGUOUS = "ambiguous"


class EventStatus(StrEnum):
    """schema.org eventStatus, normalized by the ACL (FR-8.7a)."""

    SCHEDULED = "scheduled"
    CANCELLED = "cancelled"
    RESCHEDULED = "rescheduled"


class PriceStatus(StrEnum):
    """What the catalog can truthfully assert about an event's public price (FR-3.7/FR-4.6)."""

    FREE = "free"
    PAID = "paid"
    UNKNOWN = "unknown"

    @property
    def is_free(self) -> bool | None:
        """Compatibility projection for ACLs that still expose tri-state ``is_free`` values."""
        if self is PriceStatus.FREE:
            return True
        if self is PriceStatus.PAID:
            return False
        return None

    @classmethod
    def from_is_free(cls, is_free: bool | None) -> PriceStatus:
        """Lift legacy free/paid/unknown parsing into the explicit catalog vocabulary."""
        if is_free is True:
            return cls.FREE
        if is_free is False:
            return cls.PAID
        return cls.UNKNOWN


class CatalogSourceMode(StrEnum):
    """Approved public-catalog ingestion mechanism (FR-3.1/FR-10.3)."""

    PUBLIC_JSONLD = "public_jsonld"
    LIVEWHALE_JSON = "livewhale_json"
    SF_GOV_JSON = "sf_gov_json"
    DATASF_OUR415 = "datasf_our415"
    BIBLIOCOMMONS_RSS = "bibliocommons_rss"
    SAN_JOSE_LEGISTAR = "san_jose_legistar"
    SUNNYVALE_LEGISTAR = "sunnyvale_legistar"
    ALAMEDA_LEGISTAR = "alameda_legistar"
    OAKLAND_LEGISTAR = "oakland_legistar"
    COMMUNICO_JSON = "communico_json"
    TRIBE_EVENTS_JSON = "tribe_events_json"
    LOCALIST_JSON = "localist_json"
    LIBCAL_ICS = "libcal_ics"
    CIVIC_ENGAGE_RSS = "civic_engage_rss"
    MIDPEN_HTML = "midpen_html"
    USFCA_HTML = "usfca_html"
    CAL_PERFORMANCES_JSON = "calperformances_json"
    BERKELEY_REP_HTML = "berkeley_rep_html"
    YBCA_HTML = "ybca_html"
    OAKLAND_HTML = "oakland_html"


class CatalogRefreshRunStatus(StrEnum):
    """Durable state of one source/time-bucket refresh attempt (NFR-8)."""

    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class CatalogRefreshClaimOutcome(StrEnum):
    """Whether a worker owns a durable catalog-refresh lease (NFR-8)."""

    ACQUIRED = "acquired"
    BUSY = "busy"
    SUCCEEDED = "succeeded"


class HandoffReason(StrEnum):
    BROWSER_FAIL = "browser_fail"
    CAPTCHA = "captcha"
    IDENTITY_WALL = "identity_wall"
    APPROVAL_GATED = "approval_gated"
    DUES = "dues"
    DEFERRED_REGISTER = "deferred_register"
    SATURATION = "saturation"
    UNEXPECTED_PAYWALL = "unexpected_paywall"
    NO_AUTONOMOUS_LANE = "no_autonomous_lane"
    CALENDAR_WRITE_FAILED = "calendar_write_failed"
    WITHDRAWAL_REQUIRED = "withdrawal_required"


class HandoffState(StrEnum):
    OPEN = "open"
    NOTIFIED = "notified"
    COMPLETED = "completed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class HandoffReminderKind(StrEnum):
    """The owner-ratified durable reminder cadence for an open handoff (ADR-007)."""

    T24H = "t24h"
    T5D = "t5d"


class HandoffReminderStatus(StrEnum):
    """One guarded reminder-enqueue outcome, never a lifecycle transition (ADR-007/009)."""

    ENQUEUED = "enqueued"
    ALREADY_ENQUEUED = "already_enqueued"
    NOT_DUE = "not_due"
    INACTIVE = "inactive"


class CredentialKind(StrEnum):
    OAUTH_REFRESH = "oauth_refresh"
    SESSION_COOKIE = "session_cookie"
    PASSWORD_FALLBACK = "password_fallback"


class CredentialStatus(StrEnum):
    ACTIVE = "active"
    NEEDS_REAUTH = "needs_reauth"
    REVOKED = "revoked"


class ConsentScope(StrEnum):
    """Closed P14c consent-evidence purposes; no scope implies another (FR-2.9)."""

    REGISTRATION = "registration"


class ConflictVerdict(StrEnum):
    """Output of the mandatory pre-registration conflict gate (FR-4.5)."""

    OK = "ok"
    DEMOTE = "demote"  # soft: near-adjacent / tentative / transparent
    BLOCKED = "blocked"  # hard overlap -> never attempted
