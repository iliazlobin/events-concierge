"""Catalog-refresh service safety and idempotency tests (FR-3.1/FR-10.3/NFR-8)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest

from events_concierge.adapters.livewhale.source import LiveWhaleCatalogFetcher, LiveWhaleFetchError
from events_concierge.adapters.mock.discovery_policy import MockDiscoveryPolicyReader
from events_concierge.adapters.mock.policy import MockSourceQuarantineRepository
from events_concierge.adapters.policy.discovery import StoreBackedDiscoveryPolicyGate
from events_concierge.api.app import _parse
from events_concierge.application.catalog_refresh import (
    CatalogRefreshOutcome,
    CatalogRefreshService,
    catalog_refresh_lease_seconds,
)
from events_concierge.composition import Container
from events_concierge.domain.catalog_sources import (
    CatalogCollectionWindow,
    CatalogRefreshClaim,
    CatalogRefreshCommit,
    CatalogSource,
    CatalogSourceObservation,
)
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import (
    CatalogRefreshClaimOutcome,
    CatalogSourceMode,
    Modality,
    Source,
)
from events_concierge.domain.events import CandidateEvent, CanonicalEvent
from events_concierge.domain.policy import (
    PolicyDecision,
    SourcePolicy,
    SourceQuarantineSignal,
)
from events_concierge.domain.request import EventRequest, RequestConstraints
from events_concierge.ports.catalog_sources import CatalogCollectionWindowUnavailableError
from events_concierge.ports.policy import PacerLease, PacerLeaseStatus, PacerOperation, PacerRequest
from events_concierge.ports.sources import (
    SourceAccessDeniedError,
    SourceRateLimitedError,
    SourceTransientError,
)


class _MemorySourceRepository:
    def __init__(self, source: CatalogSource | None) -> None:
        self.source = source
        self.completed: set[tuple[str, str]] = set()
        self.claimed: dict[tuple[str, str], UUID] = {}
        self.failed: list[str] = []
        self.paused: list[str] = []
        self.claim_leases: list[int] = []
        self.live_lease_checks: list[tuple[str, str, UUID]] = []
        self.live_lease_allowed = True
        self.live_lease_error: Exception | None = None

    async def get(self, source_key: str) -> CatalogSource | None:
        if self.source is not None and self.source.source_key == source_key:
            return self.source
        return None

    async def claim_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_seconds: int,
    ) -> CatalogRefreshClaim:
        self.claim_leases.append(lease_seconds)
        key = (source_key, run_key)
        if key in self.completed:
            return CatalogRefreshClaim(CatalogRefreshClaimOutcome.SUCCEEDED)
        if key in self.claimed:
            return CatalogRefreshClaim(CatalogRefreshClaimOutcome.BUSY)
        token = uuid4()
        self.claimed[key] = token
        return CatalogRefreshClaim(CatalogRefreshClaimOutcome.ACQUIRED, token)

    async def has_live_refresh_lease(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
    ) -> bool:
        self.live_lease_checks.append((source_key, run_key, lease_token))
        if self.live_lease_error is not None:
            raise self.live_lease_error
        return self.live_lease_allowed and self.claimed.get((source_key, run_key)) == lease_token

    async def complete_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        candidate_count: int,
        canonical_count: int,
    ) -> bool:
        del candidate_count, canonical_count
        key = (source_key, run_key)
        if self.claimed.get(key) != lease_token:
            return False
        self.completed.add(key)
        return True

    async def fail_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        key = (source_key, run_key)
        if self.claimed.get(key) != lease_token:
            return False
        self.failed.append(error)
        self.claimed.pop(key)
        return True

    async def pause_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        key = (source_key, run_key)
        if self.claimed.get(key) != lease_token:
            return False
        self.paused.append(error)
        self.claimed.pop(key)
        return True


class _Catalog:
    def __init__(self) -> None:
        self.batches: list[list[CandidateEvent]] = []

    async def upsert_candidates(self, candidates: list[CandidateEvent]) -> list[CanonicalEvent]:
        self.batches.append(list(candidates))
        return [
            CanonicalEvent(
                canonical_event_id=uuid4(), title=candidate.title, start_at=candidate.start_at
            )
            for candidate in candidates
        ]


class _Observations:
    def __init__(self) -> None:
        self.records: list[tuple[list[CatalogSourceObservation], str]] = []

    async def record(self, observations: list[CatalogSourceObservation], run_key: str) -> None:
        self.records.append((list(observations), run_key))


class _Committer:
    """Offline atomic-commit double: a rejected completion makes no catalog fixture mutation."""

    def __init__(
        self,
        sources: _MemorySourceRepository,
        catalog: _Catalog,
        observations: _Observations,
        *,
        outcomes: list[CatalogRefreshCommit | None] | None = None,
    ) -> None:
        self._sources = sources
        self._catalog = catalog
        self._observations = observations
        self._outcomes = list(outcomes or [])
        self.calls: list[tuple[str, str, UUID, list[CandidateEvent]]] = []

    async def commit_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        candidates: list[CandidateEvent],
    ) -> CatalogRefreshCommit | None:
        self.calls.append((source_key, run_key, lease_token, list(candidates)))
        if self._outcomes:
            return self._outcomes.pop(0)
        canonical_events = await self._catalog.upsert_candidates(candidates)
        records = [
            CatalogSourceObservation(
                source_key=source_key,
                source=candidate.source,
                source_event_id=candidate.source_event_id,
                canonical_event_id=event.canonical_event_id,
                registration_url=candidate.registration_url,
                price_status=candidate.price_status,
                content_hash="fixture",
            )
            for candidate, event in zip(candidates, canonical_events, strict=True)
        ]
        await self._observations.record(records, run_key)
        completed = await self._sources.complete_refresh(
            source_key,
            run_key,
            lease_token=lease_token,
            candidate_count=len(candidates),
            canonical_count=len({event.canonical_event_id for event in canonical_events}),
        )
        if not completed:
            raise AssertionError("fixture committer unexpectedly lost its current lease")
        return CatalogRefreshCommit(
            len(candidates), len({event.canonical_event_id for event in canonical_events})
        )


class _Fetcher:
    def __init__(self, candidates: list[CandidateEvent], error: Exception | None = None) -> None:
        self._candidates = candidates
        self._error = error
        self.calls: list[CatalogSource] = []

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        self.calls.append(source)
        if self._error is not None:
            raise self._error
        return list(self._candidates)


class _FrozenWindowRepository:
    def __init__(self, window: CatalogCollectionWindow | None) -> None:
        self.window = window
        self.calls: list[tuple[str, str, UUID, int]] = []

    async def prepare_collection_window(
        self, source_key: str, run_key: str, *, lease_token: UUID, expected_revision: int,
    ) -> CatalogCollectionWindow | None:
        self.calls.append((source_key, run_key, lease_token, expected_revision))
        return self.window


class _RecordingPacer:
    def __init__(
        self,
        leases: list[PacerLease] | None = None,
        *,
        on_first_acquire: Callable[[], None] | None = None,
    ) -> None:
        self.keys: list[str] = []
        self.requests: list[PacerRequest] = []
        self.backoffs: list[tuple[str, float | None]] = []
        self._leases = list(leases or [])
        self._on_first_acquire = on_first_acquire

    async def acquire(self, request: PacerRequest) -> PacerLease:
        self.keys.append(request.bucket_key)
        self.requests.append(request)
        if self._on_first_acquire is not None:
            callback = self._on_first_acquire
            self._on_first_acquire = None
            callback()
        if self._leases:
            return self._leases.pop(0)
        return PacerLease(PacerLeaseStatus.GRANTED)

    async def observe_backoff(
        self,
        request: PacerRequest,
        *,
        retry_after_seconds: float | None = None,
        reset_at: datetime | None = None,
    ) -> None:
        del reset_at
        self.backoffs.append((request.bucket_key, retry_after_seconds))


class _MutableDiscoveryPolicyGate:
    """Fixture gate whose next policy read can change without recreating the service."""

    def __init__(self, decisions: list[PolicyDecision] | None = None) -> None:
        self._decisions = list(decisions or [])
        self.default = PolicyDecision.allow()
        self.calls: list[tuple[Source, Modality]] = []

    async def evaluate_discovery(self, source: Source, modality: Modality) -> PolicyDecision:
        self.calls.append((source, modality))
        if self._decisions:
            return self._decisions.pop(0)
        return self.default


def _source(
    now: datetime,
    *,
    enabled: bool = True,
    reviewed: bool = True,
    review_expires_at: datetime | None = None,
    handoff_only: bool = True,
) -> CatalogSource:
    return CatalogSource(
        source_key="approved-calendar",
        display_name="Approved Calendar",
        publisher="Test Publisher",
        seed_url="https://events.example.test/calendar",
        approved_origins=("https://events.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=enabled,
        reviewed_at=now if reviewed else None,
        review_expires_at=review_expires_at,
        refresh_interval_minutes=60,
        min_interval_ms=1_500,
        handoff_only=handoff_only,
    )


def _candidate(now: datetime) -> CandidateEvent:
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id="approved-event",
        title="Approved event",
        start_at=now + timedelta(days=1),
        registration_url="https://events.example.test/event",
        is_free=True,
    )


async def test_refresh_claims_once_paces_and_never_refetches_a_completed_run() -> None:
    """A repeat after a completed run key makes no second external request or catalog write (NFR-8)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer()
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    first = await service.refresh("approved-calendar", "manual:once")
    second = await service.refresh("approved-calendar", "manual:once")

    assert first.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert (first.candidate_count, first.canonical_count) == (1, 1)
    assert second.outcome is CatalogRefreshOutcome.ALREADY_SUCCEEDED
    assert len(fetcher.calls) == 1
    assert len(catalog.batches) == 1
    assert pacer.keys == ["public_jsonld:catalog:approved-calendar"]
    assert observations.records[0][1] == "manual:once"
    assert observations.records[0][0][0].source_key == "approved-calendar"
    assert repository.claim_leases == [300, 300]


