"""Mutable offline source-policy reader for discovery dispatch tests and demos."""

from __future__ import annotations

from ...domain.enums import Source
from ...domain.policy import SourcePolicy


class MockDiscoveryPolicyReader:
    """Return the current fixture source policy on every read, or model a store outage."""

    def __init__(self, source_policies: dict[Source, SourcePolicy] | None = None) -> None:
        self.source_policies = source_policies or {}
        self.unavailable = False

    async def read_source_policy(self, source: Source) -> SourcePolicy | None:
        """Read fresh mutable fixture policy; unavailability must be handled fail closed by the gate."""
        if self.unavailable:
            raise RuntimeError("fixture discovery policy store unavailable")
        return self.source_policies.get(source)
