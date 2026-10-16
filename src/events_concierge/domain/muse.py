"""User-selected signup batches; provider outcomes are reported by Muse."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MUSE_EVENTS = 5
_MAX_URL_LENGTH = 2048
_SPACE = 32
_DELETE = 127
MUSE_HOSTS = frozenset(
    {
        "lu.ma", "www.lu.ma", "luma.com", "www.luma.com", "meetup.com", "www.meetup.com",
        "partiful.com", "www.partiful.com",
    }
)
_PARTIFUL_HOSTS = frozenset({"partiful.com", "www.partiful.com"})
_PARTIFUL_EVENT_PATH = re.compile(r"/e/[A-Za-z0-9_-]+/?")
type RegistrationExecutor = Literal["muse"]
type RegistrationProvider = Literal["luma", "meetup", "partiful"]
_EMAIL_LOCAL = re.compile(r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*")
_EMAIL_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
_EMAIL_TLD = re.compile(r"[A-Za-z]{2,63}")
_MAX_EMAIL_LENGTH = 254
_MAX_EMAIL_LOCAL_LENGTH = 64
_MIN_EMAIL_DOMAIN_LABELS = 2
type SignupStatus = Literal[
    "queued",
    "in_progress",
    "needs_input",
    "awaiting_approval",
    "waitlisted",
    "registered",
    "failed",
    "uncertain",
]


def registration_window_open(start_at: datetime, end_at: datetime | None, now: datetime) -> bool:
    """Future dates or ongoing dates with a confirmed, valid end remain open."""
    values = (start_at, now) if end_at is None else (start_at, end_at, now)
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        return False
    start_at = start_at.astimezone(UTC)
    end_at = end_at.astimezone(UTC) if end_at is not None else None
    now = now.astimezone(UTC)
    if end_at is not None and end_at <= start_at:
        return False
    return start_at > now or (end_at is not None and end_at > now)


def provider_url(value: str) -> str:
    """Only reviewed HTTPS event providers; credentials never belong in links."""
    parsed = urlsplit(value)
    if (
        len(value) > _MAX_URL_LENGTH
        or not value.isascii()
        or any(ord(c) <= _SPACE or ord(c) >= _DELETE or c == "\\" for c in value)
        or parsed.scheme != "https"
        or parsed.hostname not in MUSE_HOSTS
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or not parsed.path.strip("/")
        or parsed.fragment
        or (parsed.hostname in _PARTIFUL_HOSTS and _PARTIFUL_EVENT_PATH.fullmatch(parsed.path) is None)
    ):
        raise ValueError("a Luma, Meetup or Partiful HTTPS event URL is required")
    return value


class MuseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def registration_email(value: str) -> str:
    """A user-authored account selector, never a verified identity or access grant."""
    if not value.isascii() or any(ord(char) < _SPACE or ord(char) == _DELETE for char in value):
        raise ValueError("use an email address")
    value = value.strip(" ")
    local, separator, domain = value.rpartition("@")
    labels = domain.split(".")
    if (
        not separator
        or len(value) > _MAX_EMAIL_LENGTH
        or len(local) > _MAX_EMAIL_LOCAL_LENGTH
        or _EMAIL_LOCAL.fullmatch(local) is None
        or len(labels) < _MIN_EMAIL_DOMAIN_LABELS
        or any(_EMAIL_LABEL.fullmatch(label) is None for label in labels)
        or _EMAIL_TLD.fullmatch(labels[-1]) is None
    ):
        raise ValueError("use an email address")
    return local + "@" + domain.lower()


def _optional_email(value: str | None) -> str | None:
    return registration_email(value) if value is not None else None


def _email_overrides(value: dict[RegistrationProvider, str]) -> dict[RegistrationProvider, str]:
    return {provider: registration_email(email) for provider, email in value.items()}


class RegistrationAccountUpdate(MuseModel):
    expected_email: str | None
    provider_overrides: dict[RegistrationProvider, str]
    revision: int = Field(ge=1, strict=True)

    _email = field_validator("expected_email")(_optional_email)
    _overrides = field_validator("provider_overrides")(_email_overrides)


class RegistrationAccountSettings(MuseModel):
    executor: RegistrationExecutor = "muse"
    expected_email: str | None = None
    provider_overrides: dict[RegistrationProvider, str] = Field(default_factory=dict)
    revision: int = Field(default=0, ge=0, strict=True)
    updated_at: datetime | None = None

    _email = field_validator("expected_email")(_optional_email)
    _overrides = field_validator("provider_overrides")(_email_overrides)

    def for_event(self, event: SignupEvent) -> RegistrationAccount:
        host = urlsplit(event.registration_url).hostname
        provider: RegistrationProvider
        if host in _PARTIFUL_HOSTS:
            provider = "partiful"
        elif host in {"meetup.com", "www.meetup.com"}:
            provider = "meetup"
        else:
            provider = "luma"
        return RegistrationAccount(
            executor=self.executor,
            provider=provider,
            expected_email=self.provider_overrides.get(provider, self.expected_email),
        )


class RegistrationAccount(MuseModel):
    executor: RegistrationExecutor = "muse"
    provider: RegistrationProvider
    expected_email: str | None = Field(
        default=None,
        description=(
            "Selected signup/contact email and email-based provider login, including company/work email when "
            "accepted. If rejected or a business-domain address is required, ask the user; never "
            "invent or substitute an email. Null uses the current provider browser account and "
            "requires asking for unanswered email fields, never inferring the app login email."
            " Partiful uses phone login: this email does not select or verify a phone account."
        ),
    )

    _email = field_validator("expected_email")(_optional_email)


class SignupEvent(MuseModel):
    canonical_event_id: UUID
    title: str = Field(min_length=1, max_length=512)
    start_at: datetime
    end_at: datetime | None
    venue_name: str | None
    city: str | None
    registration_url: str
    price_status: Literal["free"] = "free"
    observed_at: datetime

    _url = field_validator("registration_url")(provider_url)


class SignupOutcome(MuseModel):
    status: Literal[
        "needs_input",
        "awaiting_approval",
        "waitlisted",
        "registered",
        "failed",
        "uncertain",
    ]
    note: str = Field(default="", max_length=500)
    confirmation_reference: str | None = Field(default=None, min_length=1, max_length=256)
    evidence_url: str | None = None

    @field_validator("note", "confirmation_reference")
    @classmethod
    def printable(cls, value: str | None) -> str | None:
        if value is not None and (
            any(ord(c) < _SPACE or ord(c) == _DELETE for c in value)
            or (value and not value.strip())
        ):
            raise ValueError("use a short plain-text result, without personal form answers")
        return value

    @field_validator("evidence_url")
    @classmethod
    def safe_evidence(cls, value: str | None) -> str | None:
        return provider_url(value) if value is not None else None

    @model_validator(mode="after")
    def confirmation_required(self) -> SignupOutcome:
        if self.status == "registered" and not (self.confirmation_reference or self.evidence_url):
            raise ValueError("registered requires provider confirmation evidence")
        return self


class SignupItem(MuseModel):
    event: SignupEvent
    registration_account: RegistrationAccount
    status: SignupStatus = "queued"
    attempt_id: UUID | None = None
    outcome: SignupOutcome | None = None
    updated_at: datetime

    @model_validator(mode="before")
    @classmethod
    def legacy_browser_account(cls, value: Any) -> Any:
        # Retained attempts have no recorded provider identity. Never infer the app login email.
        if isinstance(value, Mapping) and "event" in value and value.get("registration_account") is None:
            event = SignupEvent.model_validate(value["event"])
            return {**value, "registration_account": RegistrationAccountSettings().for_event(event)}
        return value


class SignupBatch(MuseModel):
    batch_id: UUID
    request_id: UUID
    created_at: datetime
    items: list[SignupItem]


class SignupRegistration(SignupItem):
    batch_id: UUID
    created_at: datetime
    version: int = Field(ge=1)
    unread: bool


class SignupStatusCounts(MuseModel):
    queued: int = Field(default=0, ge=0, strict=True)
    in_progress: int = Field(default=0, ge=0, strict=True)
    needs_input: int = Field(default=0, ge=0, strict=True)
    awaiting_approval: int = Field(default=0, ge=0, strict=True)
    waitlisted: int = Field(default=0, ge=0, strict=True)
    registered: int = Field(default=0, ge=0, strict=True)
    failed: int = Field(default=0, ge=0, strict=True)
    uncertain: int = Field(default=0, ge=0, strict=True)


class SignupRegistrations(MuseModel):
    items: list[SignupRegistration]
    total: int = Field(ge=0)
    unread_count: int = Field(ge=0)
    status_counts: SignupStatusCounts
    next_cursor: UUID | None = None

    @model_validator(mode="after")
    def consistent_totals(self) -> SignupRegistrations:
        if sum(self.status_counts.model_dump().values()) != self.total:
            raise ValueError("registration status counts must sum to the account total")
        if self.unread_count > self.total:
            raise ValueError("unread registrations cannot exceed the account total")
        return self


class SeenRegistration(MuseModel):
    event_id: UUID
    version: int = Field(ge=1)


class MuseConnection(MuseModel):
    connected: bool
    expires_at: datetime | None = None


class MuseConflictError(ValueError):
    """The command conflicts with a durable selection or attempt."""


class MuseNotFoundError(LookupError):
    """No item exists inside this account."""
