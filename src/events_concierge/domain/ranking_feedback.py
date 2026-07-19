"""Pure, bounded implicit-ranking feedback values (FR-2.1, FR-4.3, ADR-001).

The feedback ingress owns no raw client text, no client-selected weight, and no profile mutation.
It derives a small snapshot of normalized public event terms from a canonical event.  The current
P17 baseline deliberately uses fixed server-side signal polarity and a bounded aggregate; decay,
retention, learned taxonomies, and exposure-proof semantics remain product-policy work rather than
being silently encoded in an adapter.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from math import isfinite
from numbers import Real
from types import MappingProxyType
from uuid import UUID

from .events import CanonicalEvent

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_MAX_FEATURES_PER_SIGNAL = 24
_MAX_FEATURE_LABEL_LENGTH = 64
_MAX_IMPLICIT_AFFINITY_ABS = 5.0


class FeedbackSignalKind(StrEnum):
    """The fixed inbound interaction vocabulary for the P17 feedback baseline."""

    SCROLL = "scroll"
    DWELL = "dwell"
    CLICK = "click"
    DISMISS = "dismiss"


_SIGNAL_TOTAL_DELTAS: Mapping[FeedbackSignalKind, float] = MappingProxyType(
    {
        FeedbackSignalKind.SCROLL: 0.25,
        FeedbackSignalKind.DWELL: 0.75,
        FeedbackSignalKind.CLICK: 1.0,
        FeedbackSignalKind.DISMISS: -1.0,
    }
)


@dataclass(frozen=True, slots=True)
class RankingFeedbackSignal:
    """A caller-deduplicated, tenant-bound interaction with a canonical event (NFR-8).

    ``signal_id`` is an opaque idempotency key, not an ordering or score input.  The authenticated
    tenant is intentionally carried by the application boundary, never by this public value.
    """

    signal_id: UUID
    canonical_event_id: UUID
    kind: FeedbackSignalKind

    def __post_init__(self) -> None:
        """Reject ambiguous values before any durable receipt is attempted."""
        if not isinstance(self.signal_id, UUID):
            raise TypeError("ranking feedback signal_id must be a UUID")
        if not isinstance(self.canonical_event_id, UUID):
            raise TypeError("ranking feedback canonical_event_id must be a UUID")
        if not isinstance(self.kind, FeedbackSignalKind):
            raise TypeError("ranking feedback kind must be a FeedbackSignalKind")


@dataclass(frozen=True, slots=True)
class RankingFeedbackReceipt:
    """Immutable, derived feature deltas persisted for one feedback signal (FR-4.3).

    The receipt deliberately retains neither title/description nor a client-provided duration or
    weight. A repository persists this map as the first-write snapshot; an idempotency replay is
    instead identified by the caller-visible signal fields, so later catalog enrichment cannot
    turn a retry into a different preference effect.
    """

    signal: RankingFeedbackSignal
    feature_deltas: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Canonicalize bounded finite feature deltas for durable equality checks."""
        if not isinstance(self.signal, RankingFeedbackSignal):
            raise TypeError("ranking feedback receipt requires a RankingFeedbackSignal")
        object.__setattr__(
            self,
            "feature_deltas",
            normalize_feedback_feature_deltas(self.feature_deltas),
        )


def ranking_event_document(event: CanonicalEvent) -> str:
    """Build the public-event document shared by feedback extraction and the ranker."""
    return " ".join(
        part
        for part in (
            event.title,
            event.description,
            event.venue_name or "",
            event.city_norm or "",
        )
        if part
    )


def ranking_tokens(text: str) -> tuple[str, ...]:
    """Return bounded normalized lexical terms used by affinity matching.

    Oversized tokens are excluded instead of truncated so a public event cannot manufacture a
    label that the client later cannot represent or reason about consistently.
    """
    if not isinstance(text, str):
        raise TypeError("ranking text must be a string")
    return tuple(
        token
        for token in _TOKEN_RE.findall(text.lower())
        if len(token) <= _MAX_FEATURE_LABEL_LENGTH
    )


def feedback_feature_deltas(
    event: CanonicalEvent, kind: FeedbackSignalKind
) -> Mapping[str, float]:
    """Derive one bounded server-controlled feature snapshot from a canonical event.

    The fixed total delta is divided across first-seen terms, so one interaction cannot add more
    than its assigned absolute influence merely because an event description is verbose.  This is
    an explicit P17 implementation baseline, not a learned taste taxonomy or decay policy.
    """
    if not isinstance(event, CanonicalEvent):
        raise TypeError("ranking feedback requires a CanonicalEvent")
    if not isinstance(kind, FeedbackSignalKind):
        raise TypeError("ranking feedback kind must be a FeedbackSignalKind")

    features: list[str] = []
    seen: set[str] = set()
    for token in ranking_tokens(ranking_event_document(event)):
        if token in seen:
            continue
        seen.add(token)
        features.append(token)
        if len(features) == _MAX_FEATURES_PER_SIGNAL:
            break
    if not features:
        return MappingProxyType({})

    per_feature_delta = _SIGNAL_TOTAL_DELTAS[kind] / len(features)
    return MappingProxyType(dict.fromkeys(features, per_feature_delta))


def normalize_feedback_feature_deltas(affinities: Mapping[str, float]) -> Mapping[str, float]:
    """Validate the database-safe bounded feature map shared by all feedback adapters."""
    if not isinstance(affinities, Mapping):
        raise TypeError("ranking feedback feature_deltas must be a mapping")
    if len(affinities) > _MAX_FEATURES_PER_SIGNAL:
        raise ValueError("ranking feedback has too many feature deltas")

    normalized: dict[str, float] = {}
    for raw_label, raw_delta in affinities.items():
        if not isinstance(raw_label, str):
            raise TypeError("ranking feedback labels must be strings")
        label = raw_label.strip().lower()
        if not label or len(label) > _MAX_FEATURE_LABEL_LENGTH or not label.isascii():
            raise ValueError("ranking feedback labels must be bounded ASCII tokens")
        if _TOKEN_RE.fullmatch(label) is None:
            raise ValueError("ranking feedback labels must be normalized tokens")
        if isinstance(raw_delta, bool) or not isinstance(raw_delta, Real):
            raise TypeError("ranking feedback deltas must be non-boolean real numbers")
        delta = float(raw_delta)
        if not isfinite(delta):
            raise ValueError("ranking feedback deltas must be finite")
        if delta == 0.0:
            raise ValueError("ranking feedback deltas must not be zero")
        if abs(delta) > 1.0:
            raise ValueError("ranking feedback deltas must not exceed one per feature")
        if label in normalized:
            raise ValueError(f"ranking feedback contains duplicate label {label!r}")
        normalized[label] = delta
    return MappingProxyType(normalized)


def bound_implicit_affinity(value: float) -> float:
    """Clamp a feedback-derived aggregate so repeated self-signals cannot grow without bound."""
    if not isfinite(value):
        raise ValueError("implicit affinity must be finite")
    return max(-_MAX_IMPLICIT_AFFINITY_ABS, min(_MAX_IMPLICIT_AFFINITY_ABS, value))


def implicit_affinity_bound() -> float:
    """Expose the shared aggregate ceiling to deterministic adapters and tests."""
    return _MAX_IMPLICIT_AFFINITY_ABS