async def test_refresh_reports_busy_without_a_non_atomic_fallback_when_commit_lease_is_lost() -> (
    None
):
    """A final lease rejection cannot make the generic service publish through older split writes (NFR-8)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    committer = _Committer(repository, catalog, observations, outcomes=[None])
    service = CatalogRefreshService(
        repository,
        committer,
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        _RecordingPacer(),
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh("approved-calendar", "manual:atomic-lease-lost")

    assert result.outcome is CatalogRefreshOutcome.BUSY
    assert (result.candidate_count, result.canonical_count) == (0, 0)
    assert len(fetcher.calls) == 1
    assert len(committer.calls) == 1
    assert catalog.batches == []
    assert observations.records == []
    assert repository.completed == set()
    assert repository.failed == []


async def test_refresh_does_not_fetch_after_its_lease_is_lost_while_waiting_for_pacer() -> None:
    """A post-Pacer database lease rejection fences generic source egress (FR-10.3/NFR-8)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    committer = _Committer(repository, catalog, observations)
    pacer = _RecordingPacer(
        on_first_acquire=lambda: setattr(repository, "live_lease_allowed", False)
    )
    service = CatalogRefreshService(
        repository,
        committer,
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh("approved-calendar", "manual:post-pacer-lease-lost")

    assert result.outcome is CatalogRefreshOutcome.BUSY
    assert len(pacer.requests) == 1
    assert [(source_key, run_key) for source_key, run_key, _ in repository.live_lease_checks] == [
        ("approved-calendar", "manual:post-pacer-lease-lost")
    ]
    assert fetcher.calls == []
    assert committer.calls == []
    assert catalog.batches == []
    assert observations.records == []
    assert repository.completed == set()
    assert repository.failed == []


async def test_refresh_releases_without_fetch_when_final_lease_authority_is_unavailable() -> None:
    """An unavailable final lease capability fails closed before generic source egress (NFR-8)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    repository.live_lease_error = RuntimeError("database unavailable")
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    committer = _Committer(repository, catalog, observations)
    service = CatalogRefreshService(
        repository,
        committer,
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        _RecordingPacer(),
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh("approved-calendar", "manual:lease-authority-unavailable")

    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert result.detail == "catalog refresh lease authority unavailable before fetch"
    assert len(repository.live_lease_checks) == 1
    assert fetcher.calls == []
    assert committer.calls == []
    assert catalog.batches == []
    assert observations.records == []
    assert repository.completed == set()
    assert repository.failed == ["catalog refresh lease authority unavailable before fetch"]


async def test_single_get_refresh_uses_the_exact_pacer_boundary_for_libcal_only() -> None:
    """P15a admits LibCal's one physical GET with its reviewed cadence and no page cursor (ADR-005)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = replace(
        _source(now),
        source_key="mountain-view-library-events",
        mode=CatalogSourceMode.LIBCAL_ICS,
        page_limit=1,
        min_interval_ms=1_500,
    )
    repository = _MemorySourceRepository(source)
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer()
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.LIBCAL_ICS: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh(
        source.source_key,
        "manual:libcal-single-get",
        require_single_http_get=True,
    )

    assert result.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert len(fetcher.calls) == 1
    assert len(pacer.requests) == 1
    request = pacer.requests[0]
    assert request.operation is PacerOperation.CATALOG_HTTP_GET
    assert request.queue_item_id == "manual:libcal-single-get:get:0"
    assert request.catalog_min_interval_ms == 1_500
    assert request.bucket_key == "public_jsonld:catalog:mountain-view-library-events"


