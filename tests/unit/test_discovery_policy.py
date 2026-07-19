"""Discovery dispatch policy tests (FR-3.9/FR-10.1, AC-23)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import uuid4

from events_concierge.adapters.mock.discovery_policy import MockDiscoveryPolicyReader
from events_concierge.adapters.mock.policy import MockSourceQuarantineRepository
from events_concierge.adapters.policy.discovery import StoreBackedDiscoveryPolicyGate
from events_concierge.application.discovery import DiscoveryService
from events_concierge.domain.enums import Modality, Source
from events_concierge.domain.events import CandidateEvent, CanonicalEvent
from events_concierge.domain.policy import (
    PolicyDecision,
    SourcePolicy,
    SourceQuarantineSignal,
)
from events_concierge.domain.request import RequestConstraints
from events_concierge.ports.discovery_policy import DiscoveryPolicyGate
from events_concierge.ports.repositories import CatalogRepository
from events_concierge.ports.sources import (
    SourceAccessDeniedError,
    SourceCapability,
    SourcePort,
)


class _FixtureSource:
    def __init__(
        self,
        *,
        source: Source = Source.PUBLIC_JSONLD,
        supports_api: bool = False,
        supports_browser_discovery: bool = True,
    ) -> None:
        self.capability = SourceCapability(
            source=source,
            supports_api=supports_api,
            supports_browser_discovery=supports_browser_discovery,
            supports_autonomous_register=False,
        )
        self.calls = 0
        self._candidate = CandidateEvent(
            source=source,
            source_event_id="policy-fixture",
            title="Policy fixture",
            start_at=datetime(2026, 7, 20, 17, tzinfo=UTC),
            registration_url="https://events.example.test/policy-fixture",
            is_free=True,
        )

    async def discover(self, constraints: RequestConstraints) -> list[CandidateEvent]:
        del constraints
        self.calls += 1
        return [self._candidate]


class _Catalog:
    def __init__(self) -> None:
        self.batches: list[list[CandidateEvent]] = []

    async def upsert_candidates(self, candidates: list[CandidateEvent]) -> list[CanonicalEvent]:
        self.batches.append(list(candidates))
        return [
            CanonicalEvent(
                canonical_event_id=uuid4(),
                title=candidate.title,
                start_at=candidate.start_at + timedelta(0),
            )
            for candidate in candidates
        ]


class _AccessDeniedSource(_FixtureSource):
    """Source fixture that emits only normalized ban evidence at its wire boundary."""

    async def discover(self, constraints: RequestConstraints) -> list[CandidateEvent]:
        del constraints
        self.calls += 1
        raise SourceAccessDeniedError(SourceQuarantineSignal.FORBIDDEN)


class _MutableGate:
    def __init__(self, decision: PolicyDecision) -> None:
        self.decision = decision
        self.calls: list[tuple[Source, Modality]] = []

    async def evaluate_discovery(self, source: Source, modality: Modality) -> PolicyDecision:
        self.calls.append((source, modality))
        return self.decision


async def test_disabled_policy_makes_zero_source_discovery_calls() -> None:
    """A disabled public/browser policy excludes that adapter before its wire call (AC-23)."""
    source = _FixtureSource()
    catalog = _Catalog()
    gate = _MutableGate(PolicyDecision.deny("automation not allowed for public_jsonld/browser"))
    service = DiscoveryService(
        [cast(SourcePort, source)],
        cast(CatalogRepository, catalog),
        cast(DiscoveryPolicyGate, gate),
    )

    result = await service.discover(RequestConstraints())

    assert result == []
    assert source.calls == 0
    assert catalog.batches == [[]]
    assert gate.calls == [(Source.PUBLIC_JSONLD, Modality.BROWSER)]


async def test_discovery_rechecks_the_current_policy_on_each_dispatch() -> None:
    """A policy flip between calls takes effect without recreating the service or deploying (AC-23)."""
    source = _FixtureSource()
    catalog = _Catalog()
    gate = _MutableGate(PolicyDecision.allow())
    service = DiscoveryService(
        [cast(SourcePort, source)],
        cast(CatalogRepository, catalog),
        cast(DiscoveryPolicyGate, gate),
    )

    allowed = await service.discover(RequestConstraints())
    gate.decision = PolicyDecision.deny("source 'public_jsonld' quarantined")
    denied = await service.discover(RequestConstraints())

    assert len(allowed) == 1
    assert denied == []
    assert source.calls == 1
    assert gate.calls == [
        (Source.PUBLIC_JSONLD, Modality.BROWSER),
        (Source.PUBLIC_JSONLD, Modality.BROWSER),
    ]


async def test_store_backed_gate_freshly_denies_disabled_quarantined_and_unavailable_policy() -> (
    None
):
    """The narrow source-policy gate has no cache/default-allow escape hatch (FR-10.1)."""
    policy = SourcePolicy(
        source=Source.PUBLIC_JSONLD,
        automation_allowed={Modality.BROWSER: True},
    )
    reader = MockDiscoveryPolicyReader({Source.PUBLIC_JSONLD: policy})
    gate = StoreBackedDiscoveryPolicyGate(reader)

    assert (await gate.evaluate_discovery(Source.PUBLIC_JSONLD, Modality.BROWSER)).allowed is True
    policy.automation_allowed[Modality.BROWSER] = False
    assert (await gate.evaluate_discovery(Source.PUBLIC_JSONLD, Modality.BROWSER)).allowed is False
    policy.automation_allowed[Modality.BROWSER] = True
    policy.quarantined = True
    assert (await gate.evaluate_discovery(Source.PUBLIC_JSONLD, Modality.BROWSER)).allowed is False
    reader.unavailable = True
    unavailable = await gate.evaluate_discovery(Source.PUBLIC_JSONLD, Modality.BROWSER)

    assert unavailable.allowed is False
    assert "unavailable" in unavailable.reason


async def test_source_with_both_modalities_deterministically_prefers_api() -> None:
    """API is selected before browser when both capability flags are enabled (FR-3.1/FR-3.9)."""
    source = _FixtureSource(supports_api=True, supports_browser_discovery=True)
    catalog = _Catalog()
    gate = _MutableGate(PolicyDecision.allow())
    service = DiscoveryService(
        [cast(SourcePort, source)],
        cast(CatalogRepository, catalog),
        cast(DiscoveryPolicyGate, gate),
    )

    result = await service.discover(RequestConstraints())

    assert len(result) == 1
    assert source.calls == 1
    assert gate.calls == [(Source.PUBLIC_JSONLD, Modality.API)]


async def test_access_denial_quarantines_source_and_fresh_retry_stops_before_provider() -> None:
    """A ban flips durable policy once; the next dispatch makes no second source call (AC-72)."""
    source_policies = {
        Source.PUBLIC_JSONLD: SourcePolicy(
            source=Source.PUBLIC_JSONLD,
            automation_allowed={Modality.BROWSER: True},
        )
    }
    source = _AccessDeniedSource()
    catalog = _Catalog()
    quarantine = MockSourceQuarantineRepository(source_policies)
    gate = StoreBackedDiscoveryPolicyGate(MockDiscoveryPolicyReader(source_policies))
    service = DiscoveryService(
        [cast(SourcePort, source)],
        cast(CatalogRepository, catalog),
        gate,
        quarantine,
    )

    first = await service.discover(RequestConstraints())
    second = await service.discover(RequestConstraints())

    assert first == []
    assert second == []
    assert source.calls == 1
    assert source_policies[Source.PUBLIC_JSONLD].quarantined is True
    assert quarantine.calls == [(Source.PUBLIC_JSONLD, SourceQuarantineSignal.FORBIDDEN)]
    assert catalog.batches == [[], []]
