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


async def test_parser_recognizes_a_singular_free_event_noun_phrase() -> None:
    """A modifier between ``free`` and singular ``event`` keeps the price request explicit."""
    parser = HeuristicRequestParser(DeterministicEmbedding(dim=8))

    request = await parser.parse(
        uuid4(),
        uuid4(),
        "Find one free technology event in San Francisco",
    )

    assert request.constraints.budget_free is True
    assert request.constraints.categories == ("tech",)


async def test_parser_does_not_treat_availability_as_a_free_price_constraint() -> None:
    """A person's free time is not evidence that an event has verified-free admission."""
    parser = HeuristicRequestParser(DeterministicEmbedding(dim=8))

    available = await parser.parse(uuid4(), uuid4(), "I am free to attend an event")
    free_time = await parser.parse(uuid4(), uuid4(), "I have free time to attend an event")

    assert available.constraints.budget_free is False
    assert free_time.constraints.budget_free is False


async def test_parser_does_not_treat_a_hyphenated_dietary_free_as_free_admission() -> None:
    """A dietary ``*-free`` modifier is not evidence that admission itself is free."""
    parser = HeuristicRequestParser(DeterministicEmbedding(dim=8))

    request = await parser.parse(uuid4(), uuid4(), "Find a gluten-free food event")

    assert request.constraints.budget_free is False