async def test_direct_single_get_refresh_refuses_to_bypass_the_temporal_workflow() -> None:
    """A manual/service caller cannot issue LibCal's GET without P15a's durable retry path (ADR-003)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = replace(
        _source(now),
        source_key="mountain-view-library-events",
        mode=CatalogSourceMode.LIBCAL_ICS,
        page_limit=1,
    )
    repository = _MemorySourceRepository(source)
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer()
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.LIBCAL_ICS: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh(source.source_key, "manual:must-use-temporal")

    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert result.detail == "source requires the single-GET catalog workflow"
    assert repository.claim_leases == []
    assert pacer.requests == []
    assert fetcher.calls == []


@pytest.mark.parametrize(
    ("source_key", "display_name", "publisher", "seed_url", "mode"),
    [
        (
            "san-jose-legistar-meetings",
            "San Jose Public Meetings",
            "City of San Jose City Clerk",
            "https://webapi.legistar.com/v1/SanJose/Events",
            CatalogSourceMode.SAN_JOSE_LEGISTAR,
        ),
        (
            "sunnyvale-legistar-meetings",
            "Sunnyvale Public Meetings",
            "City of Sunnyvale City Clerk",
            "https://webapi.legistar.com/v1/SunnyvaleCA/Events",
            CatalogSourceMode.SUNNYVALE_LEGISTAR,
        ),
        (
            "alameda-legistar-meetings",
            "Alameda Public Meetings",
            "City of Alameda City Clerk",
            "https://webapi.legistar.com/v1/Alameda/Events",
            CatalogSourceMode.ALAMEDA_LEGISTAR,
        ),
        (
            "oakland-legistar-meetings",
            "Oakland Public Meetings",
            "City of Oakland City Clerk",
            "https://webapi.legistar.com/v1/Oakland/Events",
            CatalogSourceMode.OAKLAND_LEGISTAR,
        ),
    ],
)
async def test_direct_refresh_refuses_exact_paged_legistar_profiles_without_a_cursor(
    source_key: str,
    display_name: str,
    publisher: str,
    seed_url: str,
    mode: CatalogSourceMode,
) -> None:
    """The legacy service cannot restart a reviewed P15b/P15c/P15d/P15e page sequence at zero (NFR-8)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = CatalogSource(
        source_key=source_key,
        display_name=display_name,
        publisher=publisher,
        seed_url=seed_url,
        approved_origins=("https://webapi.legistar.com",),
        region="bay_area_9_county",
        mode=mode,
        enabled=True,
        reviewed_at=now,
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=5,
    )
    repository = _MemorySourceRepository(source)
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer()
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {mode: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh(source.source_key, "manual:must-use-paged-temporal")

    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert result.detail == "source requires the paged catalog workflow"
    assert repository.claim_leases == []
    assert pacer.requests == []
    assert fetcher.calls == []


async def test_single_get_contract_change_after_pacer_admission_releases_without_a_get() -> None:
    """A changed LibCal interval/end-point contract gets a new Pacer token before egress (ADR-005)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = replace(
        _source(now),
        source_key="mountain-view-library-events",
        mode=CatalogSourceMode.LIBCAL_ICS,
        page_limit=1,
        min_interval_ms=1_500,
    )
    repository = _MemorySourceRepository(source)
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer(
        on_first_acquire=lambda: setattr(
            repository, "source", replace(source, min_interval_ms=5_000)
        )
    )
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.LIBCAL_ICS: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh(
        source.source_key,
        "manual:libcal-contract-race",
        require_single_http_get=True,
    )

    assert result.outcome is CatalogRefreshOutcome.DEFERRED
    assert result.retry_after_seconds == 1.0
    assert fetcher.calls == []
    assert catalog.batches == []
    assert repository.failed == ["single-GET catalog request contract changed while awaiting Pacer"]
    assert pacer.requests[0].catalog_min_interval_ms == 1_500


async def test_direct_refresh_refuses_a_source_converted_to_single_get_while_pacer_is_pending() -> (
    None
):
    """An owner mode edit cannot turn a legacy Pacer token into an untracked LibCal GET (ADR-005)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = _source(now)
    repository = _MemorySourceRepository(source)
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer(
        on_first_acquire=lambda: setattr(
            repository,
            "source",
            replace(source, mode=CatalogSourceMode.LIBCAL_ICS, page_limit=1),
        )
    )
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh(source.source_key, "manual:legacy-to-single-get")

    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert result.detail == "source requires the single-GET catalog workflow before fetch"
    assert fetcher.calls == []
    assert catalog.batches == []
    assert repository.failed == [
        "catalog source requires the single-GET catalog workflow before fetch"
    ]


async def test_single_get_refresh_refuses_a_mode_without_a_durable_page_cursor() -> None:
    """P15a never routes a paginator through a timer that could restart it at page one (NFR-8)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer()
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh(
        "approved-calendar",
        "manual:must-not-page-restart",
        require_single_http_get=True,
    )

    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert result.detail == "source is not approved for the single-GET catalog workflow"
    assert fetcher.calls == []
    assert pacer.requests == []
    assert repository.claim_leases == []


async def test_refresh_lease_covers_the_reviewed_paced_request_cap() -> None:
    """A complete slow source cannot lose its durable lease before its final catalog write (NFR-8)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = CatalogSource(
        source_key="bounded-slow-source",
        display_name="Bounded Slow Source",
        publisher="Test Publisher",
        seed_url="https://events.example.test/sitemap.xml",
        approved_origins=("https://events.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=now,
        review_expires_at=None,
        refresh_interval_minutes=1_440,
        min_interval_ms=5_000,
        page_limit=160,
    )
    repository = _MemorySourceRepository(source)
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer()
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh(source.source_key, "manual:slow-source")

    assert result.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert repository.claim_leases == [860]


async def test_meetup_detail_enrichment_lease_covers_all_reviewed_request_units() -> None:
    """Meetup's city request plus forty paced detail requests fit in one durable lease."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = CatalogSource(
        source_key="meetup-sf",
        display_name="Meetup San Francisco",
        publisher="Meetup",
        seed_url="https://www.meetup.com/find/?location=us--ca--San%20Francisco",
        approved_origins=("https://www.meetup.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.MEETUP_CITY_JSONLD,
        enabled=True,
        reviewed_at=now,
        review_expires_at=None,
        refresh_interval_minutes=120,
        min_interval_ms=1_500,
        page_limit=41,
        source_revision=2,
    )
    repository = _MemorySourceRepository(source)
    service = CatalogRefreshService(
        repository,
        _Committer(repository, _Catalog(), _Observations()),
        {CatalogSourceMode.MEETUP_CITY_JSONLD: _Fetcher([_candidate(now)])},
        _RecordingPacer(),
        _MutableDiscoveryPolicyGate(),
        lease_seconds=60,
        now=lambda: now,
    )

    result = await service.refresh(source.source_key, "manual:meetup-detail-cap")

    assert result.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert repository.claim_leases == [122]


async def test_refresh_rejects_a_source_whose_paced_cap_exceeds_database_lease_bound() -> None:
    """An over-budget registry row makes zero Pacer, source, or catalog calls (FR-10.3/NFR-8)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = CatalogSource(
        source_key="impossibly-slow-source",
        display_name="Impossibly Slow Source",
        publisher="Test Publisher",
        seed_url="https://events.example.test/sitemap.xml",
        approved_origins=("https://events.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=now,
        review_expires_at=None,
        refresh_interval_minutes=1_440,
        min_interval_ms=5_000,
        page_limit=1_000,
    )
    repository = _MemorySourceRepository(source)
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer()
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh(source.source_key, "manual:impossible-source")

    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert result.detail == "source pacing and page cap exceed the maximum catalog refresh lease"
    assert repository.claim_leases == []
    assert pacer.keys == []
    assert fetcher.calls == []
    assert catalog.batches == []


@pytest.mark.parametrize(
    ("enabled", "reviewed", "review_expires_at"),
    [
        (False, True, None),
        (True, False, None),
        (True, True, datetime(2026, 7, 16, 11, 0, tzinfo=UTC)),
    ],
)
async def test_disabled_unreviewed_or_expired_source_makes_zero_fetches(
    enabled: bool, reviewed: bool, review_expires_at: datetime | None
) -> None:
    """Registry approval is checked before a Pacer or network call (FR-10.3)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(
        _source(now, enabled=enabled, reviewed=reviewed, review_expires_at=review_expires_at)
    )
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer()
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh("approved-calendar", "manual:blocked")

    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert fetcher.calls == []
    assert catalog.batches == []
    assert observations.records == []
    assert pacer.keys == []


async def test_disabled_discovery_policy_skips_before_any_pacer_or_fetch() -> None:
    """A durable source-modality disable makes zero catalog dispatches (FR-3.9/FR-10.1, AC-23)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer()
    gate = _MutableDiscoveryPolicyGate(
        [PolicyDecision.deny("automation not allowed for public_jsonld/browser")]
    )
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        gate,
        now=lambda: now,
    )

    result = await service.refresh("approved-calendar", "manual:policy-disabled")

    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert "policy denied" in result.detail
    assert fetcher.calls == []
    assert catalog.batches == []
    assert observations.records == []
    assert pacer.keys == []
    assert gate.calls == [(Source.PUBLIC_JSONLD, Modality.BROWSER)]


async def test_policy_flip_after_pacing_releases_run_without_a_source_call() -> None:
    """The immediate pre-fetch read closes the Pacer-to-wire policy race (FR-3.9, AC-23)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer()
    gate = _MutableDiscoveryPolicyGate(
        [
            PolicyDecision.allow(),
            PolicyDecision.deny("source 'public_jsonld' quarantined"),
        ]
    )
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        gate,
        now=lambda: now,
    )

    denied = await service.refresh("approved-calendar", "manual:policy-race")
    recovered = await service.refresh("approved-calendar", "manual:policy-race")

    assert denied.outcome is CatalogRefreshOutcome.SKIPPED
    assert "quarantined" in denied.detail
    assert recovered.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert len(fetcher.calls) == 1
    assert repository.failed == ["Discovery policy denied: source 'public_jsonld' quarantined"]
    assert len(pacer.keys) == 2
    assert gate.calls == [
        (Source.PUBLIC_JSONLD, Modality.BROWSER),
        (Source.PUBLIC_JSONLD, Modality.BROWSER),
        (Source.PUBLIC_JSONLD, Modality.BROWSER),
        (Source.PUBLIC_JSONLD, Modality.BROWSER),
    ]


async def test_source_registry_flip_after_pacing_releases_run_without_a_source_call() -> None:
    """A source disable during Pacer acquisition fences the subsequent egress call (FR-10.3)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])

    def disable_source() -> None:
        repository.source = _source(now, enabled=False)

    pacer = _RecordingPacer(on_first_acquire=disable_source)
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh("approved-calendar", "manual:source-registry-race")

    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert result.detail == "source disabled, unreviewed, or expired before fetch"
    assert repository.failed == ["catalog source no longer refreshable before fetch"]
    assert fetcher.calls == []
    assert catalog.batches == []
    assert observations.records == []


