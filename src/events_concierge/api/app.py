"""FastAPI application with an injected tenant-authentication boundary.

The offline graph resolves a test-only header through ``AuthContextPort``; it never trusts a tenant
identifier in a request body.  A provisioned graph must inject the real OIDC BFF/session adapter
(FR-1.1/1.3). The feed endpoint reads the persisted catalog; source refresh is a separate durable
worker path, never an HTTP side effect of user intake. Temporal remains optional for registration.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Sequence
from contextlib import asynccontextmanager, suppress
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from pathlib import Path
from typing import Annotated, Any, Literal, NamedTuple, cast
from unicodedata import category
from urllib.parse import urlencode
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..adapters.identity_platform import IdentityPlatformBrowserSessionAdapter
from ..adapters.oidc.session import _safe_return_path
from ..api.firebase_auth_helper import install_firebase_auth_helper
from ..api.muse import is_muse_path, muse_router
from ..api.release_profile import apply_release_profile
from ..application.discovery_results import lifecycle
from ..application.feed import MAX_FEED_OFFSET
from ..application.profile_media import AvatarRejectedError, normalize_avatar
from ..application.ranking_feedback import UnknownFeedbackEventError
from ..application.request_start import RequestIntakeService, RequestStartWorker
from ..composition import Container, build_container
from ..config import Settings, get_settings
from ..deployment.startup import preflight_application_runtime
from ..domain.account_erasure import AccountErasureStatus
from ..domain.catalog_browse import (
    MIN_CATALOG_NAME_QUERY_LENGTH,
    CatalogBrowseCursor,
    CatalogBrowseEvent,
    CatalogNameKind,
)
from ..domain.consumer import (
    ConsumerRegistrationSummary,
    ConsumerRequestSummary,
    ConsumerTaskSummary,
)
from ..domain.credentials import Tenant
from ..domain.enums import HandoffState, LifecycleState
from ..domain.event_semantics import CATALOG_TOPICS, MAX_CATALOG_TOPIC_SELECTIONS
from ..domain.lifecycle import HandoffCompletionTarget
from ..domain.ranking_feedback import FeedbackSignalKind, RankingFeedbackSignal
from ..domain.request import EventRequest, Feed, RequestConstraints
from ..infra.db import dispose_engine, system_session_scope
from ..infra.logging import configure_logging, get_logger
from ..infra.metrics import PROMETHEUS_CONTENT_TYPE, ApplicationMetrics, HttpMetricsMiddleware
from ..ports.account_erasure import (
    AccountErasureAccountNotFoundError,
    AccountErasureConflictError,
)
from ..ports.api_keys import ApiKeyRecord
from ..ports.auth import (
    AuthenticationFailedError,
    BrowserIdentity,
    BrowserSessionCredentials,
    BrowserSessionLifecyclePort,
    BrowserSessionUnavailableError,
    BrowserStepUpUnavailableError,
    ConsumerSignInFailureReason,
    ConsumerSignInRejectedError,
    CsrfVerificationFailedError,
    RecentAuthenticationRequiredError,
)
from ..ports.media_store import MediaNotFoundError
from ..ports.profile_avatar import ProfileAvatar, ProfileMediaMutationBusyError
from ..ports.ranking import RankingProfileUpdate, UserRankingProfile
from ..ports.ranking_feedback import RankingFeedbackConflictError
from ..ports.saved_catalog_filters import (
    MAX_SAVED_CATALOG_FILTERS,
    SavedCatalogFilter,
)
from ..ports.tenant_effects import (
    TenantEffectFencedError,
    TenantEffectKind,
    TenantEffectLockTimeoutError,
    TenantEffectRequest,
    TenantEffectTimedOutError,
)
from ..ports.tenant_profile import TenantProfile
from ..ports.tenant_roles import TenantRole
from ..ports.workflows import RegistrationLifecycleSignaler
from ..workflows.temporal_client import connect_temporal, validate_temporal_settings
from .admin import install_ingestion_admin_routes
from .command_investigation import install_command_investigation_routes
from .model_usage import install_model_usage_routes
from .operator import build_operator_services, install_operator_session_routes
from .operator_operations import install_operator_operations_routes

_log = get_logger("api")
_READINESS_TIMEOUT_SECONDS = 2.0
_MAX_REQUEST_BODY_BYTES = 64 * 1024
_ACCOUNT_ERASURE_WRITE_FENCE_SQLSTATE = "55000"
_ACCOUNT_ERASURE_WRITE_FENCE_MESSAGE = "tenant account is fenced for erasure"
_MAX_FEED_CURSOR = MAX_FEED_OFFSET
_MAX_INTEREST_LENGTH = 64
_MAX_TASK_ID_LENGTH = 512
_MAX_CATALOG_CURSOR_LENGTH = 256
_CATALOG_CURSOR_PARTS = 4
_CATALOG_CURSOR_VERSION = 2
_MAX_CATALOG_BROWSE_WINDOW = timedelta(days=370)
_MAX_SOURCE_CATALOG_ARCHIVE_WINDOW = timedelta(days=7_305)
_MAX_CATALOG_FILTER_LENGTH = 160
_MAX_SAVED_FILTER_NAME_LENGTH = 80
_MAX_SAVED_FILTER_KEYS = 32
_MAX_SAVED_FILTER_LIST_LENGTH = 64
_MAX_SAVED_FILTER_FIELD_NAME_LENGTH = 64
_MAX_SAVED_FILTER_ENTRY_KEYS = 8
# Mirrors saved_catalog_filters_payload_bounded in migration 0158.
_MAX_SAVED_FILTER_PAYLOAD_BYTES = 8192
_MAX_CATALOG_LOCATION_SELECTIONS = 20
_MAX_CATALOG_SOURCE_SELECTIONS = 40
_MAX_CATALOG_TIME_ZONE_LENGTH = 64
_MAX_CATALOG_DATE_RANGES = 8
_MAX_CATALOG_DATE_RANGE_LENGTH = 96
_MIN_PRINTABLE_CODEPOINT = 0x20
_DELETE_CODEPOINT = 0x7F
_FEED_CURSOR_PATTERN = rf"^(?:0|[1-9][0-9]{{0,{len(str(_MAX_FEED_CURSOR)) - 1}}})$"
_CATALOG_SOURCE_KEY_PATTERN = r"^[a-z0-9][a-z0-9-]{1,79}$"
_CATALOG_CURSOR_PATTERN = r"^[A-Za-z0-9_-]+$"
_STATIC_DIR = Path(__file__).with_name("static")
_STATIC_ASSETS = {
    "admin.css": ("text/css; charset=utf-8", "admin.css"),
    "admin.js": ("text/javascript; charset=utf-8", "admin.js"),
    "app.css": ("text/css; charset=utf-8", "app.css"),
    "app.js": ("text/javascript; charset=utf-8", "app.js"),
    "leaflet.css": ("text/css; charset=utf-8", "leaflet.css"),
    "leaflet.js": ("text/javascript; charset=utf-8", "leaflet.js"),
    "mark.svg": ("image/svg+xml", "mark.svg"),
}
_UI_SECURITY_HEADERS = {
    "Cache-Control": "no-cache, max-age=0",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data: https://tile.openstreetmap.org; "
        "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'"
    ),
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


class _MalformedContentLengthError(ValueError):
    """The edge supplied an ambiguous or syntactically invalid message boundary."""


class _RequestBodyTooLargeError(ValueError):
    """The decoded ASGI body exceeded the application boundary."""


class _RequestDisconnectedError(ConnectionError):
    """The caller left before completing its request body."""


def _declared_content_length(scope: Scope, limit: int) -> int | None:
    """Parse every Content-Length field without unbounded integer conversion.

    ASGI servers normally reject malformed framing before application dispatch, but this boundary
    remains defensive because tests, proxies, and alternate servers may preserve duplicate fields.
    Equal duplicates are harmless; conflicting values are ambiguous and fail closed.
    """
    raw_values = [
        value for name, value in scope.get("headers", ()) if name.lower() == b"content-length"
    ]
    if not raw_values:
        return None

    values: set[int] = set()
    limit_digits = len(str(limit))
    for raw_value in raw_values:
        for raw_token in raw_value.split(b","):
            token = raw_token.strip()
            if not token or any(byte < ord("0") or byte > ord("9") for byte in token):
                raise _MalformedContentLengthError
            significant = token.lstrip(b"0") or b"0"
            # A valid decimal with more digits than the configured limit can use one bounded
            # sentinel; converting attacker-sized decimal strings is unnecessary and unsafe.
            value = limit + 1 if len(significant) > limit_digits else int(significant)
            values.add(value)
    if len(values) != 1:
        raise _MalformedContentLengthError
    return values.pop()


class _BoundedRequestBodyMiddleware:
    """Buffer at most one small API body, including requests without Content-Length."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int,
        body_read_timeout_seconds: float,
    ) -> None:
        self._app = app
        self._max_body_bytes = max_body_bytes
        self._body_read_timeout_seconds = body_read_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method", "GET").upper() in {
            "GET",
            "HEAD",
            "OPTIONS",
            "TRACE",
        }:
            await self._app(scope, receive, send)
            return
        await self._handle_bounded_http(scope, receive, send)

    async def _handle_bounded_http(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            declared_length = _declared_content_length(scope, self._max_body_bytes)
        except _MalformedContentLengthError:
            await self._error_response(400, "invalid Content-Length")(scope, receive, send)
            return
        if declared_length is not None and declared_length > self._max_body_bytes:
            await self._error_response(413, "request body too large")(scope, receive, send)
            return

        try:
            async with asyncio.timeout(self._body_read_timeout_seconds):
                body = await self._read_body(receive)
        except TimeoutError:
            await self._error_response(408, "request body timed out")(scope, receive, send)
            return
        except _RequestBodyTooLargeError:
            await self._error_response(413, "request body too large")(scope, receive, send)
            return
        except _RequestDisconnectedError:
            return

        if declared_length is not None and declared_length != len(body):
            await self._error_response(400, "Content-Length does not match request body")(
                scope, receive, send
            )
            return
        replay = {"type": "http.request", "body": bytes(body), "more_body": False}
        await self._app(scope, self._single_message_receive(replay, receive), send)

    async def _read_body(self, receive: Receive) -> bytearray:
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                raise _RequestDisconnectedError
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            if len(chunk) > self._max_body_bytes - len(body):
                raise _RequestBodyTooLargeError
            body.extend(chunk)
            if not message.get("more_body", False):
                return body

    @staticmethod
    def _single_message_receive(message: Message, receive: Receive) -> Receive:
        delivered = False

        async def replay() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return message
            return await receive()

        return replay

    @staticmethod
    def _error_response(status_code: int, detail: str) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"detail": detail})


class OnboardBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    notify_email: str = Field(min_length=3, max_length=320)


class RequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    cursor: str | None = Field(
        default=None,
        max_length=len(str(_MAX_FEED_CURSOR)),
        pattern=_FEED_CURSOR_PATTERN,
    )

    @field_validator("cursor")
    @classmethod
    def cursor_is_bounded(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if (
            not value.isascii()
            or not value.isdecimal()
            or (len(value) > 1 and value.startswith("0"))
        ):
            raise ValueError("cursor must be a canonical nonnegative decimal")
        if int(value) > _MAX_FEED_CURSOR:
            raise ValueError(f"cursor must be no greater than {_MAX_FEED_CURSOR}")
        return value


class FeedSourceOut(BaseModel):
    source: str
    registration_url: str


class EventEntityProfileOut(BaseModel):
    name: str
    role: Literal["host", "organizer", "speaker", "partner"]
    kind: Literal["person", "organization"]
    profile_url: str


class EventExtractionEvidenceOut(BaseModel):
    field: Literal["topic", "price_status"]
    value: str
    source: Literal["provider_metadata", "title", "description"]
    rule: str


class EventOccurrenceOut(BaseModel):
    canonical_event_id: UUID
    start_at: str
    end_at: str | None
    registration_urls: list[str]


class FeedItemOut(BaseModel):
    additional_dates: list[EventOccurrenceOut] = Field(default_factory=list)
    discovery_state: str = "upcoming"
    source_freshness: str = "unknown"
    canonical_event_id: UUID
    title: str
    start_at: str
    price_status: str
    price_min_cents: int | None
    price_max_cents: int | None
    price_currency: str | None
    score: float
    rationale: str
    conflict: str
    lanes: list[str]
    registration_urls: list[str]
    end_at: str | None
    venue_name: str | None
    city: str | None
    description: str
    event_status: str
    latitude: float | None
    longitude: float | None
    registerable: bool
    sources: list[FeedSourceOut]
    organizer_name: str | None
    host_names: list[str]
    speaker_names: list[str]
    partner_names: list[str]
    entity_profiles: list[EventEntityProfileOut]
    attendance_count: int | None
    registration_status: str
    topics: list[str]
    extraction_evidence: list[EventExtractionEvidenceOut]


class FeedUnderstandingOut(BaseModel):
    categories: list[str]
    free_only: bool
    window_start: str | None
    window_end: str | None
    radius_km: float | None


class FeedOut(BaseModel):
    signals: dict[str, float] = Field(default_factory=dict)
    request_id: UUID
    items: list[FeedItemOut]
    next_cursor: str | None
    understood: FeedUnderstandingOut


class CatalogBrowseSourceOut(FeedSourceOut):
    source_key: str
    label: str
    publisher: str
    provider: str
    seed_url: str
    source_event_id: str
    last_seen_at: str
    refresh_run_key: str


class CatalogBrowseItemOut(BaseModel):
    """Feed-shaped catalog data with explicit not-ranked/not-conflict-checked sentinels."""

    discovery_state: str = "upcoming"
    source_freshness: str = "unknown"
    canonical_event_id: UUID
    title: str
    start_at: str
    price_status: str
    price_min_cents: int | None
    price_max_cents: int | None
    price_currency: str | None
    score: float | None
    rationale: str
    conflict: Literal["not_evaluated"]
    lanes: list[str]
    registration_urls: list[str]
    end_at: str | None
    venue_name: str | None
    city: str | None
    description: str
    event_status: str
    latitude: float | None
    longitude: float | None
    registerable: bool | None
    source_keys: list[str]
    providers: list[str]
    calendar_labels: list[str]
    sources: list[CatalogBrowseSourceOut]
    organizer_name: str | None
    host_names: list[str]
    speaker_names: list[str]
    partner_names: list[str]
    entity_profiles: list[EventEntityProfileOut]
    attendance_count: int | None
    registration_status: str
    topics: list[str]
    extraction_evidence: list[EventExtractionEvidenceOut]


class CatalogBrowseProviderOut(BaseModel):
    source_key: str
    label: str
    display_name: str
    publisher: str
    provider: str
    seed_url: str
    event_count: int


class CatalogBrowseTopicOut(BaseModel):
    topic: str
    label: str
    event_count: int


class CatalogBrowseCityOut(BaseModel):
    city: str
    event_count: int


class CatalogBrowsePageOut(BaseModel):
    items: list[CatalogBrowseItemOut]
    next_cursor: str | None
    providers: list[CatalogBrowseProviderOut]
    city_facets: list[CatalogBrowseCityOut]
    topic_facets: list[CatalogBrowseTopicOut]


class CatalogNameSuggestionOut(BaseModel):
    name: str
    kinds: list[CatalogNameKind]
    event_count: int


class CatalogDayTopicOut(BaseModel):
    topic: str
    label: str
    event_count: int


class CatalogDayOut(BaseModel):
    start_day: date
    event_count: int
    topics: list[CatalogDayTopicOut]


class CatalogDaySummaryOut(BaseModel):
    """Per-local-day counts for a calendar range, plus the range's own totals.

    ``event_count`` values count distinct events. A day's count is therefore not the sum of its
    topic counts, because one event may carry several topics; ``total_event_count`` is likewise
    not the sum of the days only when a multi-day range is requested with overlapping windows,
    which the API merges before reaching the capability.
    """

    days: list[CatalogDayOut]
    total_event_count: int
    time_zone: str


class CatalogEntityOut(BaseModel):
    entity_id: UUID
    display_name: str
    kind: Literal["person", "organization", "unknown"]
    identity_status: Literal["profile_verified", "source_scoped"]
    canonical_profile_url: str | None
    summary: str | None
    website_url: str | None
    logo_url: str | None
    city: str | None
    country: str | None
    event_count: int
    roles: list[str]
    source_count: int
    research_status: Literal[
        "identity_required", "researchable", "queued", "researching", "review_pending"
    ]


class CatalogEntityEventOut(BaseModel):
    canonical_event_id: UUID
    title: str
    start_at: str
    end_at: str | None
    venue_name: str | None
    city: str | None
    description: str
    roles: list[str]
    source_labels: list[str]
    registration_url: str
    is_past: bool


class CatalogEntityCollaboratorOut(BaseModel):
    entity_id: UUID
    display_name: str
    kind: Literal["person", "organization", "unknown"]
    shared_event_count: int


class CatalogEntityInsightsOut(BaseModel):
    """Insights derived from the admitted catalog itself, not from any external provider."""

    event_count: int
    upcoming_count: int
    past_count: int
    first_event_at: str | None
    last_event_at: str | None
    recent_event_count: int
    active_months: int
    events_per_month: float | None
    typical_attendance: int | None
    free_count: int
    paid_count: int
    top_topics: list[str]
    top_venues: list[str]
    top_cities: list[str]
    source_labels: list[str]
    collaborators: list[CatalogEntityCollaboratorOut]


class CatalogEntityExternalSourceOut(BaseModel):
    provider_key: str
    external_id: str
    source_url: str
    display_name: str
    status: Literal["linked", "fresh", "failed", "blocked"]
    fetched_at: str | None
    next_refresh_at: str
    error_code: str | None


class CatalogEntityExternalFactOut(BaseModel):
    provider_key: str
    source_url: str
    fact_key: Literal[
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
    value: str
    value_url: str | None
    sort_order: int
    observed_at: str


class CatalogEntityDetailOut(BaseModel):
    entity: CatalogEntityOut
    events: list[CatalogEntityEventOut]
    external_sources: list[CatalogEntityExternalSourceOut]
    external_facts: list[CatalogEntityExternalFactOut]
    refresh_due: bool
    insights: CatalogEntityInsightsOut | None


class CatalogEntityResolutionOut(BaseModel):
    entity_id: UUID


class CatalogEntityGraphNodeOut(BaseModel):
    """One drawn node.  ``node_id`` is a render-time namespace, never a persisted identifier."""

    node_id: str
    node_kind: Literal["entity", "event", "topic"]
    ring: int
    label: str
    degree: int
    entity_id: UUID | None
    entity_kind: Literal["person", "organization", "unknown"] | None
    identity_status: Literal["profile_verified", "source_scoped"] | None
    profile_url: str | None
    profile_key: str | None
    canonical_event_id: UUID | None
    start_at: str | None
    end_at: str | None
    is_past: bool | None
    venue_name: str | None
    city: str | None
    price_status: str | None
    topics: list[str]
    ego_roles: list[str]
    registration_url: str | None
    shared_event_count: int | None
    roles: list[str]


class CatalogEntityGraphEdgeOut(BaseModel):
    """A recorded mention.  Deliberately carries no weight: a mention is a fact, not a score."""

    a: str
    b: str
    kind: Literal["mention", "topic"]
    roles: list[str]
    source_labels: list[str]
    observed_at: str | None


class CatalogEntityGraphCountsOut(BaseModel):
    """What was drawn against what matched.

    ``edges`` counts the emitted array; ``mention_edges`` is the capped mention subset that
    ``edges_total`` and ``truncated.edges`` compare against.
    """

    events: int
    events_total: int
    peers: int
    peers_total: int
    topics: int
    edges: int
    mention_edges: int
    edges_total: int


class CatalogEntityGraphTruncationOut(BaseModel):
    events: bool
    peers: bool
    edges: bool


class CatalogEntitySameNameCandidateOut(BaseModel):
    """An exact normalized-name match, offered for review.  Never merged, never a join key."""

    entity_id: UUID
    display_name: str
    kind: Literal["person", "organization", "unknown"]
    identity_status: Literal["profile_verified", "source_scoped"]
    event_count: int


class CatalogEntityGraphOut(BaseModel):
    focus_id: str
    generated_at: str
    counts: CatalogEntityGraphCountsOut
    truncated: CatalogEntityGraphTruncationOut
    nodes: list[CatalogEntityGraphNodeOut]
    edges: list[CatalogEntityGraphEdgeOut]
    same_name_candidates: list[CatalogEntitySameNameCandidateOut]


class CatalogEntityDirectoryTotalsOut(BaseModel):
    entity_count: int
    person_count: int
    organization_count: int
    unknown_count: int
    verified_count: int
    scoped_count: int


class CatalogEntityDirectoryCoverageOut(BaseModel):
    """How much of the catalog names anyone at all, carried beside the ranking it qualifies."""

    events_with_entities: int
    events_total: int
    mention_count: int


class CatalogEntityHubOut(BaseModel):
    entity_id: UUID
    display_name: str
    kind: Literal["person", "organization", "unknown"]
    identity_status: Literal["profile_verified", "source_scoped"]
    profile_url: str | None
    profile_key: str | None
    event_count: int
    upcoming_count: int
    peer_count: int
    source_count: int
    roles: list[str]
    top_city: str | None
    last_event_at: str | None


class CatalogEntityDirectoryOut(BaseModel):
    generated_at: str
    totals: CatalogEntityDirectoryTotalsOut
    coverage: CatalogEntityDirectoryCoverageOut
    matched: int
    hubs: list[CatalogEntityHubOut]


class UiConfigOut(BaseModel):
    product_name: str = "Events Concierge"
    release_profile: Literal["full", "discovery"] = "full"
    muse_enabled: bool = False
    catalog_name_suggestions_enabled: bool = False
    local_demo: bool
    auth_mode: Literal["local_demo", "deployment_session"]
    auth_provider: Literal["custom_claim", "google", "identity_platform"] | None = None
    anonymous_browsing: bool = True
    identity_platform: dict[str, Any] | None = None
    consumer_legal_mode: Literal["required", "deferred"] = "required"
    legal_policy: dict[str, str] | None = None
    auth_start_url: str | None
    reauth_url: str | None
    logout_url: str | None
    csrf_cookie_name: str | None
    csrf_header_name: str | None


class IdentitySessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id_token: str = Field(min_length=1, max_length=16 * 1024, repr=False)
    google_id_token: str | None = Field(
        default=None, min_length=1, max_length=16 * 1024, repr=False
    )
    state: str = Field(min_length=43, max_length=43)
    accepted_terms: bool = Field(default=False, strict=True)
    terms_version: str | None = Field(default=None, min_length=1, max_length=80)
    privacy_version: str | None = Field(default=None, min_length=1, max_length=80)


class VersionOut(BaseModel):
    release_revision: str
    image_digest: str | None


_TIME_ZONE_PATTERN = re.compile(r"[A-Za-z0-9+_/-]{1,64}")


class SavedFilterOut(BaseModel):
    """One named catalog filter selection belonging to the caller."""

    saved_filter_id: str
    name: str
    filters: dict[str, Any]
    created_at: str | None = None
    updated_at: str | None = None
    last_used_at: str | None = None


class SavedFilterBody(BaseModel):
    """A name plus the whole filter selection to store under it.

    ``filters`` is the client's own filter vocabulary and is stored opaquely: it changes with the
    UI, and re-modelling it here would make every UI change a schema change. What is enforced is
    that it is a bounded object of scalars and shallow lists, so the column cannot become a
    general-purpose document store.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=_MAX_SAVED_FILTER_NAME_LENGTH)
    filters: dict[str, Any]

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        """Mirror the stored CHECK, so the database is the second gate and never the first.

        Without this a blank or control-bearing name passes pydantic and dies on the constraint,
        which reaches the caller as a 500 for what is plainly a bad request.
        """
        collapsed = " ".join(value.split()).strip()
        if not collapsed:
            raise ValueError("saved filter name must not be blank")
        for character in collapsed:
            if category(character) in {"Cc", "Cf", "Zl", "Zp"}:
                raise ValueError("saved filter name contains unsupported characters")
        return collapsed


class ProfileOut(BaseModel):
    """Mutable, user-authored display facts. Never an identity binding (FR-1.5, ADR-011)."""

    display_name: str | None = None
    time_zone: str | None = None
    avatar_url: str | None = None
    revision: int = 0
    updated_at: str | None = None


class ProfileBody(BaseModel):
    """Whole-value replace carrying a caller-minted monotonic revision, like ``PreferencesBody``.

    Every field is always sent and ``null`` clears, so there is no ambiguity between "leave
    unchanged" and "clear". Identity bindings are absent by construction, and ``extra="forbid"``
    rejects any attempt to smuggle one in (ADR-011, FR-1.3).
    """

    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1, le=9_223_372_036_854_775_807)
    display_name: str | None = Field(default=None, max_length=64)
    time_zone: str | None = Field(default=None, max_length=64)

    @field_validator("display_name")
    @classmethod
    def normalize_display_name(cls, value: str | None) -> str | None:
        """Collapse whitespace and refuse characters that can rewrite surrounding chrome."""
        if value is None:
            return None
        collapsed = " ".join(value.split()).strip()
        if collapsed == "":
            return None
        for character in collapsed:
            # Bidi overrides let a name visually reorder the copy rendered beside it.
            if category(character) in {"Cc", "Cf", "Zl", "Zp"}:
                raise ValueError("display name contains unsupported characters")
        return collapsed

    @field_validator("time_zone")
    @classmethod
    def normalize_time_zone(cls, value: str | None) -> str | None:
        """Accept only an IANA-shaped identifier; the database CHECK is the second gate."""
        if value is None or value.strip() == "":
            return None
        candidate = value.strip()
        if not _TIME_ZONE_PATTERN.fullmatch(candidate):
            raise ValueError("time zone is not a supported identifier")
        return candidate


class ApiKeyOut(BaseModel):
    """One API key as its owner may see it. Never carries the secret."""

    key_id: UUID
    name: str
    key_prefix: str
    created_at: str
    last_used_at: str | None = None
    revoked_at: str | None = None
    active: bool


class ApiKeyBody(BaseModel):
    """A label for a new key. The secret is generated server side and never accepted from input."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        """Collapse whitespace and refuse control characters."""
        collapsed = " ".join(value.split()).strip()
        if not collapsed:
            raise ValueError("name is required")
        if any(category(character) in {"Cc", "Cf"} for character in collapsed):
            raise ValueError("name contains unsupported characters")
        return collapsed


class IssuedApiKeyOut(BaseModel):
    """The one and only response that carries a key secret."""

    key: ApiKeyOut
    secret: str


class AvatarOut(BaseModel):
    """What the client needs to render and cache-bust a freshly stored avatar."""

    avatar_url: str
    width: int
    height: int
    content_type: str
    byte_size: int
    checksum: str


class MeOut(BaseModel):
    notify_email: str
    interests: list[str]
    preference_revision: int
    local_demo: bool
    # Operator affordances are hidden from ordinary accounts. This is presentation only: the
    # ingestion administration API is separately restricted to loopback callers, and a hidden link
    # is never the boundary.
    is_admin: bool = False
    # Additive and defaulted, so the legacy static shell -- which reads only notify_email and
    # interests -- keeps deserializing this response unchanged.
    relay_inbox: str | None = None
    profile: ProfileOut = Field(default_factory=ProfileOut)


class AccountErasureBody(BaseModel):
    """One replay identity plus an exact destructive-action acknowledgement."""

    model_config = ConfigDict(extra="forbid")

    request_id: UUID
    confirmation: Literal["DELETE MY ACCOUNT"]


class ReauthenticationBody(BaseModel):
    """One bounded same-origin return path; the adapter rejects every external destination."""

    model_config = ConfigDict(extra="forbid")

    return_to: str = Field(default="/app#/settings", min_length=1, max_length=2048)


class AccountErasureOut(BaseModel):
    """PII-free aggregate progress for the tenant's one durable erasure command."""

    request_id: UUID
    status: str
    workflow_targets: int
    workflows_cancelled: int
    calendar_targets: int
    calendar_deleted: int
    browser_sessions_revoked: bool
    credential_vault_purged: bool
    object_store_purged: bool
    retained_audit_rows: int
    failed_stage: str | None


class PreferencesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interests: list[str] = Field(default_factory=list, max_length=20)
    revision: int = Field(ge=1, le=9_223_372_036_854_775_807)

    @field_validator("interests")
    @classmethod
    def normalize_interests(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for raw_value in values:
            value = " ".join(raw_value.split()).strip().casefold()
            if (
                not value
                or len(value) > _MAX_INTEREST_LENGTH
                or any(ord(char) < _MIN_PRINTABLE_CODEPOINT for char in value)
            ):
                raise ValueError("interests must be bounded, printable labels")
            if value in seen:
                raise ValueError("interests must be unique")
            seen.add(value)
            normalized.append(value)
        return normalized


class PreferencesOut(BaseModel):
    status: str
    interests: list[str]
    revision: int


class RequestOutcomeOut(BaseModel):
    canonical_event_id: UUID
    title: str
    start_at: str
    state: str
    lane: str | None
    source: str | None
    updated_at: str


class RequestSummaryOut(BaseModel):
    request_id: UUID
    text: str
    state: str
    created_at: str
    categories: list[str]
    free_only: bool
    window_start: str | None
    window_end: str | None
    outcome: RequestOutcomeOut | None


class RequestPageOut(BaseModel):
    items: list[RequestSummaryOut]
    next_cursor: str | None


class RegistrationSummaryOut(BaseModel):
    canonical_event_id: UUID
    title: str
    start_at: str
    end_at: str | None
    venue_name: str | None
    city: str | None
    description: str
    price_status: str
    event_status: str
    state: str
    lane: str | None
    source: str | None
    conflict_warning: bool
    registration_url: str | None
    updated_at: str
    can_withdraw: bool


class RegistrationPageOut(BaseModel):
    items: list[RegistrationSummaryOut]
    next_cursor: str | None


class TaskSummaryOut(BaseModel):
    task_id: str
    canonical_event_id: UUID
    event_summary: str
    title: str
    start_at: str
    venue_name: str | None
    city: str | None
    reason: str
    state: str
    deep_link: str
    expires_at: str
    created_at: str


class TaskPageOut(BaseModel):
    items: list[TaskSummaryOut]
    next_cursor: str | None


class FeedFeedbackBody(BaseModel):
    """Self-reported implicit signal with a caller-minted replay key (FR-2.1, FR-4.3, NFR-8)."""

    model_config = ConfigDict(extra="forbid")

    signal_id: UUID
    canonical_event_id: UUID
    kind: FeedbackSignalKind


class FeedFeedbackAccepted(BaseModel):
    """Converged result for one at-least-once feedback delivery."""

    status: str


class RequestAccepted(BaseModel):
    request_id: UUID
    workflow_started: bool
    feed: FeedOut


class UnrsvpBody(BaseModel):
    """Inbound user withdrawal command; the generated id makes reply/API redelivery observable."""

    model_config = ConfigDict(extra="forbid")

    canonical_event_id: UUID
    request_id: UUID = Field(default_factory=uuid4)


class UnrsvpAccepted(BaseModel):
    status: str
    request_id: UUID


class HandoffCompletionAccepted(BaseModel):
    status: str


async def _resolve_handoff_completion_target(
    container: Container,
    token: str,
) -> HandoffCompletionTarget:
    """Resolve one fixed-shape bearer capability without reflecting it in an error."""
    if re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
        raise HTTPException(status_code=404, detail="handoff task not found")
    target = await container.handoff_repo.resolve_completion_token(token)
    if target is None:
        raise HTTPException(status_code=404, detail="handoff task not found")
    return target


def _completion_page(message: str, *, show_form: bool) -> HTMLResponse:
    """Render an inert, scanner-safe confirmation page with no capability referrer."""
    safe_message = escape(message)
    eyebrow = "One last check" if show_form else "All set"
    description = (
        "Confirm only after the event site says your registration is complete. "
        "We'll verify it independently before updating your calendar."
        if show_form
        else "That confirmation is already recorded. You can safely close this page."
    )
    form = (
        (
            '<form method="post" class="onboarding-form" aria-describedby="completion-note">'
            '<button class="button button-primary button-wide" type="submit">'
            '<span>Mark registration done</span><span aria-hidden="true">&#8594;</span>'
            "</button>"
            '<p id="completion-note" class="form-note">'
            "This deliberate POST is the only action that submits your confirmation."
            "</p></form>"
        )
        if show_form
        else (
            '<div class="onboarding-form">'
            '<a class="button button-secondary button-wide" href="/app">'
            '<span>Return to Events Concierge</span><span aria-hidden="true">&#8594;</span>'
            "</a></div>"
        )
    )
    return HTMLResponse(
        content=(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<meta name="theme-color" content="#173f35">'
            '<meta name="referrer" content="no-referrer">'
            '<link rel="icon" href="/favicon.svg" type="image/svg+xml">'
            '<link rel="stylesheet" href="/assets/app.css">'
            f"<title>{safe_message} · Events Concierge</title></head><body>"
            '<main class="welcome" id="main-content">'
            '<section class="welcome-art" aria-hidden="true">'
            '<div class="welcome-orbit welcome-orbit-one"></div>'
            '<div class="welcome-orbit welcome-orbit-two"></div>'
            '<p class="welcome-art-copy">One tap.<br>A truthful calendar.</p>'
            '<div class="mini-invite mini-invite-one"><span>SECURE HANDOFF</span>'
            "<strong>Registration checked</strong><small>No guesswork, no duplicate action</small>"
            "</div></section>"
            '<section class="welcome-panel">'
            '<header class="welcome-brand"><img src="/assets/mark.svg" alt="" width="42" height="42">'
            "<span>Events Concierge</span></header>"
            '<div class="welcome-copy">'
            f'<p class="eyebrow">{eyebrow}</p><h1>{safe_message}</h1>'
            f'<p class="welcome-description">{description}</p>'
            f"</div>{form}</section></main></body></html>"
        ),
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Content-Security-Policy": (
                "default-src 'none'; style-src 'self'; img-src 'self'; form-action 'self'; "
                "base-uri 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
        },
    )


def _feed_out(feed: Feed, constraints: RequestConstraints | None = None) -> FeedOut:
    constraints = constraints or RequestConstraints()
    return FeedOut(
        request_id=feed.request_id,
        signals=feed.signals,
        next_cursor=feed.next_cursor,
        understood=FeedUnderstandingOut(
            categories=list(constraints.categories),
            free_only=constraints.budget_free,
            window_start=(
                constraints.time_window.start.isoformat() if constraints.time_window else None
            ),
            window_end=(
                constraints.time_window.end.isoformat() if constraints.time_window else None
            ),
            radius_km=constraints.geo.radius_km if constraints.geo else None,
        ),
        items=[
            FeedItemOut(
                canonical_event_id=item.canonical_event.canonical_event_id,
                title=item.canonical_event.title,
                start_at=item.canonical_event.start_at.isoformat(),
                price_status=item.canonical_event.price_status.value,
                price_min_cents=item.canonical_event.price_min_cents,
                price_max_cents=item.canonical_event.price_max_cents,
                price_currency=item.canonical_event.price_currency,
                score=item.score,
                rationale=item.rationale,
                additional_dates=[
                    EventOccurrenceOut(
                        canonical_event_id=e.canonical_event_id,
                        start_at=e.start_at.isoformat(),
                        end_at=e.end_at.isoformat() if e.end_at else None,
                        registration_urls=e.registration_urls(),
                    )
                    for e in item.additional_dates
                ],
                discovery_state=item.discovery_state,
                source_freshness=item.source_freshness,
                conflict=item.conflict_verdict.value,
                lanes=[lane.value for lane in item.lane_plan],
                registration_urls=item.canonical_event.registration_urls(),
                end_at=(
                    item.canonical_event.end_at.isoformat()
                    if item.canonical_event.end_at is not None
                    else None
                ),
                venue_name=item.canonical_event.venue_name,
                city=item.canonical_event.city_norm,
                description=item.canonical_event.description[:800],
                event_status=item.canonical_event.event_status.value,
                latitude=(item.canonical_event.geo.lat if item.canonical_event.geo else None),
                longitude=(item.canonical_event.geo.lon if item.canonical_event.geo else None),
                registerable=item.registerable,
                sources=[
                    FeedSourceOut(
                        source=source_link.source.value,
                        registration_url=source_link.registration_url,
                    )
                    for source_link in item.canonical_event.source_links
                ],
                organizer_name=item.canonical_event.organizer_name,
                host_names=list(item.canonical_event.host_names),
                speaker_names=list(item.canonical_event.speaker_names),
                partner_names=list(item.canonical_event.partner_names),
                entity_profiles=[
                    EventEntityProfileOut(**profile.as_payload())
                    for profile in item.canonical_event.entity_profiles
                ],
                attendance_count=item.canonical_event.attendance_count,
                registration_status=item.canonical_event.registration_status.value,
                topics=list(item.canonical_event.topics),
                extraction_evidence=[
                    EventExtractionEvidenceOut(**evidence.as_payload())
                    for evidence in item.canonical_event.extraction_evidence
                ],
            )
            for item in feed.items
        ],
    )


def _encode_catalog_cursor(
    start_at: datetime,
    canonical_event_id: UUID,
    filter_scope: str,
) -> str:
    payload = [
        _CATALOG_CURSOR_VERSION,
        filter_scope,
        start_at.astimezone(UTC).isoformat(),
        str(canonical_event_id),
    ]
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    return encoded.rstrip("=")


def _catalog_cursor_out(item: CatalogBrowseEvent, filter_scope: str) -> str:
    event = item.canonical_event
    return _encode_catalog_cursor(event.start_at, event.canonical_event_id, filter_scope)


def _catalog_cursor_in(cursor: str | None, filter_scope: str) -> CatalogBrowseCursor | None:
    if cursor is None:
        return None
    try:
        if (
            len(cursor) > _MAX_CATALOG_CURSOR_LENGTH
            or re.fullmatch(_CATALOG_CURSOR_PATTERN, cursor) is None
        ):
            raise ValueError
        padded = cursor + ("=" * (-len(cursor) % 4))
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
        if (
            not isinstance(payload, list)
            or len(payload) != _CATALOG_CURSOR_PARTS
            or payload[0] != _CATALOG_CURSOR_VERSION
            or payload[1] != filter_scope
            or not isinstance(payload[2], str)
            or not isinstance(payload[3], str)
        ):
            raise ValueError
        start_at = datetime.fromisoformat(payload[2])
        if start_at.tzinfo is None or start_at.utcoffset() is None:
            raise ValueError
        canonical_event_id = UUID(payload[3])
        decoded = CatalogBrowseCursor(
            start_at=start_at.astimezone(UTC),
            canonical_event_id=canonical_event_id,
        )
        if (
            _encode_catalog_cursor(
                decoded.start_at,
                decoded.canonical_event_id,
                filter_scope,
            )
            != cursor
        ):
            raise ValueError
        return decoded
    except (UnicodeDecodeError, ValueError, binascii.Error):
        raise HTTPException(status_code=422, detail="invalid catalog cursor") from None


def _catalog_filter_scope(
    *,
    source_keys: tuple[str, ...],
    starts_after: datetime | None,
    starts_before: datetime | None,
    date_ranges: tuple[tuple[datetime, datetime], ...] = (),
    query: str | None,
    cities: tuple[str, ...],
    location_scopes: tuple[str, ...],
    price: str | None,
    price_max_cents: int | None,
    price_min_cents: int | None,
    topics: tuple[str, ...],
    sort: Literal["soonest", "latest"] = "soonest",
    availability: str | None = None,
) -> str:
    payload = [
        source_keys,
        starts_after.astimezone(UTC).isoformat() if starts_after is not None else None,
        starts_before.astimezone(UTC).isoformat() if starts_before is not None else None,
        tuple(
            (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat())
            for start, end in date_ranges
        ),
        query,
        cities,
        location_scopes,
        price,
        price_max_cents,
        price_min_cents,
        topics,
        sort,
        availability,
    ]
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def _catalog_date_range_endpoint(value: str, *, end: bool) -> datetime:
    """Parse a date-only or timezone-aware ISO endpoint into an absolute UTC boundary.

    Date-only ranges are inclusive at the product boundary, so the end date becomes the next UTC
    midnight. Datetime ranges retain the legacy endpoint semantics: start inclusive, end exclusive.
    """
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        parsed_date = date.fromisoformat(value)
        if end:
            parsed_date += timedelta(days=1)
        return datetime.combine(parsed_date, time.min, tzinfo=UTC)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("catalog date range datetimes require a timezone")
    return parsed.astimezone(UTC)


def _merge_catalog_date_ranges(
    ranges: list[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for raw_start, raw_end in sorted(ranges):
        if raw_start.tzinfo is None or raw_start.utcoffset() is None:
            raise ValueError("catalog dates require a timezone")
        if raw_end.tzinfo is None or raw_end.utcoffset() is None:
            raise ValueError("catalog dates require a timezone")
        utc_start = raw_start.astimezone(UTC)
        utc_end = raw_end.astimezone(UTC)
        if utc_end <= utc_start:
            raise ValueError("catalog date range is invalid")
        if merged and utc_start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], utc_end))
        else:
            merged.append((utc_start, utc_end))
    return merged


class _CatalogFilterSelection(NamedTuple):
    """The scalar catalog filters after shared validation and stable normalization."""

    query: str | None
    cities: tuple[str, ...]
    location_scopes: tuple[str, ...]
    topics: tuple[str, ...]


def _normalized_catalog_filters(
    *,
    q: str | None,
    city: Sequence[str] | None,
    location_scope: Sequence[str] | None,
    topic: Sequence[str] | None,
    price: str | None,
    price_max_cents: int | None,
    price_min_cents: int | None = None,
) -> _CatalogFilterSelection:
    """Validate the filters shared by the catalog page and its day summary.

    Both routes must reject exactly the same inputs, or a calendar grid could summarize a filter
    the paged agenda refuses to serve.
    """
    query = q.strip() if q and q.strip() else None
    cities = tuple(
        sorted(
            {value.strip() for value in (city or []) if value.strip()},
            key=str.casefold,
        )
    )
    location_scopes = tuple(sorted(set(location_scope or [])))
    topics = tuple(sorted(set(topic or [])))
    if len(cities) > _MAX_CATALOG_LOCATION_SELECTIONS:
        raise HTTPException(status_code=422, detail="too many catalog locations")
    if any(len(value) > _MAX_CATALOG_FILTER_LENGTH for value in cities):
        raise HTTPException(status_code=422, detail="catalog location is too long")
    if len(topics) > MAX_CATALOG_TOPIC_SELECTIONS or any(
        value not in CATALOG_TOPICS for value in topics
    ):
        raise HTTPException(status_code=422, detail="catalog topic filter is invalid")
    if price_max_cents is not None and price == "unknown":
        raise HTTPException(
            status_code=422,
            detail="maximum price cannot be combined with an unlisted-price filter",
        )
    # A floor describes a paid amount, so free and unlisted cannot carry one, and an inverted
    # band is a request that can never match rather than one that matches nothing today.
    if price_min_cents is not None and price in {"free", "unknown"}:
        raise HTTPException(
            status_code=422,
            detail="minimum price cannot be combined with a free or unlisted-price filter",
        )
    if (
        price_min_cents is not None
        and price_max_cents is not None
        and price_min_cents > price_max_cents
    ):
        raise HTTPException(
            status_code=422,
            detail="minimum price cannot exceed the maximum price",
        )
    if any(
        ord(character) < _MIN_PRINTABLE_CODEPOINT or ord(character) == _DELETE_CODEPOINT
        for value in (query, *cities)
        if value is not None
        for character in value
    ):
        raise HTTPException(status_code=422, detail="catalog filter is invalid")
    return _CatalogFilterSelection(
        query=query,
        cities=cities,
        location_scopes=location_scopes,
        topics=topics,
    )


def _normalize_catalog_date_ranges(
    values: list[str],
    *,
    starts_after: datetime | None,
    starts_before: datetime | None,
    source_keys: tuple[str, ...],
) -> tuple[tuple[datetime, datetime], ...]:
    """Validate, sort, deduplicate, and merge overlapping or adjacent catalog windows."""
    legacy_range_count = int(starts_after is not None and starts_before is not None)
    if len(values) + legacy_range_count > _MAX_CATALOG_DATE_RANGES:
        raise ValueError("too many catalog date ranges")
    parsed: list[tuple[datetime, datetime]] = []
    if starts_after is not None and starts_before is not None:
        parsed.append((starts_after, starts_before))
    for value in values:
        if len(value) > _MAX_CATALOG_DATE_RANGE_LENGTH or value.count("..") != 1:
            raise ValueError("catalog date range is invalid")
        start_value, end_value = value.split("..")
        if not start_value or not end_value:
            raise ValueError("catalog date range is invalid")
        parsed.append(
            (
                _catalog_date_range_endpoint(start_value, end=False),
                _catalog_date_range_endpoint(end_value, end=True),
            )
        )
    if not parsed:
        return ()
    max_window = _MAX_SOURCE_CATALOG_ARCHIVE_WINDOW if source_keys else _MAX_CATALOG_BROWSE_WINDOW
    normalized = _merge_catalog_date_ranges(parsed)
    if sum((end - start for start, end in normalized), timedelta()) > max_window:
        raise ValueError("catalog date range is invalid")
    if any(end - start > max_window for start, end in normalized):
        raise ValueError("catalog date range is invalid")
    return tuple(normalized)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _catalog_browse_item_out(item: CatalogBrowseEvent) -> CatalogBrowseItemOut:
    event = item.canonical_event
    sources = [
        CatalogBrowseSourceOut(
            source=source.source.value,
            registration_url=source.registration_url,
            source_key=source.source_key,
            label=source.label,
            publisher=source.publisher,
            provider=source.provider,
            seed_url=source.seed_url,
            source_event_id=source.source_event_id,
            last_seen_at=source.last_seen_at.isoformat(),
            refresh_run_key=source.refresh_run_key,
        )
        for source in item.sources
    ]
    now = datetime.now(UTC)
    source_checked = max((source.last_seen_at for source in item.sources), default=None)
    source_freshness = (
        "unknown"
        if source_checked is None or source_checked > now
        else "stale"
        if now - source_checked > timedelta(days=7)
        else "recent"
    )
    return CatalogBrowseItemOut(
        discovery_state=lifecycle(event, now),
        source_freshness=source_freshness,
        canonical_event_id=event.canonical_event_id,
        title=event.title,
        start_at=event.start_at.isoformat(),
        price_status=event.price_status.value,
        price_min_cents=event.price_min_cents,
        price_max_cents=event.price_max_cents,
        price_currency=event.price_currency,
        score=None,
        rationale="Retained catalog observation."
        if lifecycle(event, now) == "past"
        else "Current catalog observation; chronological browse is not personalized.",
        conflict="not_evaluated",
        lanes=["handoff"],
        registration_urls=_unique([source.registration_url for source in item.sources]),
        end_at=event.end_at.isoformat() if event.end_at is not None else None,
        venue_name=event.venue_name,
        city=event.city_norm,
        description=event.description[:800],
        event_status=event.event_status.value,
        latitude=event.geo.lat if event.geo is not None else None,
        longitude=event.geo.lon if event.geo is not None else None,
        registerable=None,
        source_keys=_unique([source.source_key for source in item.sources]),
        providers=_unique([source.provider for source in item.sources]),
        calendar_labels=_unique([source.label for source in item.sources]),
        sources=sources,
        organizer_name=event.organizer_name,
        host_names=list(event.host_names),
        speaker_names=list(event.speaker_names),
        partner_names=list(event.partner_names),
        entity_profiles=[
            EventEntityProfileOut(**profile.as_payload()) for profile in event.entity_profiles
        ],
        attendance_count=event.attendance_count,
        registration_status=event.registration_status.value,
        topics=list(event.topics),
        extraction_evidence=[
            EventExtractionEvidenceOut(**evidence.as_payload())
            for evidence in event.extraction_evidence
        ],
    )


def _catalog_entity_out(entity: Any) -> CatalogEntityOut:
    return CatalogEntityOut(
        entity_id=entity.entity_id,
        display_name=entity.display_name,
        kind=entity.kind,
        identity_status=entity.identity_status,
        canonical_profile_url=entity.canonical_profile_url,
        summary=entity.summary,
        website_url=entity.website_url,
        logo_url=entity.logo_url,
        city=entity.city,
        country=entity.country,
        event_count=entity.event_count,
        roles=list(entity.roles),
        source_count=entity.source_count,
        research_status=entity.research_status,
    )


def _catalog_entity_detail_out(detail: Any) -> CatalogEntityDetailOut:
    return CatalogEntityDetailOut(
        entity=_catalog_entity_out(detail.entity),
        events=[
            CatalogEntityEventOut(
                canonical_event_id=event.canonical_event_id,
                title=event.title,
                start_at=event.start_at.isoformat(),
                end_at=event.end_at.isoformat() if event.end_at is not None else None,
                venue_name=event.venue_name,
                city=event.city,
                description=event.description,
                roles=list(event.roles),
                source_labels=list(event.source_labels),
                registration_url=event.registration_url,
                is_past=event.is_past,
            )
            for event in detail.events
        ],
        external_sources=[
            CatalogEntityExternalSourceOut(
                provider_key=source.provider_key,
                external_id=source.external_id,
                source_url=source.source_url,
                display_name=source.display_name,
                status=source.status,
                fetched_at=(
                    source.fetched_at.isoformat() if source.fetched_at is not None else None
                ),
                next_refresh_at=source.next_refresh_at.isoformat(),
                error_code=source.error_code,
            )
            for source in detail.external_sources
        ],
        external_facts=[
            CatalogEntityExternalFactOut(
                provider_key=fact.provider_key,
                source_url=fact.source_url,
                fact_key=fact.fact_key,
                value=fact.value,
                value_url=fact.value_url,
                sort_order=fact.sort_order,
                observed_at=fact.observed_at.isoformat(),
            )
            for fact in detail.external_facts
        ],
        refresh_due=detail.refresh_due,
        insights=(
            _catalog_entity_insights_out(detail.insights) if detail.insights is not None else None
        ),
    )


def _catalog_entity_graph_out(graph: Any) -> CatalogEntityGraphOut:
    return CatalogEntityGraphOut(
        focus_id=graph.focus_id,
        generated_at=graph.generated_at.isoformat(),
        counts=CatalogEntityGraphCountsOut(
            events=graph.counts.events,
            events_total=graph.counts.events_total,
            peers=graph.counts.peers,
            peers_total=graph.counts.peers_total,
            topics=graph.counts.topics,
            edges=graph.counts.edges,
            mention_edges=graph.counts.mention_edges,
            edges_total=graph.counts.edges_total,
        ),
        truncated=CatalogEntityGraphTruncationOut(
            events=graph.truncated.events,
            peers=graph.truncated.peers,
            edges=graph.truncated.edges,
        ),
        nodes=[
            CatalogEntityGraphNodeOut(
                node_id=node.node_id,
                node_kind=node.node_kind,
                ring=node.ring,
                label=node.label,
                degree=node.degree,
                entity_id=node.entity_id,
                entity_kind=node.entity_kind,
                identity_status=node.identity_status,
                profile_url=node.profile_url,
                profile_key=node.profile_key,
                canonical_event_id=node.canonical_event_id,
                start_at=node.start_at.isoformat() if node.start_at is not None else None,
                end_at=node.end_at.isoformat() if node.end_at is not None else None,
                is_past=node.is_past,
                venue_name=node.venue_name,
                city=node.city,
                price_status=node.price_status,
                topics=list(node.topics),
                ego_roles=list(node.ego_roles),
                registration_url=node.registration_url,
                shared_event_count=node.shared_event_count,
                roles=list(node.roles),
            )
            for node in graph.nodes
        ],
        edges=[
            CatalogEntityGraphEdgeOut(
                a=edge.a,
                b=edge.b,
                kind=edge.kind,
                roles=list(edge.roles),
                source_labels=list(edge.source_labels),
                observed_at=(
                    edge.observed_at.isoformat() if edge.observed_at is not None else None
                ),
            )
            for edge in graph.edges
        ],
        same_name_candidates=[
            CatalogEntitySameNameCandidateOut(
                entity_id=candidate.entity_id,
                display_name=candidate.display_name,
                kind=candidate.kind,
                identity_status=candidate.identity_status,
                event_count=candidate.event_count,
            )
            for candidate in graph.same_name_candidates
        ],
    )


def _catalog_entity_directory_out(directory: Any) -> CatalogEntityDirectoryOut:
    return CatalogEntityDirectoryOut(
        generated_at=directory.generated_at.isoformat(),
        totals=CatalogEntityDirectoryTotalsOut(
            entity_count=directory.totals.entity_count,
            person_count=directory.totals.person_count,
            organization_count=directory.totals.organization_count,
            unknown_count=directory.totals.unknown_count,
            verified_count=directory.totals.verified_count,
            scoped_count=directory.totals.scoped_count,
        ),
        coverage=CatalogEntityDirectoryCoverageOut(
            events_with_entities=directory.coverage.events_with_entities,
            events_total=directory.coverage.events_total,
            mention_count=directory.coverage.mention_count,
        ),
        matched=directory.matched,
        hubs=[
            CatalogEntityHubOut(
                entity_id=hub.entity_id,
                display_name=hub.display_name,
                kind=hub.kind,
                identity_status=hub.identity_status,
                profile_url=hub.profile_url,
                profile_key=hub.profile_key,
                event_count=hub.event_count,
                upcoming_count=hub.upcoming_count,
                peer_count=hub.peer_count,
                source_count=hub.source_count,
                roles=list(hub.roles),
                top_city=hub.top_city,
                last_event_at=(
                    hub.last_event_at.isoformat() if hub.last_event_at is not None else None
                ),
            )
            for hub in directory.hubs
        ],
    )


def _catalog_entity_insights_out(insights: Any) -> CatalogEntityInsightsOut:
    return CatalogEntityInsightsOut(
        event_count=insights.event_count,
        upcoming_count=insights.upcoming_count,
        past_count=insights.past_count,
        first_event_at=(
            insights.first_event_at.isoformat() if insights.first_event_at is not None else None
        ),
        last_event_at=(
            insights.last_event_at.isoformat() if insights.last_event_at is not None else None
        ),
        recent_event_count=insights.recent_event_count,
        active_months=insights.active_months,
        events_per_month=insights.events_per_month,
        typical_attendance=insights.typical_attendance,
        free_count=insights.free_count,
        paid_count=insights.paid_count,
        top_topics=list(insights.top_topics),
        top_venues=list(insights.top_venues),
        top_cities=list(insights.top_cities),
        source_labels=list(insights.source_labels),
        collaborators=[
            CatalogEntityCollaboratorOut(
                entity_id=peer.entity_id,
                display_name=peer.display_name,
                kind=peer.kind,
                shared_event_count=peer.shared_event_count,
            )
            for peer in insights.collaborators
        ],
    )


def _request_summary_out(item: ConsumerRequestSummary) -> RequestSummaryOut:
    outcome = item.outcome
    return RequestSummaryOut(
        request_id=item.request_id,
        text=item.text,
        state=item.state,
        created_at=item.created_at.isoformat(),
        categories=list(item.categories),
        free_only=item.budget_free,
        window_start=item.window_start.isoformat() if item.window_start else None,
        window_end=item.window_end.isoformat() if item.window_end else None,
        outcome=(
            RequestOutcomeOut(
                canonical_event_id=outcome.canonical_event_id,
                title=outcome.title,
                start_at=outcome.start_at.isoformat(),
                state=outcome.state.value,
                lane=outcome.lane.value if outcome.lane else None,
                source=outcome.source.value if outcome.source else None,
                updated_at=outcome.updated_at.isoformat(),
            )
            if outcome is not None
            else None
        ),
    )


def _registration_summary_out(
    item: ConsumerRegistrationSummary,
) -> RegistrationSummaryOut:
    return RegistrationSummaryOut(
        canonical_event_id=item.canonical_event_id,
        title=item.title,
        start_at=item.start_at.isoformat(),
        end_at=item.end_at.isoformat() if item.end_at else None,
        venue_name=item.venue_name,
        city=item.city,
        description=item.description,
        price_status=item.price_status.value,
        event_status=item.event_status.value,
        state=item.state.value,
        lane=item.lane.value if item.lane else None,
        source=item.source.value if item.source else None,
        conflict_warning=item.conflict_warning,
        registration_url=item.registration_url,
        updated_at=item.updated_at.isoformat(),
        can_withdraw=item.can_withdraw,
    )


def _task_summary_out(item: ConsumerTaskSummary) -> TaskSummaryOut:
    return TaskSummaryOut(
        task_id=item.task_id,
        canonical_event_id=item.canonical_event_id,
        event_summary=item.event_summary,
        title=item.title,
        start_at=item.start_at.isoformat(),
        venue_name=item.venue_name,
        city=item.city,
        reason=item.reason.value,
        state=item.state.value,
        deep_link=item.deep_link,
        expires_at=item.expires_at.isoformat(),
        created_at=item.created_at.isoformat(),
    )


def _page_offset(cursor: str | None) -> int:
    if cursor is None:
        return 0
    if (
        re.fullmatch(_FEED_CURSOR_PATTERN, cursor) is None
        or not cursor.isascii()
        or int(cursor) > _MAX_FEED_CURSOR
    ):
        raise HTTPException(status_code=422, detail="invalid page cursor")
    return int(cursor)


def _next_page_cursor(offset: int, limit: int, returned: int) -> str | None:
    next_offset = offset + limit
    if returned <= limit or next_offset > _MAX_FEED_CURSOR:
        return None
    return str(next_offset)


async def _parse(container: Container, tenant_id: UUID, text: str) -> tuple[EventRequest, UUID]:
    """Parse against the existing catalog; never fetch public websites on this path (NFR-8)."""
    request_id = uuid4()
    request = await container.parser.parse(tenant_id, request_id, text)
    return request, request_id


async def _session_tenant(request: Request) -> UUID:
    """Resolve one edge-authenticated tenant; caller JSON can never select an RLS context.

    The local header adapter is deliberately an injected test seam only.  Production composition
    refuses that adapter and requires one verified browser-session authority (FR-1.1/1.3).
    """
    container: Container = request.app.state.container
    try:
        tenant_id = await container.auth_context.resolve_tenant_id(request.headers)
    except BrowserSessionUnavailableError as error:
        raise HTTPException(status_code=503, detail="browser identity is unavailable") from error
    except AuthenticationFailedError as error:
        raise HTTPException(status_code=401, detail="authentication required") from error
    erasure_repo = getattr(container, "account_erasure_repo", None)
    erasure = await erasure_repo.get(tenant_id) if erasure_repo is not None else None
    if erasure is not None:
        if erasure.status is AccountErasureStatus.COMPLETED:
            raise HTTPException(status_code=410, detail="account erased")
        raise HTTPException(status_code=423, detail="account erasure in progress")
    # A signed claim establishes who the caller says they are; provisioning establishes that this
    # product account exists.  Check both before any tenant-scoped route can create orphan state.
    if await container.tenant_repo.get(tenant_id) is None:
        raise HTTPException(status_code=404, detail="account not found")
    return tenant_id


async def _authenticated_tenant(
    request: Request, tenant_id: Annotated[UUID, Depends(_session_tenant)]
) -> UUID:
    container: Container = request.app.state.container
    if (
        request.app.state.settings.identity_platform_enabled
        and (policy := request.app.state.settings.consumer_legal_policy) is not None
        and not await container.consumer_accounts.has_accepted(tenant_id, policy)
    ):
        raise HTTPException(status_code=428, detail="current terms and privacy acceptance required")
    return tenant_id


type AuthenticatedTenant = Annotated[UUID, Depends(_authenticated_tenant)]


async def _csrf_protected_tenant(
    request: Request,
    tenant_id: AuthenticatedTenant,
) -> UUID:
    """Require deployment-bound CSRF authority for an authenticated consumer mutation.

    Authentication and provisioning run first, so the verifier can bind its evidence to the same
    canonical tenant. Local header auth injects an explicit no-op because it has no ambient cookie;
    non-mock composition cannot start without a deployment-provided verifier.
    """
    container: Container = request.app.state.container
    try:
        await container.csrf_protection.verify_state_change(tenant_id, request.headers)
    except BrowserSessionUnavailableError as error:
        raise HTTPException(status_code=503, detail="browser identity is unavailable") from error
    except CsrfVerificationFailedError as error:
        raise HTTPException(status_code=403, detail="state-change verification required") from error
    return tenant_id


type CsrfProtectedTenant = Annotated[UUID, Depends(_csrf_protected_tenant)]


async def _csrf_session_tenant(
    request: Request, tenant_id: Annotated[UUID, Depends(_session_tenant)]
) -> UUID:
    # Logout can revoke a session even when newly activated legal documents are unaccepted.
    return await _csrf_protected_tenant(request, tenant_id)


async def _account_erasure_protected_tenant(request: Request) -> UUID:
    """Authenticate an existing account or its tombstone, then verify the same CSRF authority.

    Ordinary authenticated routes reject a fenced tenant. This command alone may read a pending
    tombstone while its BFF session remains live. Tenant-wide session revocation is itself a
    durable late erasure stage, after which status requires a future opaque receipt capability.
    """
    container: Container = request.app.state.container
    try:
        tenant_id = await container.auth_context.resolve_tenant_id(request.headers)
    except BrowserSessionUnavailableError as error:
        raise HTTPException(status_code=503, detail="browser identity is unavailable") from error
    except AuthenticationFailedError as error:
        raise HTTPException(status_code=401, detail="authentication required") from error
    if (
        await container.tenant_repo.get(tenant_id) is None
        and await container.account_erasure_repo.get(tenant_id) is None
    ):
        raise HTTPException(status_code=404, detail="account not found")
    try:
        await container.csrf_protection.verify_state_change(tenant_id, request.headers)
    except BrowserSessionUnavailableError as error:
        raise HTTPException(status_code=503, detail="browser identity is unavailable") from error
    except CsrfVerificationFailedError as error:
        raise HTTPException(status_code=403, detail="state-change verification required") from error
    settings: Settings = request.app.state.settings
    if not settings.mock_cloud:
        browser_session = container.browser_session
        if browser_session is None:
            raise HTTPException(status_code=503, detail="browser identity is unavailable")
        try:
            await browser_session.verify_recent_auth(
                tenant_id,
                request.headers,
                max_age_seconds=settings.account_erasure_recent_auth_seconds,
            )
        except BrowserSessionUnavailableError as error:
            raise HTTPException(
                status_code=503, detail="browser identity is unavailable"
            ) from error
        except RecentAuthenticationRequiredError as error:
            raise HTTPException(
                status_code=428,
                detail="recent sign-in required before account erasure",
            ) from error
        except BrowserStepUpUnavailableError as error:
            raise HTTPException(
                status_code=503,
                detail="account deletion is unavailable until a supported step-up method is configured",
            ) from error
    return tenant_id


type AccountErasureProtectedTenant = Annotated[
    UUID,
    Depends(_account_erasure_protected_tenant),
]


def _validated_saved_filter_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Accept a filter selection without accepting an arbitrary document.

    The server does not model the client's filter vocabulary, so it constrains the SHAPE instead:
    a bounded number of keys whose values are scalars or shallow lists of scalars. That is enough
    to store any filter set the UI can express and not enough to smuggle a nested payload in.
    """
    if len(payload) > _MAX_SAVED_FILTER_KEYS:
        raise HTTPException(status_code=422, detail="saved filter has too many fields")
    for key, value in payload.items():
        if not isinstance(key, str) or len(key) > _MAX_SAVED_FILTER_FIELD_NAME_LENGTH:
            raise HTTPException(status_code=422, detail="saved filter field name is invalid")
        if isinstance(value, list):
            if len(value) > _MAX_SAVED_FILTER_LIST_LENGTH:
                raise HTTPException(status_code=422, detail="saved filter list is too long")
            entries = value
        else:
            entries = [value]
        for entry in entries:
            if isinstance(entry, dict):
                # One level of nesting is allowed for date ranges, which are {id,start,end}.
                if len(entry) > _MAX_SAVED_FILTER_ENTRY_KEYS or any(
                    not isinstance(inner, str | int | float | bool | None)
                    for inner in entry.values()
                ):
                    raise HTTPException(status_code=422, detail="saved filter value is invalid")
                continue
            if not isinstance(entry, str | int | float | bool | None):
                raise HTTPException(status_code=422, detail="saved filter value is invalid")
            if isinstance(entry, str) and len(entry) > _MAX_CATALOG_FILTER_LENGTH:
                raise HTTPException(status_code=422, detail="saved filter value is too long")
    # A shape-legal payload can still be far larger than the column accepts; refusing it here
    # keeps the stored bound from surfacing as a 500.
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > _MAX_SAVED_FILTER_PAYLOAD_BYTES:
        raise HTTPException(status_code=422, detail="saved filter is too large")
    return payload


def _saved_filter_out(saved: SavedCatalogFilter) -> SavedFilterOut:
    return SavedFilterOut(
        saved_filter_id=str(saved.saved_filter_id),
        name=saved.name,
        filters=saved.payload,
        created_at=saved.created_at.isoformat() if saved.created_at is not None else None,
        updated_at=saved.updated_at.isoformat() if saved.updated_at is not None else None,
        last_used_at=saved.last_used_at.isoformat() if saved.last_used_at is not None else None,
    )


_API_KEY_SECRET_BYTES = 32
_API_KEY_PREFIX_CHARS = 8


def _mint_api_key() -> tuple[str, str, str]:
    """Generate one key, returning ``(secret, displayable_prefix, digest)``.

    The secret carries 256 bits from the system CSPRNG. Against that entropy a plain SHA-256 digest
    is sufficient: password hashing exists to slow attacks on guessable inputs, and a slow KDF on
    the authentication path would buy nothing while adding a denial-of-service lever.

    The prefix is a non-secret label so a person can tell two keys apart in the UI. It is taken from
    the token's own leading characters, which are already displayed to whoever holds the key.
    """
    body = secrets.token_hex(_API_KEY_SECRET_BYTES)
    prefix = f"ec_{body[:_API_KEY_PREFIX_CHARS]}"
    secret = f"{prefix}_{body[_API_KEY_PREFIX_CHARS:]}"
    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return secret, prefix, digest


def _api_key_out(record: ApiKeyRecord) -> ApiKeyOut:
    """Project a key record onto the wire. There is no field here that could carry the secret."""
    return ApiKeyOut(
        key_id=record.key_id,
        name=record.name,
        key_prefix=record.key_prefix,
        created_at=record.created_at.isoformat(),
        last_used_at=(record.last_used_at.isoformat() if record.last_used_at is not None else None),
        revoked_at=record.revoked_at.isoformat() if record.revoked_at is not None else None,
        active=record.active,
    )


def _avatar_url(avatar: ProfileAvatar | None) -> str | None:
    """Build the self-addressed avatar URL, or ``None`` when the tenant has not set one.

    The digest rides along as a query parameter purely to bust a stale cache entry when the image
    changes; the route itself resolves the tenant from the session and ignores it.
    """
    if avatar is None:
        return None
    return f"/v1/me/avatar?v={avatar.checksum_sha256[:16]}"


def _is_account_erasure_write_fence(error: DBAPIError) -> bool:
    """Recognize only the trigger raised by ``fn_fence_account_erasure_write``."""
    original = error.orig
    sqlstate = getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    diagnostic = getattr(original, "diag", None)
    primary_message = getattr(diagnostic, "message_primary", None)
    if primary_message is None:
        primary_message = str(original).partition("\n")[0]
    return (
        sqlstate == _ACCOUNT_ERASURE_WRITE_FENCE_SQLSTATE
        and primary_message == _ACCOUNT_ERASURE_WRITE_FENCE_MESSAGE
    )


def _profile_out(profile: TenantProfile, avatar: ProfileAvatar | None = None) -> ProfileOut:
    """Project stored display facts onto the wire shape."""
    return ProfileOut(
        display_name=profile.display_name,
        time_zone=profile.time_zone,
        avatar_url=_avatar_url(avatar),
        revision=profile.revision,
        updated_at=profile.updated_at.isoformat() if profile.updated_at is not None else None,
    )


async def _record_feed_feedback(
    request: Request, body: FeedFeedbackBody, tenant_id: CsrfProtectedTenant
) -> FeedFeedbackAccepted:
    """Record a tenant-local, derived preference signal for a later feed re-score.

    The API intentionally accepts only a canonical event id, a fixed signal kind, and a
    caller-minted idempotency key. It never accepts a body tenant, raw event text, dwell duration,
    or client-selected score. Until a signed served-item capability is introduced, these are
    self-reported feedback signals rather than cryptographically proven impressions (FR-1.1/1.3,
    FR-2.1, FR-4.3, NFR-8).
    """
    container: Container = request.app.state.container
    try:
        status = await container.ranking_feedback.record(
            tenant_id,
            RankingFeedbackSignal(
                signal_id=body.signal_id,
                canonical_event_id=body.canonical_event_id,
                kind=body.kind,
            ),
        )
    except UnknownFeedbackEventError as error:
        raise HTTPException(status_code=404, detail="canonical event not found") from error
    except RankingFeedbackConflictError as error:
        raise HTTPException(status_code=409, detail="feedback signal id conflicts") from error
    return FeedFeedbackAccepted(status=status.value)


async def _configure_temporal(app: FastAPI, settings: Settings, container: Container) -> None:
    """Install optional engine boundaries without making read-only API paths depend on Temporal."""
    app.state.temporal = None
    app.state.request_starter = None
    app.state.lifecycle_signaler = None
    # Configuration is a deployment invariant and fails startup even when the engine is down.
    # Reachability is different: the database outbox is the durable outage boundary, so a valid
    # deployment keeps accepting durable intake while the request-start worker waits for recovery.
    validate_temporal_settings(settings)
    try:
        from ..workflows.start import (
            TemporalRegistrationLifecycleSignaler,
            TemporalRequestWorkflowStarter,
        )

        app.state.temporal = await connect_temporal(
            settings,
            container.object_store,
            lazy=True,
            tenant_effect_authority=container.tenant_effect_authority,
        )
        app.state.request_starter = TemporalRequestWorkflowStarter(app.state.temporal, settings)
        app.state.lifecycle_signaler = TemporalRegistrationLifecycleSignaler(
            app.state.temporal, settings
        )
    except Exception as exc:
        _log.warning(
            "temporal unavailable; request starts remain durable in the start outbox",
            error=str(exc),
        )


async def _database_is_ready() -> bool:
    """Probe the application-role database path without opening a tenant data scope."""
    try:
        async with asyncio.timeout(_READINESS_TIMEOUT_SECONDS):
            async with system_session_scope() as session:
                await session.execute(text("SELECT 1"))
    except Exception as exc:
        _log.warning("database_readiness_failed", error=type(exc).__name__)
        return False
    return True


async def _temporal_is_reachable(app: FastAPI) -> bool:
    """Report engine degradation without making durable request intake unavailable."""
    client = getattr(app.state, "temporal", None)
    if client is None:
        return False
    try:
        return bool(
            await client.service_client.check_health(
                retry=False,
                timeout=timedelta(seconds=_READINESS_TIMEOUT_SECONDS),
            )
        )
    except Exception as exc:
        _log.warning("temporal_readiness_failed", error=type(exc).__name__)
        return False


async def _identity_is_ready(app: FastAPI) -> bool:
    """Bound readiness on the shared BFF session store without affecting local/direct auth."""
    container = getattr(app.state, "container", None)
    browser_session = cast(
        "BrowserSessionLifecyclePort | None",
        getattr(container, "browser_session", None),
    )
    if container is None or browser_session is None:
        return False
    try:
        async with asyncio.timeout(_READINESS_TIMEOUT_SECONDS):
            session_ready = await browser_session.is_ready()
            if app.state.settings.identity_platform_enabled:
                return session_ready and await container.consumer_accounts.is_ready(
                    legal_required=app.state.settings.consumer_legal_mode == "required"
                )
            return session_ready
    except (BrowserSessionUnavailableError, TimeoutError):
        return False


async def _mark_handoff_done(request: Request, token: str) -> HandoffCompletionAccepted:
    """Capability-authenticate and durably signal one retained handoff workflow.

    The path token is independent of the deterministic task id. PostgreSQL stores only its digest
    and returns an opaque routing target through a narrow privileged function; no caller-selected
    tenant/workflow/event identity is accepted. Temporal signal acknowledgement is the durability
    boundary. The workflow then performs FR-16 source read-back and a fresh freeBusy check before
    any lifecycle/calendar advance (FR-1.3, FR-6.3, FR-8.8).
    """
    container: Container = request.app.state.container
    target = await _resolve_handoff_completion_target(container, token)
    if target.status == "used":
        return HandoffCompletionAccepted(status="already_accepted")
    if target.status in {"expired", "inactive"}:
        raise HTTPException(status_code=410, detail="handoff task is no longer active")
    if target.status != "active":
        raise HTTPException(status_code=404, detail="handoff task not found")

    signaler: RegistrationLifecycleSignaler | None = request.app.state.lifecycle_signaler
    if signaler is None:
        raise HTTPException(
            status_code=503,
            detail="lifecycle engine unavailable; retry command",
        )
    completion_id = f"{target.workflow_id}:handoff-completion:{target.task_id}:1"
    try:
        await signaler.signal_handoff_completed(
            target.workflow_id,
            target.task_id,
            completion_id,
        )
    except Exception as exc:
        _log.warning(
            "handoff_completion_signal_failed",
            workflow_id=target.workflow_id,
            task_id=target.task_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=503,
            detail="handoff completion was not accepted; retry",
        ) from exc
    return HandoffCompletionAccepted(status="accepted")


async def _show_handoff_done(request: Request, token: str) -> HTMLResponse:
    """Show a confirmation form; GET itself never consumes a token or signals Temporal."""
    container: Container = request.app.state.container
    target = await _resolve_handoff_completion_target(container, token)
    if target.status == "used":
        return _completion_page("Completion already submitted", show_form=False)
    if target.status in {"expired", "inactive"}:
        raise HTTPException(status_code=410, detail="handoff task is no longer active")
    if target.status != "active":
        raise HTTPException(status_code=404, detail="handoff task not found")
    return _completion_page("Confirm completed registration", show_form=True)


def _secure_auth_response(response: Response) -> None:
    """Keep authorization codes, state, and session changes out of caches and referrers."""
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"


def _set_login_cookie(
    response: Response,
    browser_session: BrowserSessionLifecyclePort,
    token: str,
) -> None:
    response.set_cookie(
        browser_session.login_cookie_name,
        token,
        max_age=browser_session.login_ttl_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def _set_session_cookies(
    response: Response,
    browser_session: BrowserSessionLifecyclePort,
    session_token: str,
    csrf_token: str,
) -> None:
    response.set_cookie(
        browser_session.session_cookie_name,
        session_token,
        max_age=browser_session.session_ttl_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        browser_session.csrf_cookie_name,
        csrf_token,
        max_age=browser_session.session_ttl_seconds,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )


def _clear_browser_cookies(
    response: Response,
    browser_session: BrowserSessionLifecyclePort,
) -> None:
    response.delete_cookie(
        browser_session.login_cookie_name,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        browser_session.session_cookie_name,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        browser_session.csrf_cookie_name,
        path="/",
        secure=True,
        httponly=False,
        samesite="strict",
    )


def _clear_login_cookie(
    response: Response,
    browser_session: BrowserSessionLifecyclePort,
) -> None:
    response.delete_cookie(
        browser_session.login_cookie_name,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, local=settings.env == "local")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        app.state.container = build_container(
            settings, runtime_ports=preflight_application_runtime(settings)
        )
        operator_database = None
        try:
            if settings.admin_ingestion_enabled:
                operator_database = build_operator_services(app, settings)
            await _configure_temporal(app, settings, app.state.container)
            yield
        finally:
            if operator_database is not None:
                await operator_database.aclose()
            browser_session = app.state.container.browser_session
            if browser_session is not None:
                await browser_session.aclose()
            await dispose_engine()

    app = FastAPI(title="Events Concierge", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.ingestion_admin = None
    install_firebase_auth_helper(app, settings)

    @app.exception_handler(RequestValidationError)
    async def redact_auth_validation(request: Request, error: RequestValidationError) -> Response:
        if request.url.path.startswith("/auth/") or is_muse_path(request.url.path):
            # FastAPI's default validation payload includes input values, including ID tokens.
            detail = (
                "invalid Muse request"
                if is_muse_path(request.url.path)
                else "invalid sign-in request"
            )
            response = JSONResponse({"detail": detail}, status_code=422)
            _secure_auth_response(response)
            return response
        return await request_validation_exception_handler(request, error)

    @app.exception_handler(HTTPException)
    async def secure_auth_error(request: Request, error: HTTPException) -> Response:
        response = await http_exception_handler(request, error)
        if request.url.path.startswith("/auth/") or is_muse_path(request.url.path):
            _secure_auth_response(response)
        return response

    app.add_middleware(
        _BoundedRequestBodyMiddleware,
        max_body_bytes=_MAX_REQUEST_BODY_BYTES,
        body_read_timeout_seconds=settings.request_body_timeout_seconds,
    )
    metrics = ApplicationMetrics(
        release_revision=settings.release_revision,
        image_digest=settings.image_digest,
    )
    app.state.metrics = metrics
    app.add_middleware(HttpMetricsMiddleware, metrics=metrics)

    @app.get("/", response_class=FileResponse, include_in_schema=False)
    @app.get("/app", response_class=FileResponse, include_in_schema=False)
    async def consumer_shell() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "index.html",
            media_type="text/html; charset=utf-8",
            headers=dict(_UI_SECURITY_HEADERS),
        )

    if settings.admin_ingestion_enabled:

        @app.get("/admin", response_class=FileResponse, include_in_schema=False)
        @app.get("/admin/", response_class=FileResponse, include_in_schema=False)
        async def ingestion_admin_shell() -> FileResponse:
            return FileResponse(
                _STATIC_DIR / "admin.html",
                media_type="text/html; charset=utf-8",
                headers={
                    **_UI_SECURITY_HEADERS,
                    "Cache-Control": "no-store, max-age=0",
                },
            )

    @app.get("/assets/{asset_name}", response_class=FileResponse, include_in_schema=False)
    async def consumer_asset(asset_name: str) -> FileResponse:
        asset = _STATIC_ASSETS.get(asset_name)
        if asset is None:
            raise HTTPException(status_code=404, detail="asset not found")
        media_type, filename = asset
        return FileResponse(
            _STATIC_DIR / filename,
            media_type=media_type,
            headers={
                "Cache-Control": "no-cache, max-age=0",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/manifest.webmanifest", response_class=FileResponse, include_in_schema=False)
    async def consumer_manifest() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "manifest.webmanifest",
            media_type="application/manifest+json",
            headers={
                "Cache-Control": "no-cache, max-age=0",
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/favicon.svg", response_class=FileResponse, include_in_schema=False)
    async def consumer_favicon() -> FileResponse:
        return await consumer_asset("mark.svg")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/versionz", response_model=VersionOut, include_in_schema=False)
    async def versionz() -> Response:
        return JSONResponse(
            content={
                "release_revision": settings.release_revision,
                "image_digest": settings.image_digest,
            },
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        return Response(
            content=metrics.render(),
            headers={
                "Cache-Control": "no-store, max-age=0",
                "Content-Type": PROMETHEUS_CONTENT_TYPE,
            },
        )

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        """Expose dependency readiness while treating a Temporal outage as durable degradation."""
        database_ready = await _database_is_ready()
        temporal_ready = await _temporal_is_reachable(app)
        identity_configured = settings.oidc_bff_enabled or settings.identity_platform_enabled
        identity_ready = not identity_configured or await _identity_is_ready(app)
        required_ready = database_ready and identity_ready
        metrics.set_dependency_ready("database", database_ready)
        metrics.set_dependency_ready("temporal", temporal_ready)
        metrics.set_dependency_ready("identity", identity_ready)
        return JSONResponse(
            status_code=200 if required_ready else 503,
            content={
                "status": "ready" if required_ready else "not_ready",
                "components": {
                    "database": "ready" if database_ready else "unavailable",
                    "temporal": "ready" if temporal_ready else "degraded",
                    "identity": (
                        "ready"
                        if identity_configured and identity_ready
                        else ("unavailable" if identity_configured else "not_configured")
                    ),
                },
            },
        )

    @app.get("/v1/ui-config", response_model=UiConfigOut)
    async def ui_config() -> UiConfigOut:
        browser_session = getattr(getattr(app.state, "container", None), "browser_session", None)
        return UiConfigOut(
            release_profile=settings.release_profile,
            muse_enabled=settings.muse_enabled,
            catalog_name_suggestions_enabled=settings.catalog_name_suggestions_enabled,
            local_demo=settings.mock_cloud,
            auth_mode="local_demo" if settings.mock_cloud else "deployment_session",
            auth_provider=(
                "identity_platform"
                if settings.identity_platform_enabled
                else (settings.oidc_provider if settings.oidc_bff_enabled else None)
            ),
            identity_platform=(
                {
                    "project_id": settings.identity_platform_project_id,
                    "api_key": settings.identity_platform_api_key,
                    "auth_domain": settings.identity_platform_auth_domain,
                    "providers": list(settings.identity_platform_providers),
                }
                if settings.identity_platform_enabled
                else None
            ),
            consumer_legal_mode=settings.consumer_legal_mode,
            legal_policy=(
                {
                    "terms_version": settings.signup_terms_version,
                    "terms_url": settings.signup_terms_url,
                    "privacy_version": settings.signup_privacy_version,
                    "privacy_url": settings.signup_privacy_url,
                }
                if settings.identity_platform_enabled and settings.consumer_legal_mode == "required"
                else None
            ),
            auth_start_url=(
                settings.ui_auth_start_url
                or (
                    "/sign-in"
                    if settings.identity_platform_enabled
                    else ("/auth/login" if settings.oidc_bff_enabled else None)
                )
            ),
            reauth_url=(
                "/auth/reauth"
                if browser_session is not None
                and (settings.identity_platform_enabled or settings.oidc_provider != "google")
                else None
            ),
            logout_url="/auth/logout" if browser_session is not None else None,
            csrf_cookie_name=(
                browser_session.csrf_cookie_name if browser_session is not None else None
            ),
            csrf_header_name=(
                browser_session.csrf_header_name if browser_session is not None else None
            ),
        )

    if not settings.mock_cloud:

        def configured_browser_session() -> BrowserSessionLifecyclePort:
            browser_session = cast(
                "BrowserSessionLifecyclePort | None",
                app.state.container.browser_session,
            )
            if browser_session is None:
                raise HTTPException(status_code=503, detail="browser identity is unavailable")
            return browser_session

        if settings.identity_platform_enabled:

            @app.get("/auth/identity/start", include_in_schema=False)
            async def identity_signin_start(
                return_to: str = Query(default="/", max_length=2048),
            ) -> Response:
                browser = configured_browser_session()
                if not isinstance(browser, IdentityPlatformBrowserSessionAdapter):
                    raise HTTPException(503, "browser identity is unavailable")
                if not await _identity_is_ready(app):
                    raise HTTPException(503, "browser identity is unavailable")
                try:
                    challenge = await browser.begin_login(return_to)
                except ValueError as error:
                    raise HTTPException(400, "invalid application return path") from error
                except BrowserSessionUnavailableError as error:
                    raise HTTPException(503, "browser identity is unavailable") from error
                response = JSONResponse({"state": challenge.state})
                _set_login_cookie(response, browser, challenge.transaction_token)
                _secure_auth_response(response)
                return response

            @app.post("/auth/identity/session", include_in_schema=False)
            async def identity_signin_complete(
                request: Request, body: IdentitySessionBody
            ) -> Response:
                browser = configured_browser_session()
                if not isinstance(browser, IdentityPlatformBrowserSessionAdapter):
                    raise HTTPException(503, "browser identity is unavailable")
                try:
                    completion = await browser.complete_identity_login(
                        request.headers,
                        token=body.id_token,
                        state=body.state,
                        google_id_token=body.google_id_token,
                    )
                    policy = settings.consumer_legal_policy
                    credentials = None
                    if not completion.reauthenticated:
                        if policy is None:
                            if body.accepted_terms or body.terms_version or body.privacy_version:
                                raise HTTPException(400, "legal acceptance is deferred")
                            tenant_id = await app.state.container.consumer_accounts.bootstrap(
                                completion.identity
                            )
                        elif not body.accepted_terms:
                            raise HTTPException(428, "terms and privacy acceptance required")
                        elif (
                            body.terms_version != policy.terms_version
                            or body.privacy_version != policy.privacy_version
                        ):
                            raise HTTPException(
                                409, "legal documents changed; review them before signing in"
                            )
                        else:
                            tenant_id = await app.state.container.consumer_accounts.accept(
                                completion.identity, policy
                            )
                        identity = BrowserIdentity(
                            tenant_id,
                            completion.identity.subject,
                            completion.identity.authenticated_at,
                        )

                        async def issue_bound_identity_session() -> BrowserSessionCredentials:
                            try:
                                await browser.revoke_session(request.headers)
                            except AuthenticationFailedError as error:
                                raise ConsumerSignInRejectedError(
                                    ConsumerSignInFailureReason.EXISTING_SESSION_COOKIE
                                ) from error
                            return await browser.issue_session(identity)

                        credentials = await app.state.container.tenant_effect_authority.run(
                            TenantEffectRequest(
                                tenant_id=tenant_id,
                                kind=TenantEffectKind.BROWSER_SESSION,
                                timeout_seconds=settings.tenant_effect_timeout_seconds,
                            ),
                            issue_bound_identity_session,
                        )
                except (AuthenticationFailedError, TenantEffectFencedError) as error:
                    reason = ConsumerSignInFailureReason.UNKNOWN
                    if isinstance(error, ConsumerSignInRejectedError):
                        reason = error.reason
                    elif isinstance(error, TenantEffectFencedError):
                        reason = ConsumerSignInFailureReason.ACCOUNT_FENCED
                    metrics.observe_consumer_sign_in_rejection(reason)
                    raise HTTPException(401, "sign-in could not be verified") from error
                except (
                    BrowserSessionUnavailableError,
                    DBAPIError,
                    SqlAlchemyTimeoutError,
                    TenantEffectLockTimeoutError,
                    TenantEffectTimedOutError,
                ) as error:
                    raise HTTPException(503, "browser identity is unavailable") from error
                response = JSONResponse({"return_to": completion.return_to})
                _clear_login_cookie(response, browser)
                if credentials is not None:
                    _clear_browser_cookies(response, browser)
                    _set_session_cookies(
                        response, browser, credentials.session_token, credentials.csrf_token
                    )
                _secure_auth_response(response)
                return response

        @app.get("/auth/login", include_in_schema=False)
        async def oidc_login(
            return_to: str = Query(
                default="/" if settings.release_profile == "discovery" else "/app",
                max_length=2048,
            ),
        ) -> RedirectResponse:
            browser_session = configured_browser_session()
            if settings.identity_platform_enabled:
                try:
                    safe_return = _safe_return_path(return_to)
                except ValueError as error:
                    raise HTTPException(400, "invalid application return path") from error
                return RedirectResponse(
                    "/sign-in?" + urlencode({"return_to": safe_return}),
                    status_code=303,
                    headers={"Cache-Control": "no-store"},
                )
            try:
                login = await browser_session.start_login(return_to)
            except ValueError as error:
                raise HTTPException(
                    status_code=400,
                    detail="invalid application return path",
                ) from error
            except BrowserSessionUnavailableError as error:
                raise HTTPException(
                    status_code=503,
                    detail="browser identity is unavailable",
                ) from error
            response = RedirectResponse(login.authorization_url, status_code=302)
            _set_login_cookie(response, browser_session, login.transaction_token)
            _secure_auth_response(response)
            return response

        @app.post("/auth/reauth", include_in_schema=False)
        async def oidc_reauthenticate(
            request: Request,
            body: ReauthenticationBody,
            tenant_id: CsrfProtectedTenant,
        ) -> Response:
            """Start a one-shot interactive OIDC step-up for a destructive account command."""
            browser_session = configured_browser_session()
            try:
                login = await browser_session.start_reauthentication(
                    tenant_id,
                    request.headers,
                    return_to=body.return_to,
                )
            except ValueError as error:
                raise HTTPException(
                    status_code=400,
                    detail="invalid application return path",
                ) from error
            except BrowserSessionUnavailableError as error:
                raise HTTPException(
                    status_code=503,
                    detail="browser identity is unavailable",
                ) from error
            except (AuthenticationFailedError, CsrfVerificationFailedError) as error:
                raise HTTPException(
                    status_code=403,
                    detail="state-change verification required",
                ) from error
            except BrowserStepUpUnavailableError as error:
                raise HTTPException(
                    status_code=503,
                    detail="account deletion is unavailable until a supported step-up method is configured",
                ) from error
            response = JSONResponse(
                {"authorization_url": login.authorization_url},
                headers={"Cache-Control": "no-store, max-age=0"},
            )
            _set_login_cookie(response, browser_session, login.transaction_token)
            _secure_auth_response(response)
            return response

        @app.get("/auth/callback", include_in_schema=False)
        async def oidc_callback(
            request: Request,
            code: str | None = Query(default=None, min_length=1, max_length=4096),
            state: str = Query(min_length=1, max_length=256),
            error: str | None = Query(default=None, min_length=1, max_length=256),
        ) -> Response:
            browser_session = configured_browser_session()
            credentials = None
            failure_reason = "not_authorized"
            failure: Response
            try:
                if error is not None:
                    await browser_session.cancel_login(request.headers, state=state)
                    failure_reason = "cancelled"
                    raise AuthenticationFailedError("provider login did not complete")
                if code is None:
                    raise AuthenticationFailedError("valid login callback is required")
                completion = await browser_session.complete_login(
                    request.headers,
                    code=code,
                    state=state,
                )
                tenant = await app.state.container.tenant_repo.get(completion.identity.tenant_id)
                if tenant is None or not hmac.compare_digest(
                    tenant.oidc_subject.encode("utf-8"),
                    completion.identity.subject.encode("utf-8"),
                ):
                    raise AuthenticationFailedError("OIDC identity is not bound to this account")
                if not completion.reauthenticated:
                    # The account read finishes before acquiring the erasure lock; the guarded
                    # effect needs Redis only, so it cannot exhaust the pool awaiting another DB
                    # connection. The authority rechecks the durable tombstone before issuance.
                    async def issue_bound_session() -> BrowserSessionCredentials:
                        await browser_session.revoke_session(request.headers)
                        return await browser_session.issue_session(completion.identity)

                    credentials = await app.state.container.tenant_effect_authority.run(
                        TenantEffectRequest(
                            tenant_id=completion.identity.tenant_id,
                            kind=TenantEffectKind.BROWSER_SESSION,
                            timeout_seconds=settings.tenant_effect_timeout_seconds,
                        ),
                        issue_bound_session,
                    )
            except (AuthenticationFailedError, TenantEffectFencedError):
                failure = (
                    RedirectResponse(f"/sign-in?reason={failure_reason}", status_code=303)
                    if settings.oidc_provider == "google"
                    else JSONResponse(
                        status_code=401, content={"detail": "login could not be verified"}
                    )
                )
                _clear_login_cookie(failure, browser_session)
                _secure_auth_response(failure)
                return failure
            except (
                BrowserSessionUnavailableError,
                BrowserStepUpUnavailableError,
                SqlAlchemyTimeoutError,
                DBAPIError,
                TenantEffectLockTimeoutError,
                TenantEffectTimedOutError,
            ):
                failure = (
                    RedirectResponse("/sign-in?reason=unavailable", status_code=303)
                    if settings.oidc_provider == "google"
                    else JSONResponse(
                        status_code=503, content={"detail": "browser identity is unavailable"}
                    )
                )
                _clear_login_cookie(failure, browser_session)
                _secure_auth_response(failure)
                return failure
            response = RedirectResponse(completion.return_to, status_code=303)
            if completion.reauthenticated:
                _clear_login_cookie(response, browser_session)
            else:
                if credentials is None:  # pragma: no cover - guarded by the branch above
                    raise RuntimeError("login completion did not issue a browser session")
                _clear_browser_cookies(response, browser_session)
                _set_session_cookies(
                    response,
                    browser_session,
                    credentials.session_token,
                    credentials.csrf_token,
                )
            _secure_auth_response(response)
            return response

        @app.post("/auth/logout", status_code=204, include_in_schema=False)
        async def oidc_logout(
            request: Request,
            tenant_id: Annotated[UUID, Depends(_csrf_session_tenant)],
        ) -> Response:
            del tenant_id
            browser_session = configured_browser_session()
            try:
                await browser_session.revoke_session(request.headers)
            except BrowserSessionUnavailableError as error:
                # Preserve the cookie when shared revocation might not have happened.
                raise HTTPException(
                    status_code=503,
                    detail="browser identity is unavailable",
                ) from error
            response = Response(status_code=204)
            _clear_browser_cookies(response, browser_session)
            _secure_auth_response(response)
            return response

    if settings.mock_cloud:

        @app.post("/v1/onboard", status_code=201)
        async def onboard(body: OnboardBody) -> dict[str, str]:
            """Create a local fixture identity; production has no unauthenticated bootstrap route."""
            container: Container = app.state.container
            tenant = Tenant(
                tenant_id=uuid4(),
                oidc_subject=f"oidc|{uuid4()}",
                notify_email=body.notify_email,
                relay_inbox=f"{uuid4().hex[:12]}@u.concierge.test",
            )
            await container.tenant_repo.add(tenant)
            return {"tenant_id": str(tenant.tenant_id), "relay_inbox": tenant.relay_inbox}

    @app.get("/v1/me", response_model=MeOut)
    async def me(tenant_id: AuthenticatedTenant) -> MeOut:
        container: Container = app.state.container
        identity = await container.consumer.get_identity(tenant_id)
        if identity is None:
            raise HTTPException(status_code=404, detail="account not found")
        profile = await container.tenant_profiles.get_profile(tenant_id)
        avatar = await container.profile_avatars.get(tenant_id)
        role = await container.tenant_roles.get_role(tenant_id)
        return MeOut(
            notify_email=identity.notify_email,
            interests=list(identity.interests),
            preference_revision=identity.preference_revision,
            local_demo=settings.mock_cloud,
            is_admin=settings.mock_cloud and role in {TenantRole.ADMIN, TenantRole.OPERATOR},
            profile=_profile_out(profile, avatar),
        )

    @app.get("/v1/me/saved-filters", response_model=list[SavedFilterOut])
    async def list_saved_filters(tenant_id: AuthenticatedTenant) -> list[SavedFilterOut]:
        """List the caller's saved filter selections, most recently used first.

        Recency is the picker's default order, so the server returns it already sorted rather than
        making every client re-derive it.
        """
        container: Container = app.state.container
        stored = await container.saved_catalog_filters.list_filters(tenant_id)
        return [_saved_filter_out(saved) for saved in stored]

    @app.post("/v1/me/saved-filters", response_model=SavedFilterOut, status_code=201)
    async def create_saved_filter(
        body: SavedFilterBody,
        tenant_id: CsrfProtectedTenant,
    ) -> SavedFilterOut:
        """Save the current filter selection under a name."""
        container: Container = app.state.container
        try:
            stored = await container.saved_catalog_filters.save_filter(
                tenant_id,
                name=body.name,
                payload=_validated_saved_filter_payload(body.filters),
            )
        except ValueError as error:
            detail = (
                f"a tenant may keep at most {MAX_SAVED_CATALOG_FILTERS} saved filters"
                if "too many" in str(error)
                else "a saved filter with that name already exists"
            )
            raise HTTPException(status_code=409, detail=detail) from error
        return _saved_filter_out(stored)

    @app.put("/v1/me/saved-filters/{saved_filter_id}", response_model=SavedFilterOut)
    async def replace_saved_filter(
        saved_filter_id: UUID,
        body: SavedFilterBody,
        tenant_id: CsrfProtectedTenant,
    ) -> SavedFilterOut:
        """Rename a saved selection, or point it at a different filter set."""
        container: Container = app.state.container
        try:
            stored = await container.saved_catalog_filters.save_filter(
                tenant_id,
                name=body.name,
                payload=_validated_saved_filter_payload(body.filters),
                saved_filter_id=saved_filter_id,
            )
        except ValueError as error:
            if "not found" in str(error):
                raise HTTPException(status_code=404, detail="saved filter not found") from error
            raise HTTPException(
                status_code=409, detail="a saved filter with that name already exists"
            ) from error
        return _saved_filter_out(stored)

    @app.post("/v1/me/saved-filters/{saved_filter_id}/applied", response_model=SavedFilterOut)
    async def record_saved_filter_use(
        saved_filter_id: UUID,
        tenant_id: CsrfProtectedTenant,
    ) -> SavedFilterOut:
        """Record that a selection was applied, which is what recency ordering sorts on.

        Applying a saved filter is not an edit of it, so this deliberately moves only
        ``last_used_at`` and leaves the stored selection untouched.
        """
        container: Container = app.state.container
        try:
            stored = await container.saved_catalog_filters.touch_filter(tenant_id, saved_filter_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail="saved filter not found") from error
        return _saved_filter_out(stored)

    @app.delete("/v1/me/saved-filters/{saved_filter_id}", status_code=204)
    async def delete_saved_filter(
        saved_filter_id: UUID,
        tenant_id: CsrfProtectedTenant,
    ) -> Response:
        """Forget a saved selection."""
        container: Container = app.state.container
        removed = await container.saved_catalog_filters.delete_filter(tenant_id, saved_filter_id)
        if not removed:
            raise HTTPException(status_code=404, detail="saved filter not found")
        return Response(status_code=204)

    @app.put("/v1/me/profile", response_model=ProfileOut)
    async def replace_profile(
        body: ProfileBody,
        tenant_id: CsrfProtectedTenant,
    ) -> ProfileOut:
        """Replace the tenant's display facts.

        This route deliberately cannot reach an identity binding: the notification address, the
        RelayInbox, and the OIDC subject live on ``public.tenants``, which the application may only
        read. A stale revision is a conflict rather than a silent overwrite, matching the
        preferences contract the client already implements (FR-1.2--FR-1.4, ADR-011).
        """
        container: Container = app.state.container
        try:
            stored = await container.tenant_profiles.replace_profile(
                tenant_id,
                TenantProfile(
                    display_name=body.display_name,
                    time_zone=body.time_zone,
                    revision=body.revision,
                ),
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail="profile revision conflicts") from error
        avatar = await container.profile_avatars.get(tenant_id)
        return _profile_out(stored, avatar)

    @app.post(
        "/v1/me/erasure-requests",
        status_code=202,
        response_model=AccountErasureOut,
    )
    async def erase_account(
        body: AccountErasureBody,
        response: Response,
        tenant_id: AccountErasureProtectedTenant,
    ) -> AccountErasureOut:
        """Fence the authenticated account and enqueue independently resumed erasure.

        The request body cannot select a tenant. This endpoint performs no provider deletion in
        the HTTP lifetime: the leased worker owns convergence. While its session remains live the
        caller may replay the same request id only if a lost response leaves its session available.
        Every received response clears the current browser credentials immediately; tenant-wide
        revocation follows durably, so the product promises accepted convergence rather than an
        authenticated completion poll. No provider error text or artifact identifier crosses this
        boundary.
        """
        container: Container = app.state.container
        try:
            snapshot = await container.account_erasure_repo.begin(tenant_id, body.request_id)
        except AccountErasureConflictError as error:
            raise HTTPException(
                status_code=409,
                detail="account erasure request id conflicts",
            ) from error
        except AccountErasureAccountNotFoundError as error:
            raise HTTPException(status_code=404, detail="account not found") from error

        if snapshot.status is AccountErasureStatus.COMPLETED:
            response.status_code = 200
        else:
            response.headers["Retry-After"] = str(int(settings.account_erasure_poll_seconds))
        if container.browser_session is not None:
            _clear_browser_cookies(response, container.browser_session)
        response.headers["Cache-Control"] = "no-store, max-age=0"
        return AccountErasureOut(
            request_id=snapshot.request_id,
            status=snapshot.status.value,
            workflow_targets=snapshot.workflow_target_count,
            workflows_cancelled=(
                snapshot.workflow_target_count if snapshot.workflows_completed else 0
            ),
            calendar_targets=snapshot.calendar_target_count,
            calendar_deleted=(snapshot.calendar_target_count if snapshot.calendar_completed else 0),
            browser_sessions_revoked=snapshot.browser_sessions_completed,
            credential_vault_purged=snapshot.credential_vault_completed,
            object_store_purged=snapshot.object_store_completed,
            retained_audit_rows=snapshot.retained_audit_rows,
            failed_stage=(
                snapshot.last_failure_stage.value
                if snapshot.last_failure_stage is not None
                else None
            ),
        )

    @app.put("/v1/preferences", response_model=PreferencesOut)
    async def replace_preferences(
        body: PreferencesBody,
        tenant_id: CsrfProtectedTenant,
    ) -> PreferencesOut:
        container: Container = app.state.container
        current = await container.ranking_profiles.get_profile(tenant_id)
        try:
            result = await container.ranking_profiles.replace_profile(
                tenant_id,
                RankingProfileUpdate(
                    profile=UserRankingProfile(
                        explicit_affinities=dict.fromkeys(body.interests, 1.0),
                        implicit_affinities=current.implicit_affinities,
                    ),
                    revision=body.revision,
                ),
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail="preference revision conflicts") from error
        return PreferencesOut(
            status=result.status.value,
            interests=sorted(result.profile.explicit_affinities),
            revision=result.revision,
        )

    @app.post("/v1/me/avatar", response_model=AvatarOut)
    async def upload_avatar(
        request: Request,
        tenant_id: CsrfProtectedTenant,
    ) -> AvatarOut:
        """Accept one image, re-encode it, and store the result.

        The body is raw bytes rather than a multipart form for three reasons: ``image/*`` is not a
        CORS-simple content type, so a cross-origin form cannot reach this route at all; no
        multipart parser enters the dependency graph; and there is no filename field, which removes
        the path-traversal class rather than defending against it.

        The uploaded bytes are never stored. What lands in the media store is a fresh re-encode,
        which is what strips EXIF and neutralizes polyglots (see ``application.profile_media``).
        """
        container: Container = app.state.container
        raw = await request.body()
        try:
            normalized = normalize_avatar(raw, request.headers.get("content-type"))
        except AvatarRejectedError as error:
            # The message is a fixed product string, never decoder text.
            raise HTTPException(status_code=400, detail=str(error)) from error

        async def mutate_avatar() -> AvatarOut:
            previous = await container.profile_avatars.get(tenant_id)

            # Store the object before the index points at it: the reverse order can leave a row
            # referencing bytes that were never written, which reads as a broken image forever.
            async def put_media() -> None:
                await container.media_store.put(
                    tenant_id,
                    normalized.storage_key,
                    normalized.data,
                    normalized.content_type,
                )

            try:
                await container.tenant_effect_authority.run(
                    TenantEffectRequest(
                        tenant_id=tenant_id,
                        kind=TenantEffectKind.PROFILE_MEDIA_WRITE,
                        timeout_seconds=settings.tenant_effect_timeout_seconds,
                    ),
                    put_media,
                )
            except TenantEffectFencedError as error:
                # Authentication may have completed just before erasure installed its tombstone.
                # Preserve the ordinary-route contract while the authority prevents the object write.
                raise HTTPException(
                    status_code=423, detail="account erasure in progress"
                ) from error

            # This repository opens its own transaction, whose database trigger is the write fence.
            # Keeping it outside the external-effect authority avoids re-entering the same advisory
            # lock from a separate session while still ordering the object write before erasure.
            try:
                stored = await container.profile_avatars.replace(
                    tenant_id,
                    ProfileAvatar(
                        storage_key=normalized.storage_key,
                        content_type=normalized.content_type,
                        byte_size=normalized.byte_size,
                        width_px=normalized.width_px,
                        height_px=normalized.height_px,
                        checksum_sha256=normalized.checksum_sha256,
                    ),
                )
            except DBAPIError as error:
                if not _is_account_erasure_write_fence(error):
                    raise
                raise HTTPException(
                    status_code=423,
                    detail="account erasure in progress",
                ) from error
            if previous is not None and previous.storage_key != stored.storage_key:
                # Best effort: a surviving orphan costs 32 KiB, while failing the request after the
                # index already advanced would report failure for a change that took effect.
                with suppress(Exception):
                    await container.media_store.delete(tenant_id, previous.storage_key)

            return AvatarOut(
                avatar_url=_avatar_url(stored) or "",
                width=stored.width_px,
                height=stored.height_px,
                content_type=stored.content_type,
                byte_size=stored.byte_size,
                checksum=stored.checksum_sha256,
            )

        try:
            return await container.profile_media_mutations.run(tenant_id, mutate_avatar)
        except (ProfileMediaMutationBusyError, TenantEffectTimedOutError) as error:
            raise HTTPException(
                status_code=503, detail="avatar update temporarily unavailable"
            ) from error

    @app.get("/v1/me/avatar")
    async def read_avatar(tenant_id: AuthenticatedTenant) -> Response:
        """Serve the caller's own avatar.

        There is deliberately no id-addressable variant. In a product with no public profile, an
        avatar readable by tenant id is a tenant-enumeration oracle, so the only readable avatar is
        the session's own.

        The response must never enter a shared cache: it varies by session at one fixed URL, so an
        intermediary keyed on the path alone would serve one tenant's face to another.
        """
        container: Container = app.state.container
        record = await container.profile_avatars.get(tenant_id)
        if record is None:
            raise HTTPException(status_code=404, detail="no avatar is set")
        try:
            data = await container.media_store.get(tenant_id, record.storage_key)
        except MediaNotFoundError as error:
            raise HTTPException(status_code=404, detail="no avatar is set") from error
        return Response(
            content=data,
            # The stored type from the closed vocabulary, never a request-supplied one.
            media_type=record.content_type,
            headers={
                "Cache-Control": "private, max-age=0, must-revalidate",
                "Vary": "Cookie",
                "ETag": f'"{record.checksum_sha256}"',
                "X-Content-Type-Options": "nosniff",
                "Content-Disposition": 'inline; filename="avatar.webp"',
                "Cross-Origin-Resource-Policy": "same-origin",
            },
        )

    @app.delete("/v1/me/avatar", status_code=204)
    async def delete_avatar(tenant_id: CsrfProtectedTenant) -> Response:
        """Remove the avatar and purge its object. Deleting an absent avatar succeeds."""
        container: Container = app.state.container

        async def mutate_avatar() -> Response:
            current = await container.profile_avatars.get(tenant_id)

            async def purge_current() -> ProfileAvatar | None:
                if current is not None:
                    await container.media_store.delete(tenant_id, current.storage_key)
                return current

            try:
                removed = await container.tenant_effect_authority.run(
                    TenantEffectRequest(
                        tenant_id=tenant_id,
                        kind=TenantEffectKind.PROFILE_MEDIA_WRITE,
                        timeout_seconds=settings.tenant_effect_timeout_seconds,
                    ),
                    purge_current,
                )
            except TenantEffectFencedError as error:
                raise HTTPException(
                    status_code=423, detail="account erasure in progress"
                ) from error
            except Exception as error:
                # Retain the index so the same DELETE can retry the exact failed object. Provider
                # details can contain bucket names and identities and must not reach the client.
                raise HTTPException(
                    status_code=503, detail="avatar removal temporarily unavailable"
                ) from error

            if removed is not None:
                # The trigger-protected write uses a separate session. Keep it outside external-effect
                # authority to avoid advisory-lock re-entry, and never remove a raced replacement.
                try:
                    deleted = await container.profile_avatars.delete(tenant_id, expected=removed)
                except DBAPIError as error:
                    if not _is_account_erasure_write_fence(error):
                        raise
                    raise HTTPException(
                        status_code=423, detail="account erasure in progress"
                    ) from error
                if deleted is None:
                    raise HTTPException(status_code=409, detail="avatar changed; retry removal")
            return Response(status_code=204)

        try:
            return await container.profile_media_mutations.run(tenant_id, mutate_avatar)
        except (ProfileMediaMutationBusyError, TenantEffectTimedOutError) as error:
            raise HTTPException(
                status_code=503, detail="avatar removal temporarily unavailable"
            ) from error

    @app.get("/v1/me/api-keys", response_model=list[ApiKeyOut])
    async def list_api_keys(tenant_id: AuthenticatedTenant) -> list[ApiKeyOut]:
        """List the caller's keys. No response from this route ever carries a secret."""
        records = await app.state.container.api_keys.list_keys(tenant_id)
        return [_api_key_out(record) for record in records]

    @app.post("/v1/me/api-keys", status_code=201, response_model=IssuedApiKeyOut)
    async def create_api_key(
        body: ApiKeyBody,
        tenant_id: CsrfProtectedTenant,
    ) -> IssuedApiKeyOut:
        """Mint one key and return its secret exactly once.

        The secret is generated here from the system CSPRNG; a caller can never supply or influence
        it. Only its digest is persisted, so this response is the sole opportunity to copy it.
        """
        container: Container = app.state.container
        secret, prefix, digest = _mint_api_key()
        record = await container.api_keys.issue(tenant_id, uuid4(), body.name, prefix, digest)
        return IssuedApiKeyOut(key=_api_key_out(record), secret=secret)

    @app.delete("/v1/me/api-keys/{key_id}", response_model=ApiKeyOut)
    async def revoke_api_key(
        key_id: UUID,
        tenant_id: CsrfProtectedTenant,
    ) -> ApiKeyOut:
        """Revoke one key. Revoking an already-revoked key converges rather than erroring."""
        record = await app.state.container.api_keys.revoke(tenant_id, key_id)
        if record is None:
            raise HTTPException(status_code=404, detail="api key not found")
        return _api_key_out(record)

    @app.get("/v1/requests", response_model=RequestPageOut)
    async def request_history(
        tenant_id: AuthenticatedTenant,
        cursor: str | None = Query(default=None, max_length=len(str(_MAX_FEED_CURSOR))),
        limit: int = Query(default=10, ge=1, le=50),
    ) -> RequestPageOut:
        offset = _page_offset(cursor)
        rows = await app.state.container.consumer.list_requests(
            tenant_id,
            offset=offset,
            limit=limit + 1,
        )
        return RequestPageOut(
            items=[_request_summary_out(item) for item in rows[:limit]],
            next_cursor=_next_page_cursor(offset, limit, len(rows)),
        )

    @app.get("/v1/registrations", response_model=RegistrationPageOut)
    async def registration_history(
        tenant_id: AuthenticatedTenant,
        cursor: str | None = Query(default=None, max_length=len(str(_MAX_FEED_CURSOR))),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> RegistrationPageOut:
        offset = _page_offset(cursor)
        rows = await app.state.container.consumer.list_registrations(
            tenant_id,
            offset=offset,
            limit=limit + 1,
        )
        return RegistrationPageOut(
            items=[_registration_summary_out(item) for item in rows[:limit]],
            next_cursor=_next_page_cursor(offset, limit, len(rows)),
        )

    @app.get("/v1/tasks", response_model=TaskPageOut)
    async def handoff_tasks(
        tenant_id: AuthenticatedTenant,
        state: Literal["actionable", "all"] = Query(default="actionable"),
        cursor: str | None = Query(default=None, max_length=len(str(_MAX_FEED_CURSOR))),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> TaskPageOut:
        offset = _page_offset(cursor)
        rows = await app.state.container.consumer.list_tasks(
            tenant_id,
            actionable_only=state == "actionable",
            offset=offset,
            limit=limit + 1,
        )
        return TaskPageOut(
            items=[_task_summary_out(item) for item in rows[:limit]],
            next_cursor=_next_page_cursor(offset, limit, len(rows)),
        )

    @app.post("/v1/requests", response_model=RequestAccepted)
    async def create_request(body: RequestBody, tenant_id: CsrfProtectedTenant) -> RequestAccepted:
        container: Container = app.state.container
        intake = RequestIntakeService(container.request_repo, container.parser)
        request = await intake.accept(tenant_id, body.text)
        feed = await container.feed.build_feed(request)

        started = False
        starter = app.state.request_starter
        if starter is not None:
            delivery = RequestStartWorker(
                container.request_repo,
                starter,
                lease_seconds=settings.request_start_lease_seconds,
                tenant_effect_authority=container.tenant_effect_authority,
                tenant_effect_timeout_seconds=settings.tenant_effect_timeout_seconds,
            )
            started = await delivery.start_request(tenant_id, request.request_id)
        return RequestAccepted(
            request_id=request.request_id,
            workflow_started=started,
            feed=_feed_out(feed, request.constraints),
        )

    @app.post("/v1/feed", response_model=FeedOut)
    async def feed_page(body: RequestBody, tenant_id: AuthenticatedTenant) -> FeedOut:
        container: Container = app.state.container
        request, _ = await _parse(container, tenant_id, body.text)
        feed = await container.feed.build_feed(request, cursor=body.cursor)
        return _feed_out(feed, request.constraints)

    @app.get("/v1/catalog/events", response_model=CatalogBrowsePageOut)
    async def browse_catalog_events(
        source_key: Annotated[
            list[
                Annotated[
                    str,
                    Field(min_length=2, max_length=80, pattern=_CATALOG_SOURCE_KEY_PATTERN),
                ]
            ]
            | None,
            Query(),
        ] = None,
        starts_after: Annotated[datetime | None, Query()] = None,
        starts_before: Annotated[datetime | None, Query()] = None,
        date_range: Annotated[list[str] | None, Query()] = None,
        q: str | None = Query(default=None, max_length=_MAX_CATALOG_FILTER_LENGTH),
        city: Annotated[list[str] | None, Query()] = None,
        location_scope: Annotated[
            list[Literal["bay_area", "manhattan", "los_angeles_area"]] | None,
            Query(),
        ] = None,
        price: Literal["free", "paid", "unknown"] | None = Query(default=None),
        price_max_cents: int | None = Query(default=None, ge=1, le=100_000_000),
        price_min_cents: int | None = Query(default=None, ge=1, le=100_000_000),
        topic: Annotated[list[str] | None, Query()] = None,
        availability: Literal["available", "sold_out"] | None = Query(default=None),
        sort: Literal["soonest", "latest"] = Query(default="soonest"),
        cursor: str | None = Query(default=None, max_length=_MAX_CATALOG_CURSOR_LENGTH),
        limit: int = Query(default=50, ge=1, le=100),
        include_facets: bool = Query(default=True),
    ) -> CatalogBrowsePageOut:
        """Filter current observations before stable chronological pagination in either direction.

        ``date_range`` may repeat. Date-only values (``YYYY-MM-DD..YYYY-MM-DD``) include both
        calendar dates; timezone-aware datetime values retain exclusive-end timestamp semantics.

        ``include_facets=false`` answers with the page alone. The source, city, and topic
        inventories are three whole-catalog aggregations that together cost more than the page
        itself, and the calendar's day agenda and week previews read only ``items``; they ask for
        the page they render rather than paying for inventories they discard. The default stays
        true so every existing caller keeps the response it already parses.
        """
        source_keys = tuple(dict.fromkeys(source_key or []))
        if len(source_keys) > _MAX_CATALOG_SOURCE_SELECTIONS:
            raise HTTPException(status_code=422, detail="too many catalog sources")
        if (starts_after is None) != (starts_before is None):
            raise HTTPException(status_code=422, detail="both catalog date bounds are required")
        if starts_after is not None and starts_before is not None:
            if starts_after.tzinfo is None or starts_after.utcoffset() is None:
                raise HTTPException(status_code=422, detail="catalog dates require a timezone")
            if starts_before.tzinfo is None or starts_before.utcoffset() is None:
                raise HTTPException(status_code=422, detail="catalog dates require a timezone")
            starts_after = starts_after.astimezone(UTC)
            starts_before = starts_before.astimezone(UTC)
            max_window = (
                _MAX_SOURCE_CATALOG_ARCHIVE_WINDOW if source_keys else _MAX_CATALOG_BROWSE_WINDOW
            )
            if starts_before <= starts_after or starts_before - starts_after > max_window:
                raise HTTPException(status_code=422, detail="catalog date range is invalid")
        try:
            date_ranges = _normalize_catalog_date_ranges(
                date_range or [],
                starts_after=starts_after,
                starts_before=starts_before,
                source_keys=source_keys,
            )
        except (OverflowError, ValueError):
            raise HTTPException(status_code=422, detail="catalog date range is invalid") from None
        selection = _normalized_catalog_filters(
            q=q,
            city=city,
            location_scope=location_scope,
            topic=topic,
            price=price,
            price_max_cents=price_max_cents,
            price_min_cents=price_min_cents,
        )
        query = selection.query
        city_filters = selection.cities
        location_scopes = selection.location_scopes
        topic_filters = selection.topics
        filter_scope = _catalog_filter_scope(
            source_keys=source_keys,
            starts_after=None,
            starts_before=None,
            date_ranges=date_ranges,
            query=query,
            cities=city_filters,
            location_scopes=location_scopes,
            price=price,
            price_max_cents=price_max_cents,
            price_min_cents=price_min_cents,
            topics=topic_filters,
            sort=sort,
            availability=availability,
        )
        after = _catalog_cursor_in(cursor, filter_scope)
        rows, providers = await app.state.container.catalog.browse_current(
            source_keys=source_keys,
            after=after,
            limit=limit + 1,
            starts_after=None,
            starts_before=None,
            date_ranges=date_ranges,
            query=query,
            cities=city_filters,
            location_scopes=location_scopes,
            price=price,
            price_max_cents=price_max_cents,
            price_min_cents=price_min_cents,
            topics=topic_filters,
            availability=availability,
            sort=sort,
            include_providers=include_facets,
        )
        city_facets, topic_facets = (
            await asyncio.gather(
                app.state.container.catalog.list_city_facets(),
                app.state.container.catalog.list_topic_facets(
                    source_keys=source_keys,
                    starts_after=None,
                    starts_before=None,
                    date_ranges=date_ranges,
                    query=query,
                    cities=city_filters,
                    location_scopes=location_scopes,
                    price=price,
                    price_max_cents=price_max_cents,
                    price_min_cents=price_min_cents,
                    availability=availability,
                ),
            )
            if cursor is None and include_facets
            else ([], [])
        )
        items = rows[:limit]
        next_cursor = (
            _catalog_cursor_out(items[-1], filter_scope) if len(rows) > limit and items else None
        )
        return CatalogBrowsePageOut(
            items=[_catalog_browse_item_out(item) for item in items],
            next_cursor=next_cursor,
            providers=[
                CatalogBrowseProviderOut(
                    source_key=provider.source_key,
                    label=provider.display_name,
                    display_name=provider.display_name,
                    publisher=provider.publisher,
                    provider=provider.provider,
                    seed_url=provider.seed_url,
                    event_count=provider.event_count,
                )
                for provider in providers
            ],
            city_facets=[
                CatalogBrowseCityOut(city=facet.city, event_count=facet.event_count)
                for facet in city_facets
            ],
            topic_facets=[
                CatalogBrowseTopicOut(
                    topic=facet.topic,
                    label=facet.label,
                    event_count=facet.event_count,
                )
                for facet in topic_facets
            ],
        )

    @app.get("/v1/catalog/name-suggestions", response_model=list[CatalogNameSuggestionOut])
    async def suggest_catalog_names(
        source_key: Annotated[
            list[
                Annotated[
                    str,
                    Field(min_length=2, max_length=80, pattern=_CATALOG_SOURCE_KEY_PATTERN),
                ]
            ]
            | None,
            Query(),
        ] = None,
        starts_after: Annotated[datetime | None, Query()] = None,
        starts_before: Annotated[datetime | None, Query()] = None,
        date_range: Annotated[list[str] | None, Query()] = None,
        q: str = Query(min_length=2, max_length=_MAX_CATALOG_FILTER_LENGTH),
        city: Annotated[list[str] | None, Query()] = None,
        location_scope: Annotated[
            list[Literal["bay_area", "manhattan", "los_angeles_area"]] | None,
            Query(),
        ] = None,
        price: Literal["free", "paid", "unknown"] | None = Query(default=None),
        price_max_cents: int | None = Query(default=None, ge=1, le=100_000_000),
        price_min_cents: int | None = Query(default=None, ge=1, le=100_000_000),
        topic: Annotated[list[str] | None, Query()] = None,
        availability: Literal["available", "sold_out"] | None = Query(default=None),
        limit: int = Query(default=8, ge=1, le=20),
    ) -> list[CatalogNameSuggestionOut]:
        """Suggest names across the filtered public catalog, independently of event pagination."""
        source_keys = tuple(dict.fromkeys(source_key or []))
        if len(source_keys) > _MAX_CATALOG_SOURCE_SELECTIONS:
            raise HTTPException(status_code=422, detail="too many catalog sources")
        if starts_after is not None or starts_before is not None:
            if (starts_after is None) != (starts_before is None):
                raise HTTPException(status_code=422, detail="both catalog date bounds are required")
            if (
                starts_after is not None
                and starts_before is not None
                and (
                    starts_after.tzinfo is None
                    or starts_after.utcoffset() is None
                    or starts_before.tzinfo is None
                    or starts_before.utcoffset() is None
                )
            ):
                raise HTTPException(status_code=422, detail="catalog dates require a timezone")
            starts_after = cast(datetime, starts_after).astimezone(UTC)
            starts_before = cast(datetime, starts_before).astimezone(UTC)
        try:
            date_ranges = _normalize_catalog_date_ranges(
                date_range or [],
                starts_after=starts_after,
                starts_before=starts_before,
                source_keys=source_keys,
            )
        except (OverflowError, ValueError):
            raise HTTPException(status_code=422, detail="catalog date range is invalid") from None
        selection = _normalized_catalog_filters(
            q=q,
            city=city,
            location_scope=location_scope,
            topic=topic,
            price=price,
            price_max_cents=price_max_cents,
            price_min_cents=price_min_cents,
        )
        if selection.query is None or len(selection.query) < MIN_CATALOG_NAME_QUERY_LENGTH:
            raise HTTPException(status_code=422, detail="catalog name query is too short")
        rows = await app.state.container.catalog.suggest_names(
            query=selection.query, source_keys=source_keys, date_ranges=date_ranges,
            cities=selection.cities, location_scopes=selection.location_scopes,
            price=price, price_max_cents=price_max_cents, price_min_cents=price_min_cents,
            topics=selection.topics, availability=availability, limit=limit,
        )
        return [CatalogNameSuggestionOut(
            name=row.name, kinds=list(row.kinds), event_count=row.event_count,
        ) for row in rows]

    @app.get("/v1/catalog/events/summary", response_model=CatalogDaySummaryOut)
    async def summarize_catalog_events(
        time_zone: str = Query(max_length=_MAX_CATALOG_TIME_ZONE_LENGTH),
        source_key: Annotated[
            list[
                Annotated[
                    str,
                    Field(min_length=2, max_length=80, pattern=_CATALOG_SOURCE_KEY_PATTERN),
                ]
            ]
            | None,
            Query(),
        ] = None,
        starts_after: Annotated[datetime | None, Query()] = None,
        starts_before: Annotated[datetime | None, Query()] = None,
        date_range: Annotated[list[str] | None, Query()] = None,
        q: str | None = Query(default=None, max_length=_MAX_CATALOG_FILTER_LENGTH),
        city: Annotated[list[str] | None, Query()] = None,
        location_scope: Annotated[
            list[Literal["bay_area", "manhattan", "los_angeles_area"]] | None,
            Query(),
        ] = None,
        price: Literal["free", "paid", "unknown"] | None = Query(default=None),
        price_max_cents: int | None = Query(default=None, ge=1, le=100_000_000),
        price_min_cents: int | None = Query(default=None, ge=1, le=100_000_000),
        topic: Annotated[list[str] | None, Query()] = None,
        availability: Literal["available", "sold_out"] | None = Query(default=None),
    ) -> CatalogDaySummaryOut:
        """Count one calendar range into local days without paging its events.

        The consumer calendar grid renders per-day totals and topic chips only. Paging the range
        to derive them re-read every event to display a number, so this route answers the same
        filter with one aggregate. It accepts exactly the filters ``GET /v1/catalog/events``
        accepts, minus paging and ordering, and applies the topic selection so the grid agrees
        with the agenda those filters produce.
        """
        source_keys = tuple(dict.fromkeys(source_key or []))
        if len(source_keys) > _MAX_CATALOG_SOURCE_SELECTIONS:
            raise HTTPException(status_code=422, detail="too many catalog sources")
        if starts_after is not None or starts_before is not None:
            if (starts_after is None) != (starts_before is None):
                raise HTTPException(status_code=422, detail="both catalog date bounds are required")
            if (
                starts_after is not None
                and starts_before is not None
                and (
                    starts_after.tzinfo is None
                    or starts_after.utcoffset() is None
                    or starts_before.tzinfo is None
                    or starts_before.utcoffset() is None
                )
            ):
                raise HTTPException(status_code=422, detail="catalog dates require a timezone")
            starts_after = cast(datetime, starts_after).astimezone(UTC)
            starts_before = cast(datetime, starts_before).astimezone(UTC)
        try:
            date_ranges = _normalize_catalog_date_ranges(
                date_range or [],
                starts_after=starts_after,
                starts_before=starts_before,
                source_keys=source_keys,
            )
        except (OverflowError, ValueError):
            raise HTTPException(status_code=422, detail="catalog date range is invalid") from None
        selection = _normalized_catalog_filters(
            q=q,
            city=city,
            location_scope=location_scope,
            topic=topic,
            price=price,
            price_max_cents=price_max_cents,
            price_min_cents=price_min_cents,
        )
        try:
            days = await app.state.container.catalog.list_day_facets(
                source_keys=source_keys,
                starts_after=None,
                starts_before=None,
                date_ranges=date_ranges,
                query=selection.query,
                cities=selection.cities,
                location_scopes=selection.location_scopes,
                price=price,
                price_max_cents=price_max_cents,
                price_min_cents=price_min_cents,
                topics=selection.topics,
                availability=availability,
                time_zone=time_zone,
            )
        except ValueError:
            raise HTTPException(
                status_code=422, detail="catalog summary query is invalid"
            ) from None
        return CatalogDaySummaryOut(
            days=[
                CatalogDayOut(
                    start_day=day.start_day,
                    event_count=day.event_count,
                    topics=[
                        CatalogDayTopicOut(
                            topic=day_topic.topic,
                            label=day_topic.label,
                            event_count=day_topic.event_count,
                        )
                        for day_topic in day.topics
                    ],
                )
                for day in days
            ],
            total_event_count=sum(day.event_count for day in days),
            time_zone=time_zone,
        )

    @app.get("/v1/catalog/events/{canonical_event_id}", response_model=CatalogBrowseItemOut)
    async def read_catalog_event(
        canonical_event_id: UUID,
    ) -> CatalogBrowseItemOut:
        """Read selected event details from the published catalog; never contact providers."""
        item = await app.state.container.catalog.get_browse_event(canonical_event_id)
        if item is None:
            raise HTTPException(status_code=404, detail="event is not in the published catalog")
        return _catalog_browse_item_out(item)

    @app.get("/v1/catalog/entities", response_model=list[CatalogEntityOut])
    async def browse_catalog_entities(
        q: str | None = Query(default=None, max_length=_MAX_CATALOG_FILTER_LENGTH),
        kind: Annotated[list[Literal["person", "organization", "unknown"]] | None, Query()] = None,
        limit: int = Query(default=80, ge=1, le=100),
    ) -> list[CatalogEntityOut]:
        """Browse the indexed role graph without ever merging identities by display name."""
        query = q.strip() if q and q.strip() else None
        if query is not None and any(
            ord(character) < _MIN_PRINTABLE_CODEPOINT for character in query
        ):
            raise HTTPException(status_code=422, detail="entity query is invalid")
        entities = await app.state.container.catalog_entities.list(
            query=query,
            kinds=tuple(dict.fromkeys(kind or [])),
            limit=limit,
        )
        return [_catalog_entity_out(entity) for entity in entities]

    @app.get("/v1/catalog/entity-resolution", response_model=CatalogEntityResolutionOut)
    async def resolve_catalog_event_entity(
        canonical_event_id: UUID,
        role: Literal["host", "organizer", "speaker", "partner"],
        name: str = Query(min_length=1, max_length=160),
    ) -> CatalogEntityResolutionOut:
        """Resolve only an entity name already attached to this exact public event and role."""
        entity_id = await app.state.container.catalog_entities.resolve(
            canonical_event_id, role, name
        )
        if entity_id is None:
            raise HTTPException(status_code=404, detail="event entity not found")
        return CatalogEntityResolutionOut(entity_id=entity_id)

    @app.get("/v1/catalog/entities/{entity_id}", response_model=CatalogEntityDetailOut)
    async def get_catalog_entity(
        entity_id: UUID,
    ) -> CatalogEntityDetailOut:
        detail = await app.state.container.catalog_entities.get(entity_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="entity not found")
        return _catalog_entity_detail_out(detail)

    @app.post(
        "/v1/catalog/entities/{entity_id}/refresh",
        response_model=CatalogEntityDetailOut,
    )
    async def refresh_catalog_entity(
        entity_id: UUID,
        tenant_id: CsrfProtectedTenant,
    ) -> CatalogEntityDetailOut:
        """Refresh bounded public facts from exact source/profile URLs only."""
        del tenant_id
        detail = await app.state.container.entity_intelligence.refresh(entity_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="entity not found")
        return _catalog_entity_detail_out(detail)

    @app.get(
        "/v1/catalog/entities/{entity_id}/graph",
        response_model=CatalogEntityGraphOut,
    )
    async def get_catalog_entity_graph(
        entity_id: UUID,
        events: int = Query(default=18, ge=1, le=24),
        peers: int = Query(default=32, ge=1, le=48),
        topics: int = Query(default=4, ge=0, le=6),
    ) -> CatalogEntityGraphOut:
        """Return one bounded ego bundle in a single round trip.

        Every edge is a recorded mention joining the ego to an event, so a peer is only ever
        reached *through* the evidence that names them both.  Deliberately not routed through the
        per-entity detail read, which issues five statements per entity against a pool configured
        ``pool_size=5, max_overflow=0``.
        """
        graph = await app.state.container.catalog_entities.graph(
            entity_id,
            event_limit=events,
            peer_limit=peers,
            topic_limit=topics,
        )
        if graph is None:
            raise HTTPException(status_code=404, detail="entity not found")
        return _catalog_entity_graph_out(graph)

    @app.get("/v1/catalog/entity-directory", response_model=CatalogEntityDirectoryOut)
    async def get_catalog_entity_directory(
        q: str | None = Query(default=None, max_length=_MAX_CATALOG_FILTER_LENGTH),
        kind: Annotated[list[Literal["person", "organization", "unknown"]] | None, Query()] = None,
        city: Annotated[list[str] | None, Query()] = None,
        limit: int = Query(default=48, ge=1, le=60),
        min_events: int = Query(default=1, ge=1, le=10),
    ) -> CatalogEntityDirectoryOut:
        """Rank the entities that actually connect the catalog, with their coverage caveat.

        The coverage figures travel with the ranking they qualify so the disclosure cannot drift
        away from it: only 4% of catalogued events name anyone at all.
        """
        query = q.strip() if q and q.strip() else None
        try:
            directory = await app.state.container.catalog_entities.directory(
                query=query,
                kinds=tuple(dict.fromkeys(kind or [])),
                city_norms=tuple(dict.fromkeys(city or [])),
                limit=limit,
                min_events=min_events,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return _catalog_entity_directory_out(directory)

    @app.get("/v1/catalog/entity-overview-graph", response_model=CatalogEntityGraphOut)
    async def get_catalog_entity_overview_graph(
        q: str | None = Query(default=None, max_length=_MAX_CATALOG_FILTER_LENGTH),
        kind: Annotated[list[Literal["person", "organization", "unknown"]] | None, Query()] = None,
        identity: Annotated[
            list[Literal["profile_verified", "source_scoped"]] | None, Query()
        ] = None,
        entities: int = Query(default=40, ge=1, le=60),
        pairs: int = Query(default=40, ge=1, le=60),
    ) -> CatalogEntityGraphOut:
        """Return the entities landing graph: the top hubs and how they interconnect.

        The same envelope the ego route returns, so one client parser serves both.  Ring 0 is the
        ranked hubs -- including any that share no event with another hub, which draw isolated
        rather than disappear -- and ring 1 is one representative event per connected pair, the
        most recent event that pair shares.  ``shared_event_count`` on each event node is the
        pair's true total, which the view must disclose: the node stands for more events than the
        one it names.

        ``identity`` reaches the server here, unlike on the directory where it filters an
        already-fetched page: applying it after the top-N cut would rank over a population the
        reader did not choose.
        """
        query = q.strip() if q and q.strip() else None
        try:
            graph = await app.state.container.catalog_entities.overview(
                query=query,
                kinds=tuple(dict.fromkeys(kind or [])),
                identity_statuses=tuple(dict.fromkeys(identity or [])),
                entity_limit=entities,
                pair_limit=pairs,
            )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return _catalog_entity_graph_out(graph)

    app.post("/v1/feed-feedback", status_code=202, response_model=FeedFeedbackAccepted)(
        _record_feed_feedback
    )

    @app.post("/v1/unrsvp", status_code=202, response_model=UnrsvpAccepted)
    async def unrsvp(body: UnrsvpBody, tenant_id: CsrfProtectedTenant) -> UnrsvpAccepted:
        """Signal the RLS-visible active lifecycle; never claim acceptance without Temporal durability.

        The workflow id comes from the lifecycle row rather than reconstructing a best-effort name,
        so a retry/attempt representation remains encapsulated at the durable boundary (FR-6.7/8.8,
        ADR-003/007).
        """
        container: Container = app.state.container
        lifecycle = await container.lifecycle_repo.find_active(tenant_id, body.canonical_event_id)
        if lifecycle is None:
            raise HTTPException(status_code=404, detail="no active lifecycle for this event")
        # ``registered`` is the source-confirmed but not-yet-calendar-scheduled window.  Accepting
        # a withdrawal there would let a queued signal race the remainder of the registration saga.
        # The caller can retry once the durable calendar stage has reached a post-booking state;
        # direct Temporal signals are independently gated by the child workflow (FR-8.8, ADR-003).
        if lifecycle.state not in {
            LifecycleState.SCHEDULED,
            LifecycleState.RECONCILED,
            LifecycleState.WITHDRAWING,
        }:
            raise HTTPException(
                status_code=409,
                detail="registration is not yet ready for withdrawal; retry shortly",
            )
        signaler: RegistrationLifecycleSignaler | None = app.state.lifecycle_signaler
        if signaler is None:
            raise HTTPException(
                status_code=503, detail="lifecycle engine unavailable; retry command"
            )
        try:
            await signaler.signal_unrsvp(lifecycle.workflow_id, str(body.request_id))
        except Exception as exc:
            _log.warning(
                "unrsvp_signal_failed",
                workflow_id=lifecycle.workflow_id,
                error=str(exc),
            )
            raise HTTPException(
                status_code=503, detail="lifecycle command was not accepted; retry"
            ) from exc
        return UnrsvpAccepted(
            status="accepted",
            request_id=body.request_id,
        )

    @app.post(
        "/v1/me/tasks/{task_id}/done",
        status_code=202,
        response_model=HandoffCompletionAccepted,
    )
    async def mark_authenticated_handoff_done(
        task_id: str,
        tenant_id: CsrfProtectedTenant,
    ) -> HandoffCompletionAccepted:
        """Signal one RLS-visible handoff without exposing its email capability to the UI."""
        if not 1 <= len(task_id) <= _MAX_TASK_ID_LENGTH or any(
            ord(char) < _MIN_PRINTABLE_CODEPOINT for char in task_id
        ):
            raise HTTPException(status_code=404, detail="handoff task not found")
        container: Container = app.state.container
        task = await container.handoff_repo.get(tenant_id, task_id)
        if task is None:
            raise HTTPException(status_code=404, detail="handoff task not found")
        if task.state is HandoffState.COMPLETED:
            return HandoffCompletionAccepted(status="already_accepted")
        if task.ttl_expires_at <= datetime.now(UTC) or task.state in {
            HandoffState.EXPIRED,
            HandoffState.CANCELLED,
        }:
            raise HTTPException(status_code=410, detail="handoff task is no longer active")
        if task.state not in {HandoffState.OPEN, HandoffState.NOTIFIED}:
            raise HTTPException(status_code=409, detail="handoff task cannot be completed")

        signaler: RegistrationLifecycleSignaler | None = app.state.lifecycle_signaler
        if signaler is None:
            raise HTTPException(
                status_code=503,
                detail="lifecycle engine unavailable; retry command",
            )
        completion_id = f"{task.workflow_id}:handoff-completion:{task.task_id}:1"
        try:
            await signaler.signal_handoff_completed(
                task.workflow_id,
                task.task_id,
                completion_id,
            )
        except Exception as exc:
            _log.warning(
                "authenticated_handoff_completion_signal_failed",
                workflow_id=task.workflow_id,
                task_id=task.task_id,
                error=str(exc),
            )
            raise HTTPException(
                status_code=503,
                detail="handoff completion was not accepted; retry",
            ) from exc
        return HandoffCompletionAccepted(status="accepted")

    app.get(
        "/v1/tasks/{token}/done",
        response_class=HTMLResponse,
        include_in_schema=False,
    )(_show_handoff_done)
    app.post(
        "/v1/tasks/{token}/done",
        status_code=202,
        response_model=HandoffCompletionAccepted,
    )(_mark_handoff_done)

    if settings.muse_enabled:
        app.include_router(muse_router(_authenticated_tenant, _csrf_protected_tenant))
    install_ingestion_admin_routes(app)
    install_command_investigation_routes(app)
    install_operator_operations_routes(app)
    install_operator_session_routes(app)
    install_model_usage_routes(app)
    _install_agent_chat(app, settings)
    apply_release_profile(
        app, settings.release_profile,
        catalog_name_suggestions_enabled=settings.catalog_name_suggestions_enabled,
    )
    return app


def _install_agent_chat(app: FastAPI, settings: Settings) -> None:
    """Mount ``POST /v1/chat`` when an agent runtime is configured.

    Off by default and imported lazily, so a deployment without an agent key neither carries the
    dependency nor exposes the route.
    """
    if settings.release_profile == "discovery" or not getattr(settings, "agent_enabled", False):
        return
    import os

    from ..adapters.agent_runtime.openrouter import OpenRouterAgentRuntime
    from ..adapters.postgres.model_usage import PostgresModelUsageStore
    from ..agent.routes import install_agent_routes
    from ..agent.toolset import ConciergeToolsetImpl

    api_key = os.environ.get("EC_OPENROUTER_API_KEY", "").strip()
    if not api_key:
        _log.warning("agent_chat_disabled", reason="EC_OPENROUTER_API_KEY is not set")
        return

    def build_toolset(tenant_id: UUID) -> ConciergeToolsetImpl:
        container: Container = app.state.container
        return ConciergeToolsetImpl(
            container.catalog,
            container.catalog_entities,
            container.saved_catalog_filters,
            tenant_id,
        )

    install_agent_routes(
        app,
        runtime=OpenRouterAgentRuntime(
            api_key=api_key,
            model=os.environ.get("EC_AGENT_MODEL", "deepseek/deepseek-v4-flash"),
            fallback_model=os.environ.get("EC_AGENT_MODEL_FALLBACK") or None,
            usage_ledger=PostgresModelUsageStore(),
        ),
        toolset_factory=build_toolset,
        tenant_resolver=_authenticated_tenant,
    )
    _log.info("agent_chat_enabled", model=os.environ.get("EC_AGENT_MODEL", ""))


app = create_app()
