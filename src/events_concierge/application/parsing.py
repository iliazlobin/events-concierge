"""Request parsing: natural-language text -> constraints + intent embedding.

A heuristic parser for the foundation (the production parser is a schema-constrained Claude call
behind the same interface). It extracts coarse category keywords, detects an explicit verified-free
request, and computes the intent embedding via the EmbeddingPort (FR-4.6)."""

from __future__ import annotations

from uuid import UUID

from ..domain.request import EventRequest, RequestConstraints
from ..ports.ranking import EmbeddingPort

_CATEGORY_KEYWORDS = {
    "music": ("music", "concert", "gig", "jazz", "band", "dj", "live"),
    "comedy": ("comedy", "standup", "stand-up", "improv"),
    "tech": ("tech", "hackathon", "startup", "ai", "developer", "coding"),
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


class HeuristicRequestParser:
    def __init__(self, embedding: EmbeddingPort) -> None:
        self._embedding = embedding

    async def parse(self, tenant_id: UUID, request_id: UUID, raw_text: str) -> EventRequest:
        lowered = raw_text.lower()
        categories = tuple(
            name for name, kws in _CATEGORY_KEYWORDS.items() if any(k in lowered for k in kws)
        )
        constraints = RequestConstraints(
            budget_free=any(phrase in lowered for phrase in _FREE_ONLY_PHRASES),
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
