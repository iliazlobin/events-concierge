"""Deterministic Temporal-liveness double for lifecycle integrity scans."""

from __future__ import annotations


class MockWorkflowLivenessInspector:
    """Map opaque workflow IDs to open/closed/uncertain outcomes without an engine connection."""

    def __init__(self, outcomes: dict[str, bool | Exception] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[str] = []

    async def is_open(self, workflow_id: str) -> bool:
        """Return the configured outcome; missing IDs model an authoritative closed execution."""
        self.calls.append(workflow_id)
        outcome = self.outcomes.get(workflow_id, False)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
