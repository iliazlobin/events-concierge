"""Deterministic, non-sensitive event semantic extraction contracts."""

from __future__ import annotations

from events_concierge.domain.enums import PriceStatus
from events_concierge.domain.event_semantics import extract_event_semantics


def test_extracts_specific_and_parent_topics_with_named_evidence() -> None:
    projection = extract_event_semantics(
        "Beach Volleyball Skills Clinic",
        "An outdoor training session for recreational players.",
    )

    assert projection.topics == ("outdoors", "sports", "volleyball", "workshop")
    assert {(item.value, item.source) for item in projection.evidence} >= {
        ("sports", "title"),
        ("volleyball", "title"),
        ("outdoors", "description"),
        ("workshop", "description"),
    }


def test_video_gaming_stays_distinct_from_board_games() -> None:
    projection = extract_event_semantics(
        "Showcase Showdown",
        "Indie game showcase, fighting game tourney, and Smash Melee.",
    )
    board_games = extract_event_semantics("Board game night", "Bring a favorite game.")

    assert projection.topics == ("gaming",)
    assert board_games.topics == ("board-games",)


def test_chess_emits_specific_and_board_game_parent_topics() -> None:
    projection = extract_event_semantics(
        "Chess at Alamo Square Park",
        "Friendly regular chess and exciting Bughouse games. Bring a board.",
    )

    assert projection.topics == ("board-games", "chess")
    assert {(item.value, item.source) for item in projection.evidence} >= {
        ("board-games", "title"),
        ("chess", "title"),
    }


def test_explicit_board_game_metadata_is_provider_provenanced() -> None:
    projection = extract_event_semantics(
        "Weekly strategy gathering",
        "",
        {"keywords": ["Board Games", "Chess"]},
    )

    assert projection.topics == ("board-games", "chess")
    assert all(item.source == "provider_metadata" for item in projection.evidence)


def test_explicit_provider_keywords_are_bounded_and_provenanced() -> None:
    projection = extract_event_semantics(
        "Weekly gathering",
        "",
        {"keywords": "Sports, Volleyball; Community", "irrelevant": "AI"},
    )

    assert projection.topics == ("community", "sports", "volleyball")
    assert all(item.source == "provider_metadata" for item in projection.evidence)


def test_only_unambiguous_free_text_produces_price_fallback() -> None:
    for text in ("COST: FREE!", "Free admission", "Admission is free", "No cost"):
        projection = extract_event_semantics("Public workshop", text)
        assert projection.inferred_price_status is PriceStatus.FREE
        assert any(item.field == "price_status" for item in projection.evidence)

    for text in ("Free parking", "Feel free to bring a friend", "A carefree afternoon"):
        assert extract_event_semantics("Community event", text).inferred_price_status is None


def test_does_not_infer_registration_or_sensitive_traits() -> None:
    projection = extract_event_semantics(
        "Open registration",
        "A meetup for women, seniors, a faith community, and people with diabetes.",
    )

    assert projection.topics == ("community", "networking")
    assert {item.field for item in projection.evidence} <= {"topic", "price_status"}
