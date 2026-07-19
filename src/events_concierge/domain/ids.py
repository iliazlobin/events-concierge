"""Deterministic identifiers. The calendar id and workflow ids are minted from stable inputs so
the same real-world event collides at the primary key regardless of source or retry (FR-9.2, FR-8.1)."""

from __future__ import annotations

import base64
import hashlib
import re
from uuid import UUID, uuid5


def calendar_event_id(tenant_id: UUID, canonical_event_id: UUID) -> str:
    """Google-Calendar-legal id (base32hex, chars 0-9a-v) = hash(tenant + canonical_event_id).

    Computed identically on every code path so cross-source re-registration and same-source retry
    both resolve to one entry via insert-then-409-patch (FR-9.2, ADR-001)."""
    digest = hashlib.sha256(f"{tenant_id}:{canonical_event_id}".encode()).digest()
    encoded = base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()
    # Google requires 5..1024 chars from [a-v0-9]; the 52-char encoding satisfies it.
    return encoded


def registration_workflow_id(tenant_id: UUID, canonical_event_id: UUID) -> str:
    """Per-(user, event) child workflow id with reject-duplicate reuse (FR-8.1, ADR-003)."""
    return f"{tenant_id}:{canonical_event_id}"


def request_workflow_id(tenant_id: UUID, request_id: UUID) -> str:
    """Short-lived parent request workflow id (ADR-003)."""
    return f"req:{tenant_id}:{request_id}"


def catalog_refresh_workflow_id(source_key: str, run_key: str) -> str:
    """Return P15a's deterministic source/run workflow identity (NFR-8, ADR-003/005).

    ``run_key`` can be a 256-character scheduler/manual identity, so hash it rather than placing
    source-controlled punctuation or an oversized value into Temporal's workflow-id namespace.
    The durable catalog-run ledger remains the human-readable source of truth.
    """
    digest = hashlib.sha256(run_key.encode("utf-8")).hexdigest()
    return f"catalog:{source_key}:{digest}"


def catalog_paged_refresh_workflow_id(source_key: str, run_key: str) -> str:
    """Return the P15b-specific source/run workflow identity (NFR-8, ADR-003/005).

    A separate prefix makes a source-profile migration unable to collide with P15a's one-document
    execution history. Both identities retain the same stable ledger key as their human-readable
    source of truth.
    """
    digest = hashlib.sha256(run_key.encode("utf-8")).hexdigest()
    return f"catalog-paged:{source_key}:{digest}"


_WS = re.compile(r"\s+")
_REQUEST_ID_NAMESPACE = UUID("64f9aa44-14b3-55d7-b1bd-59f1a49f30c5")


def request_dedup_key(tenant_id: UUID, raw_text: str, time_bucket: str) -> str:
    """Intake dedup key so a re-delivered email/SMS mints the same request_id (ADR-003).

    time_bucket is a caller-supplied coarse stamp (e.g. an ISO hour) -- kept as an argument rather
    than read from the clock here so the function stays pure and deterministic."""
    normalized = _WS.sub(" ", raw_text.strip().lower())
    digest = hashlib.sha256(f"{tenant_id}|{normalized}|{time_bucket}".encode()).hexdigest()
    return digest


def intake_request_id(tenant_id: UUID, raw_text: str, time_bucket: str) -> UUID:
    """Derive one stable EventRequest UUID from the ADR-003 intake deduplication key.

    The namespace UUID makes the hash legal for the database's UUID primary key while retaining the
    exact `(tenant, normalized text, time bucket)` identity.  It is intentionally computed before
    parsing so a provider/API retry cannot mint a second parent workflow (FR-6.8, AC-48).
    """
    return uuid5(_REQUEST_ID_NAMESPACE, request_dedup_key(tenant_id, raw_text, time_bucket))
