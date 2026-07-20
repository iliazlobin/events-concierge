"""Request parsing: natural-language text -> constraints + intent embedding.

A heuristic parser for the foundation (the production parser is a schema-constrained Claude call
behind the same interface). It extracts coarse category keywords, detects an explicit verified-free
request, and computes the intent embedding via the EmbeddingPort (FR-4.6)."""

from __future__ import annotations

import re
from uuid import UUID

from ..domain.request import EventRequest, RequestConstraints
from ..ports.ranking import EmbeddingPort

_CATEGORY_KEYWORDS = {
    "music": ("music", "concert", "gig", "jazz", "band", "dj", "live"),
    "comedy": ("comedy", "standup", "stand-up", "improv"),
    "tech": ("tech", "technology", "hackathon", "startup", "ai", "developer", "coding"),
    "art": ("art", "gallery", "exhibit", "museum", "opening"),
    "food": ("food", "dinner", "tasting", "supper", "brunch"),
    "social": ("meetup", "networking", "social", "mixer", "trivia"),
    "outdoors": ("hike", "run", "outdoor", "walk", "cycling"),
}

_FREE_ONLY_PHRASES = (
    "free only",
    "only free",
    "free events",
    "no cost",
    "no-cost",
    "without paying",
)

# A singular request such as ``one free technology event`` is just as explicit about price as
# ``free events``. Only known event-category modifiers may occur between the two words: a generic
# wildcard would misread phrases such as ``free time to attend an event`` as a price constraint.
_FREE_EVENT_MODIFIERS = tuple(
    sorted(
        set(_CATEGORY_KEYWORDS)
        | {keyword for keywords in _CATEGORY_KEYWORDS.values() for keyword in keywords},
        key=lambda value: (-len(value), value),
    )
)
_FREE_EVENT_NOUN_PHRASE = re.compile(
    rf"(?<!-)\bfree(?:\s+(?:{'|'.join(map(re.escape, _FREE_EVENT_MODIFIERS))}))*\s+events?\b"
)


class HeuristicRequestParser:
    def __init__(self, embedding: EmbeddingPort) -> None:
        self._embedding = embedding

    async def parse(self, tenant_id: UUID, request_id: UUID, raw_text: str) -> EventRequest:
        lowered = raw_text.lower()
        categories = tuple(
            name for name, kws in _CATEGORY_KEYWORDS.items() if any(k in lowered for k in kws)
        )
        constraints = RequestConstraints(
            budget_free=(
                any(phrase in lowered for phrase in _FREE_ONLY_PHRASES)
                or _FREE_EVENT_NOUN_PHRASE.search(lowered) is not None
            ),
            categories=categories,
        )
        intent = (await self._embedding.embed([raw_text]))[0]
        return EventRequest(
            request_id=request_id,
            tenant_id=tenant_id,
            raw_text=raw_text,
            constraints=constraints,
            intent_embedding=intent,
        )