async def test_source_pacing_cap_widened_after_claim_releases_before_fetch() -> None:
    """A registry edit cannot turn a short granted lease into a slow live source call (FR-10.3/NFR-8)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = _source(now)
    repository = _MemorySourceRepository(source)
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])

    def widen_source_request_budget() -> None:
        repository.source = replace(source, min_interval_ms=5_000, page_limit=160)

    pacer = _RecordingPacer(on_first_acquire=widen_source_request_budget)
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh("approved-calendar", "manual:source-budget-race")

    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert result.detail == "source pacing and page cap changed beyond the claimed refresh lease"
    assert repository.claim_leases == [300]
    assert repository.failed == [
        "catalog source pacing and page cap changed beyond the claimed refresh lease"
    ]
    assert fetcher.calls == []
    assert catalog.batches == []
    assert observations.records == []


async def test_bibliocommons_lease_reserves_bounded_catalog_persistence_time() -> None:
    """Large library feeds retain lease authority through their atomic O(N) catalog merge."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = replace(
        _source(now),
        mode=CatalogSourceMode.BIBLIOCOMMONS_RSS,
        min_interval_ms=5_000,
        page_limit=150,
    )
    repository = _MemorySourceRepository(source)
    fetcher = _Fetcher([_candidate(now)])
    service = CatalogRefreshService(
        repository,
        _Committer(repository, _Catalog(), _Observations()),
        {CatalogSourceMode.BIBLIOCOMMONS_RSS: fetcher},
        _RecordingPacer(),
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh(source.source_key, "manual:large-library")

    assert result.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert repository.claim_leases == [1_748]


async def test_refresh_failure_is_recorded_then_the_same_run_can_retry() -> None:
    """A crashed fetch releases a durable failed run rather than poisoning future recovery (NFR-8)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog = _Catalog()
    observations = _Observations()
    failing = _Fetcher([], RuntimeError("fixture fetch failed"))
    pacer = _RecordingPacer()
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: failing},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    with pytest.raises(RuntimeError, match="fixture fetch failed"):
        await service.refresh("approved-calendar", "manual:retry")

    recovered_fetcher = _Fetcher([_candidate(now)])
    recovered = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: recovered_fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )
    result = await recovered.refresh("approved-calendar", "manual:retry")

    assert repository.failed == ["fixture fetch failed"]
    assert result.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert len(recovered_fetcher.calls) == 1


async def test_incomplete_livewhale_feed_preserves_the_previous_publication() -> None:
    """A page cap failure must not replace already published records with a partial feed."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = replace(_source(now), mode=CatalogSourceMode.LIVEWHALE_JSON, page_limit=1)
    repository = _MemorySourceRepository(source)
    catalog = _Catalog()
    observations = _Observations()
    previous = [_candidate(now)]
    catalog.batches.append(previous)
    committer = _Committer(repository, catalog, observations)
    fetcher = LiveWhaleCatalogFetcher(
        user_agent="test",
        now=lambda: now,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "meta": {"page": 1, "per_page": 1, "total_results": 2, "total_pages": 2},
                    "data": [
                        {
                            "id": 1,
                            "title": "Partial feed",
                            "url": "/event/1",
                            "date_iso": "2026-07-20T18:00:00-07:00",
                        }
                    ],
                    "links": {"next": "?page=2"},
                },
            )
        ),
    )
    service = CatalogRefreshService(
        repository,
        committer,
        {CatalogSourceMode.LIVEWHALE_JSON: fetcher},
        _RecordingPacer(),
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )
    with pytest.raises(LiveWhaleFetchError, match="1-page cap"):
        await service.refresh(source.source_key, "manual:incomplete-livewhale")
    assert catalog.batches == [previous]
    assert committer.calls == []
    assert observations.records == []
    assert repository.completed == set()
    assert len(repository.failed) == 1


