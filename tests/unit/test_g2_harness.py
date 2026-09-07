"""Offline safety/correctness checks for the operator-run Meetup G2 field harness."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setitem(sys.modules, "requests", ModuleType("requests"))
    path = Path(__file__).parents[2] / "spikes" / "g2-meetup-rsvp" / "harness.py"
    spec = importlib.util.spec_from_file_location("g2_meetup_harness", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("MEETUP_REPORT_SALT", "s" * 32)
    monkeypatch.setattr(module, "_inter_call_delay", lambda: 0.0)
    return module


def test_generic_second_error_does_not_claim_idempotency(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter(
        (
            ({"data": {"event": {"going": 10, "selfRsvp": None}}}, {}, 200),
            ({"data": {"rsvp": {"ticket": {"id": "ticket-one"}}}}, {}, 200),
            (
                {
                    "data": {
                        "rsvp": {
                            "ticket": None,
                            "errors": [{"code": "GENERIC", "message": "private provider text"}],
                        }
                    }
                },
                {},
                200,
            ),
            ({"data": {"event": {"going": 11, "selfRsvp": {"id": "ticket-one"}}}}, {}, 200),
        )
    )
    monkeypatch.setattr(harness, "gql", lambda *_args, **_kwargs: next(responses))

    result: dict[str, Any] = harness.cmd_test_c(  # type: ignore[attr-defined]
        SimpleNamespace(event="provider-event", commit=True)
    )

    assert result["verdict"].startswith("INCONCLUSIVE")
    assert "private provider text" not in json.dumps(result)


def test_stable_identity_and_single_post_state_support_idempotency(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter(
        (
            ({"data": {"event": {"going": 20, "selfRsvp": None}}}, {}, 200),
            ({"data": {"rsvp": {"ticket": {"id": "same"}}}}, {}, 200),
            ({"data": {"rsvp": {"ticket": {"id": "same"}}}}, {}, 200),
            ({"data": {"event": {"going": 21, "selfRsvp": {"id": "same"}}}}, {}, 200),
        )
    )
    monkeypatch.setattr(harness, "gql", lambda *_args, **_kwargs: next(responses))

    result: dict[str, Any] = harness.cmd_test_c(  # type: ignore[attr-defined]
        SimpleNamespace(event="provider-event", commit=True)
    )

    assert result["same_ticket_identity"] is True
    assert result["verdict"].startswith("IDEMPOTENT:")
    assert '"same"' not in json.dumps(result)


def test_auto_join_error_report_drops_provider_message(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter(
        (
            (
                {
                    "data": {
                        "groupByUrlname": {"isMember": False, "joinMode": "OPEN"}
                    }
                },
                {},
                200,
            ),
            (
                {
                    "data": {
                        "rsvp": {
                            "errors": [
                                {"code": "MEMBERSHIP_REQUIRED", "message": "private details"}
                            ]
                        }
                    }
                },
                {},
                200,
            ),
            (
                {
                    "data": {
                        "groupByUrlname": {"isMember": False, "joinMode": "OPEN"}
                    }
                },
                {},
                200,
            ),
        )
    )
    monkeypatch.setattr(harness, "gql", lambda *_args, **_kwargs: next(responses))

    result: dict[str, Any] = harness.cmd_test_a(  # type: ignore[attr-defined]
        SimpleNamespace(group="provider-group", event="provider-event", commit=True)
    )

    encoded = json.dumps(result)
    assert result["mutation_error_codes"] == ["MEMBERSHIP_REQUIRED"]
    assert result["verdict"].startswith("INCONCLUSIVE")
    assert "private details" not in encoded
    assert "provider-group" not in encoded
    assert "provider-event" not in encoded


def test_auto_join_requires_successful_post_membership_read(
    harness: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    responses = iter(
        (
            (
                {
                    "data": {
                        "groupByUrlname": {"isMember": False, "joinMode": "OPEN"}
                    }
                },
                {},
                200,
            ),
            ({"data": {"rsvp": {"ticket": {"id": "private-ticket"}}}}, {}, 200),
            (
                {
                    "data": {
                        "groupByUrlname": {"isMember": True, "joinMode": "OPEN"}
                    }
                },
                {},
                200,
            ),
        )
    )
    monkeypatch.setattr(harness, "gql", lambda *_args, **_kwargs: next(responses))

    result: dict[str, Any] = harness.cmd_test_a(  # type: ignore[attr-defined]
        SimpleNamespace(group="provider-group", event="provider-event", commit=True)
    )

    assert result["verdict"].startswith("AUTO-JOIN CONFIRMED")
    assert "private-ticket" not in json.dumps(result)
