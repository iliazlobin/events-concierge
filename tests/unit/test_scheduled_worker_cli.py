"""Deployment-scheduled maintenance workers expose bounded one-shot commands."""

from __future__ import annotations

from events_concierge.workers import handoff_expiry, lifecycle_invariants


def test_handoff_expiry_cli_defaults_to_daemon_and_accepts_one_shot() -> None:
    assert handoff_expiry._parse_args([]).once is False
    assert handoff_expiry._parse_args(["--once"]).once is True


def test_lifecycle_invariant_cli_defaults_to_daemon_and_accepts_one_shot() -> None:
    assert lifecycle_invariants._parse_args([]).once is False
    assert lifecycle_invariants._parse_args(["--once"]).once is True
