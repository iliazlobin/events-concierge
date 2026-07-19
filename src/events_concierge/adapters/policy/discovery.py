"""Deterministic, fail-closed source-policy gate for discovery dispatch.

The gate deliberately evaluates only `automation_allowed` and quarantine state.  ADR-004's
global/per-tenant kill switches freeze registration/RSVP mutations, not shared read-only catalog
discovery.  Every evaluation reads its `DiscoveryPolicyReader` afresh so an operator flip takes
effect before the next dispatch without a deploy (FR-3.9/FR-10.1, AC-23).
"""

from __future__ import annotations

from ...domain.enums import Modality, Source
from ...domain.policy import PolicyDecision, PolicyDecisionCode, SourcePolicy
from ...ports.discovery_policy import DiscoveryPolicyReader


class StoreBackedDiscoveryPolicyGate:
    """Evaluate a fresh source-policy row and deny on every policy-plane failure."""

    def __init__(self, reader: DiscoveryPolicyReader) -> None:
        self._reader = reader

    async def evaluate_discovery(self, source: Source, modality: Modality) -> PolicyDecision:
        """Allow only an explicitly enabled, non-quarantined source modality (FR-3.9/FR-10.1)."""
        try:
            policy = await self._reader.read_source_policy(source)
        except Exception:
            return PolicyDecision.deny(
                "discovery policy store unavailable: fail closed",
                PolicyDecisionCode.POLICY_STORE_UNAVAILABLE,
            )
        return self._evaluate_source_policy(source, modality, policy)

    @staticmethod
    def _evaluate_source_policy(
        source: Source, modality: Modality, policy: SourcePolicy | None
    ) -> PolicyDecision:
        """Apply the source-only, deterministic policy order without a default allow."""
        if policy is None:
            return PolicyDecision.deny(
                f"unknown source '{source.value}': no policy entry",
                PolicyDecisionCode.UNKNOWN_SOURCE,
            )
        if policy.quarantined:
            return PolicyDecision.deny(
                f"source '{source.value}' quarantined",
                PolicyDecisionCode.SOURCE_QUARANTINED,
            )
        if not policy.allows(modality):
            return PolicyDecision.deny(
                f"automation not allowed for {source.value}/{modality.value}",
                PolicyDecisionCode.MODALITY_DISABLED,
            )
        return PolicyDecision.allow()
