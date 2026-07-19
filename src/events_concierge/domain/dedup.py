"""Deterministic dedup building blocks: blocking key + a match rule combining fuzzy title,
time delta, geo delta, and (optionally) a precomputed description-embedding cosine (FR-3.8).

The embedding cosine is INJECTED (computed by a ranking/embedding adapter) so the domain stays pure."""

from __future__ import annotations

import re
from datetime import timedelta
from difflib import SequenceMatcher
from math import asin, cos, radians, sin, sqrt

from .events import CandidateEvent, CanonicalEvent, GeoPoint

_NON_ALNUM = re.compile(r"[^a-z0-9]+")

# Conservative thresholds -- a false merge corrupts calendar/workflow keys (ADR-001 open risk).
TITLE_SIM_THRESHOLD = 0.82
EMBED_COS_THRESHOLD = 0.86
TIME_DELTA = timedelta(hours=3)
GEO_DELTA_KM = 2.0


def normalize_title(title: str) -> str:
    return _NON_ALNUM.sub(" ", title.lower()).strip()


def normalize_city(city: str | None) -> str | None:
    if not city:
        return None
    return _NON_ALNUM.sub("", city.lower()) or None


def blocking_key(city_norm: str | None, day_iso: str) -> tuple[str, str]:
    """Block candidates by (city, calendar day) before pairwise scoring."""
    return (city_norm or "_", day_iso)


def title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def haversine_km(a: GeoPoint, b: GeoPoint) -> float:
    r = 6371.0
    d_lat, d_lon = radians(b.lat - a.lat), radians(b.lon - a.lon)
    h = sin(d_lat / 2) ** 2 + cos(radians(a.lat)) * cos(radians(b.lat)) * sin(d_lon / 2) ** 2
    return 2 * r * asin(sqrt(h))


def is_duplicate(
    candidate: CandidateEvent,
    existing: CanonicalEvent,
    embedding_cosine: float | None = None,
) -> bool:
    """True if the candidate is the same real-world event as the existing canonical.

    Requires a strong title OR embedding match AND a compatible time (and geo, when both known)."""
    if abs(candidate.start_at - existing.start_at) > TIME_DELTA:
        return False
    if candidate.geo and existing.geo and haversine_km(candidate.geo, existing.geo) > GEO_DELTA_KM:
        return False
    title_ok = title_similarity(candidate.title, existing.title) >= TITLE_SIM_THRESHOLD
    embed_ok = embedding_cosine is not None and embedding_cosine >= EMBED_COS_THRESHOLD
    return title_ok or embed_ok