async def test_transient_source_failure_is_recorded_as_deferred_retry_work() -> None:
    """A normalized transport outage releases the run and supplies a durable retry delay."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    fetcher = _Fetcher(
        [],
        SourceTransientError("fixture DNS outage", retry_after_seconds=15.0),
    )
    service = CatalogRefreshService(
        repository,
        _Committer(repository, _Catalog(), _Observations()),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        _RecordingPacer(),
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh("approved-calendar", "manual:transient")

    assert result.outcome is CatalogRefreshOutcome.DEFERRED
    assert result.retry_after_seconds == 15.0
    assert result.detail == "fixture DNS outage"
    assert repository.failed == ["Source transient failure: fixture DNS outage"]


async def test_pacer_deferral_releases_the_catalog_claim_without_a_fetch() -> None:
    """A Pacer projection is not a reservation: the run can be safely re-claimed on retry."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer(
        [PacerLease(PacerLeaseStatus.WAIT, retry_after_seconds=45.0, detail="fixture quota")]
    )
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    deferred = await service.refresh("approved-calendar", "manual:paced")
    recovered = await service.refresh("approved-calendar", "manual:paced")

    assert deferred.outcome is CatalogRefreshOutcome.DEFERRED
    assert deferred.retry_after_seconds == 45.0
    assert fetcher.calls == [_source(now)]
    assert len(catalog.batches) == 1
    assert observations.records[0][1] == "manual:paced"
    assert repository.failed == []
    assert repository.paused == ["Pacer wait: fixture quota"]
    assert recovered.outcome is CatalogRefreshOutcome.SUCCEEDED


