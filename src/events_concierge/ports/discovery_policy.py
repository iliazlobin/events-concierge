"""Fresh source-modality gate for discovery dispatch (FR-3.9/FR-10.1, AC-23).

Discovery is tenant-neutral for shared catalog refreshes, while the ADR-004 kill switch applies
only to registration/RSVP mutations.  This narrow port therefore reads source policy data without
borrowing the tenant-bound registration PDP or accidentally freezing harmless catalog reads.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.enums import Modality, Source
from ..domain.policy import PolicyDecision, SourcePolicy


class DiscoveryPolicyReader(Protocol):
    """Load one current source policy for a discovery dispatch decision."""

    async def read_source_policy(self, source: Source) -> SourcePolicy | None:
        """Return the current source policy, or `None` when no durable row exists."""
        ...


class DiscoveryPolicyGate(Protocol):
    """Decide whether one source-modality may make a discovery wire call."""

    async def evaluate_discovery(self, source: Source, modality: Modality) -> PolicyDecision:
        """Fail closed for an unavailable, unknown, quarantined, or disabled source policy."""
        ...
