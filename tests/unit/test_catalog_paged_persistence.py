"""Normalized paged-catalog staging contract regressions."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from events_concierge.adapters.postgres.catalog_paged_promotion import (
    _candidate_from_stage_row,
)
from events_concierge.adapters.postgres.catalog_sources import _staged_candidate_payload
from events_concierge.domain.catalog_sources import catalog_candidate_content_hash
from events_concierge.domain.enums import PriceStatus, RegistrationStatus, Source
from events_concierge.domain.events import CandidateEvent, EventEntityProfile


def test_staged_candidate_round_trip_preserves_every_hashed_public_field() -> None:
    """The raw-free stage must reconstruct the exact candidate whose hash it stores."""
    candidate = _non_utc_candidate()

    payload = _staged_candidate_payload(candidate)

    assert set(payload) == {
        "source",
        "source_event_id",
        "title",
        "start_at",
        "end_at",
        "registration_url",
        "venue_name",
        "city",
        "description",
        "price_status",
        "price_min_cents",
        "price_max_cents",
        "price_currency",
        "organizer_name",
        "host_names",
        "speaker_names",
        "partner_names",
        "entity_profiles",
        "attendance_count",
        "registration_status",
        "content_hash",
    }
    assert payload["content_hash"] == catalog_candidate_content_hash(candidate)
    assert "raw" not in payload

    row = _database_row(payload, candidate)
    restored = _candidate_from_stage_row(row)

    assert restored == replace(candidate, raw={})
    assert restored.start_at.isoformat() != candidate.start_at.isoformat()
    assert restored.end_at is not None
    assert candidate.end_at is not None
    assert restored.end_at.isoformat() != candidate.end_at.isoformat()
    assert catalog_candidate_content_hash(restored) == payload["content_hash"]


def test_offset_sensitive_precanonical_stage_hash_is_not_promotable() -> None:
    """Strict verification rejects retained rows written before UTC hash canonicalization."""
    candidate = _non_utc_candidate()
    payload = _staged_candidate_payload(candidate)
    payload["content_hash"] = (
        "8fa4cd30e811ba108725049b30749707d9a22bacddb2d998dbedb8a6b8ebed9e"
    )

    with pytest.raises(
        RuntimeError,
        match="content hash did not match its normalized row",
    ):
        _candidate_from_stage_row(_database_row(payload, candidate))


def test_empty_profiles_keep_the_pre_0128_stage_hash_shape() -> None:
    candidate = replace(_non_utc_candidate(), entity_profiles=())
    payload: dict[str, object] = {
        "source": candidate.source.value,
        "source_event_id": candidate.source_event_id,
        "title": candidate.title,
        "start_at": candidate.start_at.astimezone(UTC).isoformat(),
        "end_at": candidate.end_at.astimezone(UTC).isoformat()
        if candidate.end_at is not None
        else None,
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
        "price_min_cents": candidate.price_min_cents,
        "price_max_cents": candidate.price_max_cents,
        "price_currency": candidate.price_currency,
    }
    legacy_hash = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    assert catalog_candidate_content_hash(candidate) == legacy_hash


def _non_utc_candidate() -> CandidateEvent:
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id="paged-enrichment-42",
        title="Planning Commission",
        start_at=datetime(2026, 9, 3, 18, 30, tzinfo=ZoneInfo("America/Los_Angeles")),
        end_at=datetime(2026, 9, 3, 20, 0, tzinfo=ZoneInfo("America/Los_Angeles")),
        registration_url="https://example.test/events/42",
        venue_name="City Hall",
        city="Alameda",
        description="Public hearing",
        raw={"provider_only": "must not be staged"},
        price_status=PriceStatus.PAID,
        price_min_cents=1_500,
        price_max_cents=3_000,
        price_currency="usd",
        organizer_name="City Clerk",
        host_names=("Planning Department", "City Council"),
        speaker_names=("Alex Rivera",),
        partner_names=("Transit Commission",),
        entity_profiles=(
            EventEntityProfile(
                name="Alex Rivera",
                role="speaker",
                kind="person",
                profile_url="https://www.linkedin.com/in/alex-rivera?trk=public",
            ),
            EventEntityProfile(
                name="Transit Commission",
                role="partner",
                kind="organization",
                profile_url="https://transit.example.test/about#leadership",
            ),
        ),
        attendance_count=275,
        registration_status=RegistrationStatus.OPEN,
    )


def _database_row(
    payload: dict[str, object],
    candidate: CandidateEvent,
) -> Any:
    row = SimpleNamespace(**payload)
    row.start_at = candidate.start_at.astimezone(UTC)
    row.end_at = candidate.end_at.astimezone(UTC) if candidate.end_at is not None else None
    return row