async def test_pacer_deferral_reports_busy_when_the_pause_lease_is_lost() -> None:
    """A failed pause fence must not claim that an unrecorded durable retry exists."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer(
        [PacerLease(PacerLeaseStatus.WAIT, retry_after_seconds=45.0, detail="fixture quota")]
    )
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    original_pause = repository.pause_refresh

    async def lose_pause(
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        del source_key, run_key, lease_token, error
        return False

    repository.pause_refresh = lose_pause  # type: ignore[method-assign]
    result = await service.refresh("approved-calendar", "manual:lost-pause")
    repository.pause_refresh = original_pause  # type: ignore[method-assign]

    assert result.outcome is CatalogRefreshOutcome.BUSY
    assert result.retry_after_seconds is None
    assert result.detail == (
        "catalog refresh lease was lost before the Pacer defer could be recorded"
    )
    assert fetcher.calls == []


async def test_catalog_source_throttle_records_retry_after_and_releases_the_run() -> None:
    """A fetcher exposes 429 metadata through the port instead of retrying blind (AC-73)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([], SourceRateLimitedError("fixture 429", retry_after_seconds=45.0))
    pacer = _RecordingPacer()
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh("approved-calendar", "manual:source-429")

    assert result.outcome is CatalogRefreshOutcome.DEFERRED
    assert result.detail == "fixture 429"
    assert (
        result.retry_after_seconds == 45.0
    )  # Redis-loss feedback cannot shorten provider backoff.
    assert pacer.backoffs == [("public_jsonld:catalog:approved-calendar", 45.0)]
    assert pacer.keys == [
        "public_jsonld:catalog:approved-calendar",
        "public_jsonld:catalog:approved-calendar",
    ]
    assert catalog.batches == []
    assert observations.records == []
    assert repository.failed == ["Source rate limited: fixture 429"]


