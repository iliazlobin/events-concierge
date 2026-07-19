"""Discovery service: policy-gated fanout into the tenant-neutral catalog (FR-3.8/FR-3.9).

Every source dispatch reads a fresh per-source/per-modality policy decision immediately before its
wire call.  A policy outage, unknown capability, quarantine, or disabled modality makes zero source
requests; it cannot be converted into a default allow (FR-10.1, AC-23).
"""

from __future__ import annotations

from ..domain.enums import Modality, Source
from ..domain.events import CandidateEvent, CanonicalEvent
from ..domain.request import RequestConstraints
from ..infra.logging import get_logger
from ..ports.discovery_policy import DiscoveryPolicyGate
from ..ports.policy import SourceQuarantinePort
from ..ports.repositories import CatalogRepository
from ..ports.sources import SourceAccessDeniedError, SourceCapability, SourcePort

_log = get_logger(__name__)


class DiscoveryService:
    def __init__(
        self,
        sources: list[SourcePort],
        catalog: CatalogRepository,
        policy_gate: DiscoveryPolicyGate,
        source_quarantine: SourceQuarantinePort | None = None,
    ) -> None:
        self._sources = list(sources)
        self._catalog = catalog
        self._policy_gate = policy_gate
        self._source_quarantine = source_quarantine

    async def discover(self, constraints: RequestConstraints) -> list[CanonicalEvent]:
        candidates: list[CandidateEvent] = []
        for source in self._sources:
            modality = _discovery_modality(source.capability)
            if modality is None:
                _log.warning(
                    "source discover skipped: no declared discovery capability",
                    source=source.capability.source.value,
                )
                continue
            try:
                decision = await self._policy_gate.evaluate_discovery(
                    source.capability.source, modality
                )
            except Exception:
                # A custom/injected gate must not turn a policy-plane implementation failure into
                # an external request. The durable gate itself already returns this denial shape.
                _log.warning(
                    "source discover skipped: policy gate unavailable",
                    source=source.capability.source.value,
                )
                continue
            if not decision.allowed:
                _log.info(
                    "source discover skipped: policy denied",
                    source=source.capability.source.value,
                    modality=modality.value,
                    reason=decision.reason,
                )
                continue
            try:
                found = await source.discover(constraints)
                candidates.extend(found)
            except SourceAccessDeniedError as denied:
                await self._quarantine_after_access_denied(source.capability.source, denied)
            except Exception as exc:  # one bad source never sinks discovery
                _log.warning(
                    "source discover failed", source=source.capability.source, error=str(exc)
                )
        canonical = await self._catalog.upsert_candidates(candidates)
        _log.info("discovery complete", candidates=len(candidates), canonical=len(canonical))
        return canonical

    async def _quarantine_after_access_denied(
        self, source: Source, denied: SourceAccessDeniedError
    ) -> None:
        """Trip a source's one-way circuit breaker without retaining provider response data."""
        actuator = self._source_quarantine
        if actuator is None:
            _log.error(
                "source_quarantine_actuator_unconfigured",
                source=source.value,
                signal=denied.signal.value,
            )
            return
        try:
            receipt = await actuator.quarantine(source, denied.signal)
        except Exception as error:
            _log.error(
                "source_quarantine_actuation_failed",
                source=source.value,
                signal=denied.signal.value,
                error_type=type(error).__name__,
            )
            return
        _log.warning(
            "source_quarantined",
            source=receipt.source.value,
            signal=receipt.signal.value,
            newly_quarantined=receipt.newly_quarantined,
        )


def _discovery_modality(capability: SourceCapability) -> Modality | None:
    """Resolve the selected discovery modality from its declared capabilities.

    API is deterministically preferred when both paths exist; browser is the ToS-reviewed fallback.
    A port with neither discovery capability is not dispatchable (FR-3.1/FR-3.9).
    """
    if capability.supports_api:
        return Modality.API
    if capability.supports_browser_discovery:
        return Modality.BROWSER
    return None
