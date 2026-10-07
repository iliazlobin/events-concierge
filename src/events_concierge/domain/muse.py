"""User-selected signup batches; provider outcomes are reported by Muse."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_MUSE_EVENTS = 5
_MAX_URL_LENGTH = 2048
_SPACE = 32
_DELETE = 127
MUSE_HOSTS = frozenset(
    {"lu.ma", "www.lu.ma", "luma.com", "www.luma.com", "meetup.com", "www.meetup.com"}
)
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
    ):
        raise ValueError("a Luma or Meetup HTTPS event URL is required")
    return value


class MuseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


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
    status: SignupStatus = "queued"
    attempt_id: UUID | None = None
    outcome: SignupOutcome | None = None
    updated_at: datetime


class SignupBatch(MuseModel):
    batch_id: UUID
    request_id: UUID
    created_at: datetime
    items: list[SignupItem]


class MuseConnection(MuseModel):
    connected: bool
    expires_at: datetime | None = None


class MuseConflictError(ValueError):
    """The command conflicts with a durable selection or attempt."""


class MuseNotFoundError(LookupError):
    """No item exists inside this account."""