async def test_access_denial_quarantines_catalog_source_without_raw_persistence_or_retry() -> None:
    """A catalog ban records a safe failed run and blocks the next fresh dispatch (AC-72)."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog = _Catalog()
    observations = _Observations()
    fetcher = _Fetcher([], SourceAccessDeniedError(SourceQuarantineSignal.BAN))
    pacer = _RecordingPacer()
    source_policies = {
        Source.PUBLIC_JSONLD: SourcePolicy(
            source=Source.PUBLIC_JSONLD,
            automation_allowed={Modality.BROWSER: True},
        )
    }
    quarantine = MockSourceQuarantineRepository(source_policies)
    gate = StoreBackedDiscoveryPolicyGate(MockDiscoveryPolicyReader(source_policies))
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        gate,
        quarantine,
        now=lambda: now,
    )

    first = await service.refresh("approved-calendar", "manual:access-denied:first")
    second = await service.refresh("approved-calendar", "manual:access-denied:retry")

    assert first.outcome is CatalogRefreshOutcome.SKIPPED
    assert first.detail == "source access denied; source quarantined"
    assert second.outcome is CatalogRefreshOutcome.SKIPPED
    assert "quarantined" in second.detail
    assert len(fetcher.calls) == 1
    assert pacer.keys == ["public_jsonld:catalog:approved-calendar"]
    assert source_policies[Source.PUBLIC_JSONLD].quarantined is True
    assert quarantine.calls == [(Source.PUBLIC_JSONLD, SourceQuarantineSignal.BAN)]
    assert catalog.batches == []
    assert observations.records == []
    assert repository.failed == ["Source access denied; source quarantine requested"]


async def test_non_handoff_registry_record_is_not_a_fetchable_escape_hatch() -> None:
    """The public catalog worker cannot become an autonomous source through configuration alone."""
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now, handoff_only=False))
    observations = _Observations()
    catalog = _Catalog()
    fetcher = _Fetcher([_candidate(now)])
    pacer = _RecordingPacer()
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh("approved-calendar", "manual:not-handoff")

    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert fetcher.calls == []
    assert observations.records == []


class _Parser:
    async def parse(self, tenant_id: UUID, request_id: UUID, text: str) -> EventRequest:
        return EventRequest(
            request_id=request_id,
            tenant_id=tenant_id,
            raw_text=text,
            constraints=RequestConstraints(),
        )


class _NeverDiscovery:
    async def discover(self, constraints: RequestConstraints) -> list[CanonicalEvent]:
        del constraints
        raise AssertionError("request parsing must not trigger a source fetch")


async def test_api_parsing_reads_the_persisted_catalog_without_invoking_discovery() -> None:
    """Feed/intake no longer turns a user request into a public-web crawl (NFR-8)."""
    container = cast(Container, SimpleNamespace(parser=_Parser(), discovery=_NeverDiscovery()))
    tenant = Tenant(uuid4(), "oidc|catalog-only", "user@example.test", "user@u.example.test")

    request, request_id = await _parse(container, tenant.tenant_id, "find a local event")

    assert request.request_id == request_id
    assert request.raw_text == "find a local event"


async def test_luma_detail_lease_covers_the_events_its_page_cap_can_carry() -> None:
    """Both Luma modes make one paced detail request per retained event, not one per page.

    Reserving pages alone left these sources leaning on the 300-second process default. A calendar
    that grows past it does not fail loudly: the fetch keeps running, the lease expires underneath
    it, and the commit is fenced out after every request has already been made.
    """
    now = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
    source = CatalogSource(
        source_key="luma-thecommons",
        display_name="The Commons",
        publisher="Luma Calendar",
        seed_url=(
            "https://api.luma.com/calendar/get-items"
            "?calendar_api_id=cal-ahTi4ptrN9WCYkg&pagination_limit=20&period=future"
        ),
        approved_origins=("https://api.luma.com", "https://api2.luma.com"),
        region="bay_area_9_county",
        mode=CatalogSourceMode.LUMA_CALENDAR_JSON,
        enabled=True,
        reviewed_at=now,
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=30,
    )
    repository = _MemorySourceRepository(source)
    catalog = _Catalog()
    observations = _Observations()
    service = CatalogRefreshService(
        repository,
        _Committer(repository, catalog, observations),
        {CatalogSourceMode.LUMA_CALENDAR_JSON: _Fetcher([_candidate(now)])},
        _RecordingPacer(),
        _MutableDiscoveryPolicyGate(),
        now=lambda: now,
    )

    result = await service.refresh(source.source_key, "manual:luma-calendar")

    assert result.outcome is CatalogRefreshOutcome.SUCCEEDED
    # 30 pages + 30 x 25 events = 780 paced units at 1.5s, plus the 60s completion buffer.
    assert repository.claim_leases == [1_230]


@pytest.mark.parametrize(
    ("mode", "page_limit", "min_interval_ms", "expected"),
    [
        # 30 pages + 30 x 25 events = 780 units at 1.5s, plus the 60s completion buffer.
        (CatalogSourceMode.LUMA_CALENDAR_JSON, 30, 1_500, 1_230),
        (CatalogSourceMode.LUMA_DISCOVER_JSON, 40, 1_500, 1_620),
        # Past this the reservation exceeds the one-hour database ceiling and the row fails closed.
        (CatalogSourceMode.LUMA_CALENDAR_JSON, 90, 1_500, 3_570),
        (CatalogSourceMode.LUMA_CALENDAR_JSON, 91, 1_500, None),
        # Alameda's 0194 cap: 400s pacing + 500s persistence + 60s completion buffer.
        (CatalogSourceMode.BIBLIOCOMMONS_RSS, 80, 5_000, 960),
        (CatalogSourceMode.BIBLIOCOMMONS_RSS, 314, 5_000, 3_593),
        (CatalogSourceMode.BIBLIOCOMMONS_RSS, 315, 5_000, None),
        # A mode with no detail lane reserves for pages alone.
        (CatalogSourceMode.PUBLIC_JSONLD, 30, 1_500, 300),
    ],
)
def test_the_lease_reservation_is_the_real_upper_bound_on_a_reviewed_page_cap(
    mode: CatalogSourceMode,
    page_limit: int,
    min_interval_ms: int,
    expected: int | None,
) -> None:
    """A row whose reservation returns None is SKIPPED with no run recorded at all.

    That is coverage disappearing with no failure to look at, so the ceiling is asserted here and
    the registry is asserted against this same function.
    """
    now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)
    source = CatalogSource(
        source_key="lease-budget-fixture",
        display_name="Lease budget fixture",
        publisher="Lease budget publisher",
        seed_url="https://lease.example.test/catalog",
        approved_origins=("https://lease.example.test",),
        region="bay_area_9_county",
        mode=mode,
        enabled=True,
        reviewed_at=now,
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=min_interval_ms,
        page_limit=page_limit,
    )

    assert catalog_refresh_lease_seconds(source, floor_seconds=300) == expected


async def test_collection_window_is_injected_and_filters_before_atomic_publication() -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    window = CatalogCollectionWindow(1, 1, now, now + timedelta(days=1), 1)
    repository = _MemorySourceRepository(_source(now))
    catalog, observations = _Catalog(), _Observations()
    candidates = [replace(_candidate(now), source_event_id=str(index), start_at=at)
                  for index, at in enumerate((now - timedelta(seconds=1), now, window.end_at))]
    fetcher = _Fetcher(candidates)
    windows = _FrozenWindowRepository(window)
    service = CatalogRefreshService(
        repository, _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher}, _RecordingPacer(), _MutableDiscoveryPolicyGate(),
        collection_windows=windows, now=lambda: now,
    )
    result = await service.refresh("approved-calendar", "manual:window")
    assert result.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert (result.candidate_count, result.canonical_count) == (1, 1)
    assert fetcher.calls[0].collection_window == window
    assert [candidate.source_event_id for candidate in catalog.batches[0]] == ["1"]
    assert windows.calls[0][3] == 1


async def test_unadmitted_collection_window_never_fetches_or_publishes() -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog, observations = _Catalog(), _Observations()
    fetcher = _Fetcher([_candidate(now)])
    service = CatalogRefreshService(
        repository, _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher}, _RecordingPacer(), _MutableDiscoveryPolicyGate(),
        collection_windows=_FrozenWindowRepository(None), now=lambda: now,
    )
    result = await service.refresh("approved-calendar", "manual:denied-window")
    assert result.outcome is CatalogRefreshOutcome.DEFERRED
    assert repository.claimed == {}
    assert result.retry_after_seconds == 1.0
    assert fetcher.calls == []
    assert catalog.batches == []
    assert observations.records == []


async def test_retry_keeps_database_window_when_worker_clock_and_registry_horizon_change() -> None:
    first_now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    current_now = first_now
    window = CatalogCollectionWindow(1, 1, first_now, first_now + timedelta(days=1), 1)
    repository = _MemorySourceRepository(_source(first_now))
    catalog, observations = _Catalog(), _Observations()
    fetcher = _Fetcher([replace(_candidate(first_now), start_at=first_now + timedelta(hours=1))],
                       error=SourceTransientError("temporary source error", retry_after_seconds=1))
    windows = _FrozenWindowRepository(window)
    service = CatalogRefreshService(
        repository, _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher}, _RecordingPacer(), _MutableDiscoveryPolicyGate(),
        collection_windows=windows, now=lambda: current_now,
    )
    assert (await service.refresh("approved-calendar", "manual:retry-window")).outcome is CatalogRefreshOutcome.DEFERRED
    current_now += timedelta(days=1)
    repository.source = replace(repository.source, collection_horizon_days=30, source_revision=2)
    windows.window = replace(window, attempt_count=2)
    fetcher._error = None
    result = await service.refresh("approved-calendar", "manual:retry-window")
    assert result.candidate_count == 1
    assert [source.collection_window.start_at for source in fetcher.calls] == [first_now, first_now]
    assert fetcher.calls[-1].collection_window.end_at == window.end_at
    assert windows.calls[-1][3] == 2


async def test_expired_frozen_window_requires_new_run_without_fetch_or_lease_wait() -> None:
    now = datetime(2026, 7, 16, 12, tzinfo=UTC)
    repository = _MemorySourceRepository(_source(now))
    catalog, observations = _Catalog(), _Observations()
    fetcher = _Fetcher([_candidate(now)])

    class Windows(_FrozenWindowRepository):
        async def prepare_collection_window(self, *args, **kwargs):
            raise CatalogCollectionWindowUnavailableError("collection_window_expired")

    service = CatalogRefreshService(
        repository, _Committer(repository, catalog, observations),
        {CatalogSourceMode.PUBLIC_JSONLD: fetcher}, _RecordingPacer(), _MutableDiscoveryPolicyGate(),
        collection_windows=Windows(None), now=lambda: now,
    )
    result = await service.refresh("approved-calendar", "manual:expired")
    assert result.outcome is CatalogRefreshOutcome.SKIPPED
    assert "collection_window_expired" in result.detail
    assert "start a new run" in result.detail
    assert repository.failed == ["source changed: collection_window_expired"]
    assert repository.claimed == {}
    assert fetcher.calls == []
    assert catalog.batches == []
    assert observations.records == []
