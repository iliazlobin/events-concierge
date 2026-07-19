"""Request-price parsing tests for the FR-4.6 hard constraint."""

from __future__ import annotations

from uuid import uuid4

from events_concierge.adapters.ranking.embedding import DeterministicEmbedding
from events_concierge.application.parsing import HeuristicRequestParser


async def test_parser_includes_paid_and_unknown_events_by_default() -> None:
    """Broad discovery is the default when a request does not name a price constraint (FR-4.6)."""
    parser = HeuristicRequestParser(DeterministicEmbedding(dim=8))

    request = await parser.parse(uuid4(), uuid4(), "Find Bay Area AI events this week")

    assert request.constraints.budget_free is False


async def test_parser_recognizes_an_explicit_free_only_request() -> None:
    """Free-only wording becomes a verified-free catalog filter, never an unknown-price guess."""
    parser = HeuristicRequestParser(DeterministicEmbedding(dim=8))

    request = await parser.parse(uuid4(), uuid4(), "Find free events only in San Francisco")

    assert request.constraints.budget_free is True
