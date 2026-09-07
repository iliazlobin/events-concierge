"""Conservative, provider-neutral event semantics extracted from public event facts.

The rules in this module intentionally recognize a small product taxonomy.  They do not infer
demographics, political/religious identity, health status, or other sensitive traits.  A topic is
emitted only for an explicit provider label or a bounded title/description phrase.  The same
projection is safe to recompute: it has no clock, network, model, or tenant dependency.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from .enums import PriceStatus

type ExtractionSource = Literal["provider_metadata", "title", "description"]

TOPIC_LABELS: dict[str, str] = {
    "ai": "AI",
    "arts": "Arts",
    "board-games": "Board games",
    "chess": "Chess",
    "community": "Community",
    "education": "Education",
    "family": "Family",
    "food-drink": "Food & drink",
    "founders": "Founders",
    "gaming": "Gaming",
    "government": "Government",
    "music": "Music",
    "networking": "Networking",
    "outdoors": "Outdoors",
    "sports": "Sports",
    "technology": "Technology",
    "volleyball": "Volleyball",
    "wellness": "Wellness",
    "workshop": "Workshop",
}
CATALOG_TOPICS = frozenset(TOPIC_LABELS)
MAX_CATALOG_TOPIC_SELECTIONS = 12
MAX_EXTRACTION_EVIDENCE = 64


@dataclass(frozen=True, slots=True)
class ExtractionEvidence:
    """One non-sensitive fact produced by a named deterministic rule."""

    field: Literal["topic", "price_status"]
    value: str
    source: ExtractionSource
    rule: str

    def as_payload(self) -> dict[str, str]:
        return {
            "field": self.field,
            "value": self.value,
            "source": self.source,
            "rule": self.rule,
        }


@dataclass(frozen=True, slots=True)
class EventSemanticProjection:
    topics: tuple[str, ...]
    inferred_price_status: PriceStatus | None
    evidence: tuple[ExtractionEvidence, ...]


@dataclass(frozen=True, slots=True)
class _TopicRule:
    topic: str
    pattern: re.Pattern[str]
    aliases: frozenset[str] = frozenset()


def _words(*values: str) -> re.Pattern[str]:
    alternatives = "|".join(values)
    return re.compile(rf"(?<![a-z0-9])(?:{alternatives})(?![a-z0-9])", re.IGNORECASE)


_TOPIC_RULES: tuple[_TopicRule, ...] = (
    _TopicRule(
        "chess",
        _words("chess", "bughouse"),
        frozenset({"chess", "bughouse", "chess clubs"}),
    ),
    _TopicRule(
        "board-games",
        _words(
            "board[ -]?games?", "tabletop[ -]?games?", "chess", "bughouse",
            "mah[ -]?jongg?", "cribbage", "backgammon",
        ),
        frozenset(
            {
                "board game",
                "board games",
                "tabletop game",
                "tabletop games",
                "tabletop gaming",
                "chess",
                "bughouse",
                "mahjong",
                "mah jongg",
                "cribbage",
                "backgammon",
            }
        ),
    ),
    _TopicRule("volleyball", _words("volleyball", "beach volleyball"), frozenset({"volleyball"})),
    _TopicRule(
        "sports",
        _words(
            "sports?", "basketball", "baseball", "softball", "soccer", "football",
            "tennis", "pickleball", "golf", "running", "run club", "cycling", "hiking",
            "volleyball", "martial arts", "swimming",
        ),
        frozenset({"sport", "sports", "recreation"}),
    ),
    _TopicRule(
        "outdoors",
        _words("outdoors?", "hiking", "nature walk", "trail walk", "birding", "gardening"),
        frozenset({"outdoor", "outdoors", "nature", "parks"}),
    ),
    _TopicRule(
        "networking",
        _words("networking", "networking mixer", "meet[ -]?up", "social mixer"),
        frozenset({"networking", "meetup", "mixer"}),
    ),
    _TopicRule(
        "founders",
        _words("founders?", "co[ -]?founders?", "entrepreneurs?", "startups?"),
        frozenset({"founders", "entrepreneurship", "startups"}),
    ),
    _TopicRule(
        "ai",
        _words(
            "ai", "artificial intelligence", "generative ai", "machine learning",
            "large language models?", "llms?", "deep learning",
        ),
        frozenset({"ai", "artificial intelligence", "machine learning"}),
    ),
    _TopicRule(
        "technology",
        _words("technology", "software", "hardware", "developers?", "coding", "programming"),
        frozenset({"technology", "tech", "software", "coding"}),
    ),
    _TopicRule(
        "gaming",
        _words(
            "gaming", "video games?", "indie games?", "fighting games?", "esports?",
            "e[ -]?sports?", "super smash bros(?:\\.)?", "smash bros(?:\\.)?",
            "smash melee", "super smash bros(?:\\.)? melee",
        ),
        frozenset({"gaming", "video games", "esports", "e-sports"}),
    ),
    _TopicRule(
        "workshop",
        _words("workshops?", "hands[ -]?on", "bootcamps?", "training session"),
        frozenset({"workshop", "workshops", "training"}),
    ),
    _TopicRule(
        "music",
        _words("music", "concert", "jazz", "orchestra", "choir", "songwriter", "dj set"),
        frozenset({"music", "concerts"}),
    ),
    _TopicRule(
        "arts",
        _words(
            "arts?", "gallery", "exhibition", "museum", "theatre", "theater", "dance",
            "film", "cinema", "poetry", "crafts?",
        ),
        frozenset({"art", "arts", "culture", "theater", "theatre"}),
    ),
    _TopicRule(
        "family",
        _words("famil(?:y|ies)", "kids?", "children", "child[ -]?friendly", "all ages"),
        frozenset({"family", "children", "kids"}),
    ),
    _TopicRule(
        "food-drink",
        _words("food", "cooking", "dinner", "brunch", "wine tasting", "coffee tasting"),
        frozenset({"food", "food & drink", "cooking"}),
    ),
    _TopicRule(
        "wellness",
        _words("wellness", "yoga", "meditation", "mindfulness", "fitness"),
        frozenset({"wellness", "fitness", "yoga"}),
    ),
    _TopicRule(
        "education",
        _words("class", "classes", "lecture", "seminar", "storytime", "tutoring"),
        frozenset({"education", "learning", "classes"}),
    ),
    _TopicRule(
        "government",
        _words("city council", "public meeting", "commission meeting", "board meeting"),
        frozenset({"government", "public meetings"}),
    ),
    _TopicRule(
        "community",
        _words("community", "volunteer", "neighborhood", "neighbourhood"),
        frozenset({"community", "volunteer"}),
    ),
)

_FREE_PRICE_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("free_admission", _words("free admission", "admission is free")),
    ("cost_free", re.compile(r"(?<![a-z0-9])cost\s*:\s*free(?:[!.]|\s|$)", re.IGNORECASE)),
    ("no_cost", _words("no cost", "free of charge")),
)
_EXPLICIT_TOPIC_KEYS = frozenset({"category", "categories", "tag", "tags", "topic", "topics", "keywords"})
_MAX_EXPLICIT_LABELS = 32
_MAX_EXPLICIT_LABEL_LENGTH = 80


def extract_event_semantics(
    title: str,
    description: str,
    explicit_metadata: Mapping[str, object] | None = None,
) -> EventSemanticProjection:
    """Project bounded topics and a narrowly evidenced free-price fallback.

    Explicit price remains outside this function and always wins.  The returned price is therefore
    a fallback that callers may use only when their structured provider value is ``unknown``.
    """
    evidence: list[ExtractionEvidence] = []
    topics: set[str] = set()
    labels = _explicit_labels(explicit_metadata)
    for rule in _TOPIC_RULES:
        if any(_normalize_label(label) in rule.aliases for label in labels):
            topics.add(rule.topic)
            evidence.append(ExtractionEvidence("topic", rule.topic, "provider_metadata", f"explicit:{rule.topic}"))
            continue
        if rule.pattern.search(title):
            topics.add(rule.topic)
            evidence.append(ExtractionEvidence("topic", rule.topic, "title", f"keyword:{rule.topic}"))
            continue
        if rule.pattern.search(description):
            topics.add(rule.topic)
            evidence.append(ExtractionEvidence("topic", rule.topic, "description", f"keyword:{rule.topic}"))

    inferred_price: PriceStatus | None = None
    for rule_name, pattern in _FREE_PRICE_RULES:
        source: ExtractionSource | None = None
        if pattern.search(title):
            source = "title"
        elif pattern.search(description):
            source = "description"
        if source is not None:
            inferred_price = PriceStatus.FREE
            evidence.append(ExtractionEvidence("price_status", "free", source, rule_name))
            break

    return EventSemanticProjection(
        topics=tuple(topic for topic in TOPIC_LABELS if topic in topics),
        inferred_price_status=inferred_price,
        evidence=tuple(evidence),
    )


def event_extraction_evidence_from_payload(value: object) -> tuple[ExtractionEvidence, ...]:
    if not isinstance(value, list) or len(value) > MAX_EXTRACTION_EVIDENCE:
        raise ValueError("event extraction evidence payload is invalid")
    evidence: list[ExtractionEvidence] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"field", "value", "source", "rule"}:
            raise ValueError("event extraction evidence payload is invalid")
        field, item_value, source, rule = (
            item.get("field"), item.get("value"), item.get("source"), item.get("rule")
        )
        if not all(isinstance(part, str) for part in (field, item_value, source, rule)):
            raise ValueError("event extraction evidence payload is invalid")
        if field not in {"topic", "price_status"} or source not in {
            "provider_metadata", "title", "description"
        }:
            raise ValueError("event extraction evidence payload is invalid")
        evidence.append(ExtractionEvidence(field, item_value, source, rule))  # type: ignore[arg-type]
    return tuple(evidence)


def extraction_evidence_payload(
    evidence: Iterable[ExtractionEvidence],
) -> list[dict[str, str]]:
    return [item.as_payload() for item in evidence]


def _explicit_labels(metadata: Mapping[str, object] | None) -> tuple[str, ...]:
    if not metadata:
        return ()
    values: list[str] = []
    for key, raw_value in metadata.items():
        if key.casefold() not in _EXPLICIT_TOPIC_KEYS:
            continue
        candidates = raw_value if isinstance(raw_value, (list, tuple)) else (raw_value,)
        for candidate in candidates:
            if isinstance(candidate, str):
                for part in re.split(r"[,;|]", candidate):
                    label = " ".join(part.split())
                    if label and len(label) <= _MAX_EXPLICIT_LABEL_LENGTH and label not in values:
                        values.append(label)
                        if len(values) >= _MAX_EXPLICIT_LABELS:
                            return tuple(values)
    return tuple(values)


def _normalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9&]+", " ", value.casefold()).strip()
