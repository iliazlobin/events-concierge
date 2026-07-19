"""P15b page-cursor service tests (FR-10.3/10.4, NFR-1/NFR-8, ADR-003/005)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from events_concierge.application.catalog_paged_refresh import PagedCatalogRefreshService
from events_concierge.application.catalog_refresh import CatalogRefreshOutcome
from events_concierge.domain.catalog_sources import (
    CatalogPagedRefreshPreparation,
    CatalogPagedRefreshProgress,
    CatalogPagedRefreshPromotion,
    CatalogPagedStageResult,
    CatalogRefreshClaim,
    CatalogSource,
    CatalogSourcePage,
)
from events_concierge.domain.enums import (
    CatalogRefreshClaimOutcome,
    CatalogSourceMode,
    Modality,
    Source,
)
from events_concierge.domain.events import CandidateEvent
from events_concierge.domain.policy import PolicyDecision
from events_concierge.ports.policy import PacerLease, PacerLeaseStatus, PacerRequest
from events_concierge.ports.sources import SourceRateLimitedError


class _Sources:
    """In-memory lease ledger exposing the same reclaim behavior as P15b's SQL capability."""

    def __init__(self, source: CatalogSource) -> None:
        self.source = source
        self.active: dict[tuple[str, str], UUID] = {}
        self.completed: set[tuple[str, str]] = set()
        self.claims: list[tuple[str, str, int]] = []

    async def get(self, source_key: str) -> CatalogSource | None:
        return self.source if self.source.source_key == source_key else None

    async def claim_refresh(
        self, source_key: str, run_key: str, *, lease_seconds: int
    ) -> CatalogRefreshClaim:
        self.claims.append((source_key, run_key, lease_seconds))
        key = (source_key, run_key)
        if key in self.completed:
            return CatalogRefreshClaim(CatalogRefreshClaimOutcome.SUCCEEDED)
        if key in self.active:
            return CatalogRefreshClaim(CatalogRefreshClaimOutcome.BUSY)
        token = uuid4()
        self.active[key] = token
        return CatalogRefreshClaim(CatalogRefreshClaimOutcome.ACQUIRED, token)

    def release(self, source_key: str, run_key: str, token: UUID) -> bool:
        key = (source_key, run_key)
        if self.active.get(key) != token:
            return False
        del self.active[key]
        return True

    def complete(self, source_key: str, run_key: str, token: UUID) -> bool:
        if not self.release(source_key, run_key, token):
            return False
        self.completed.add((source_key, run_key))
        return True


class _Progress:
    """Fixture P15b stage with revision-reset and full-page pause behavior."""

    def __init__(self, sources: _Sources) -> None:
        self._sources = sources
        self.progress: CatalogPagedRefreshProgress | None = None
        self.staged_pages: list[int] = []
        self.pauses: list[str] = []
        self.aborts: list[str] = []
        self.resets = 0

    async def prepare_paged_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        source_revision: int,
    ) -> CatalogPagedRefreshPreparation:
        if self._sources.active.get((source_key, run_key)) != lease_token:
            return CatalogPagedRefreshPreparation("lease_lost")
        if self.progress is not None and self.progress.source_revision != source_revision:
            self.progress = None
            self.resets += 1
        if self.progress is None:
            self.progress = CatalogPagedRefreshProgress(
                source_key=source_key,
                run_key=run_key,
                source_revision=source_revision,
                window_start_day=date(2026, 7, 18),
                next_page=0,
                page_limit=self._sources.source.page_limit,
                terminal_page=None,
                staged_raw_count=0,
                staged_candidate_count=0,
            )
        return CatalogPagedRefreshPreparation("ready", self.progress)

    async def stage_paged_page(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        source_revision: int,
        page: CatalogSourcePage,
    ) -> CatalogPagedStageResult:
        progress = self.progress
        if (
            progress is None
            or self._sources.active.get((source_key, run_key)) != lease_token
            or progress.source_revision != source_revision
            or progress.next_page != page.page_number
        ):
            return CatalogPagedStageResult("stale_cursor")
        self.staged_pages.append(page.page_number)
        self.progress = replace(
            progress,
            next_page=page.page_number + 1,
            terminal_page=page.page_number if page.raw_count < 100 else None,
            staged_raw_count=progress.staged_raw_count + page.raw_count,
            staged_candidate_count=progress.staged_candidate_count + len(page.candidates),
        )
        if page.raw_count < 100:
            return CatalogPagedStageResult("terminal")
        self._sources.release(source_key, run_key, lease_token)
        return CatalogPagedStageResult("more")

    async def pause_paged_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        self.pauses.append(error)
        return self._sources.release(source_key, run_key, lease_token)

    async def abort_paged_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        self.aborts.append(error)
        self.progress = None
        return self._sources.release(source_key, run_key, lease_token)


class _LeaseLostPrepareProgress(_Progress):
    """Fixture whose database prepare capability loses authority before any Pacer admission."""

    async def prepare_paged_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        source_revision: int,
    ) -> CatalogPagedRefreshPreparation:
        del source_key, run_key, lease_token, source_revision
        return CatalogPagedRefreshPreparation("lease_lost")


class _Promoter:
    """Records only terminal stage promotion and marks the fixture run complete."""

    def __init__(
        self, sources: _Sources, progress: _Progress, error: Exception | None = None
    ) -> None:
        self._sources = sources
        self._progress = progress
        self.error = error
        self.calls: list[tuple[str, str, int]] = []

    async def promote_paged_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        source_revision: int,
    ) -> CatalogPagedRefreshPromotion:
        self.calls.append((source_key, run_key, source_revision))
        if self.error is not None:
            raise self.error
        assert self._progress.progress is not None
        assert self._progress.progress.terminal_page is not None
        assert self._sources.complete(source_key, run_key, lease_token)
        return CatalogPagedRefreshPromotion(
            self._progress.progress.staged_candidate_count,
            self._progress.progress.staged_candidate_count,
        )


class _Fetcher:
    """One-page fixture fetcher that proves the service owns cursor selection and pacing."""

    def __init__(self, pages: dict[int, CatalogSourcePage | Exception]) -> None:
        self._pages = pages
        self.calls: list[tuple[date, int]] = []

    async def fetch_page(
        self,
        source: CatalogSource,
        *,
        window_start_day: date,
        page_number: int,
    ) -> CatalogSourcePage:
        del source
        self.calls.append((window_start_day, page_number))
        page = self._pages[page_number]
        if isinstance(page, Exception):
            raise page
        return page


class _Pacer:
    """Deterministic Pacer fixture with an optional source-revision race callback."""

    def __init__(
        self,
        leases: list[PacerLease] | None = None,
        *,
        on_acquire: Callable[[], None] | None = None,
    ) -> None:
        self._leases = list(leases or [])
        self._on_acquire = on_acquire
        self.requests: list[PacerRequest] = []
        self.backoffs: list[float | None] = []

    async def acquire(self, request: PacerRequest) -> PacerLease:
        self.requests.append(request)
        if self._on_acquire is not None:
            callback = self._on_acquire
            self._on_acquire = None
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
        del request, reset_at
        self.backoffs.append(retry_after_seconds)


class _Gate:
    """Always-allow fixture gate that records the P15b preflight and pre-wire checks."""

    def __init__(self) -> None:
        self.calls: list[tuple[Source, Modality]] = []

    async def evaluate_discovery(self, source: Source, modality: Modality) -> PolicyDecision:
        self.calls.append((source, modality))
        return PolicyDecision.allow()


def _source(*, source_revision: int = 1) -> CatalogSource:
    return CatalogSource(
        source_key="san-jose-legistar-meetings",
        display_name="San Jose Public Meetings",
        publisher="City of San Jose City Clerk",
        seed_url="https://webapi.legistar.com/v1/SanJose/Events",
        approved_origins=("https://webapi.legistar.com",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.SAN_JOSE_LEGISTAR,
        enabled=True,
        reviewed_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        review_expires_at=None,
        refresh_interval_minutes=360,
        min_interval_ms=1_500,
        page_limit=5,
        source_revision=source_revision,
    )


def _sunnyvale_source(*, source_revision: int = 2) -> CatalogSource:
    """Build P15c's separately reviewed Sunnyvale page contract (FR-10.3/10.4)."""
    return replace(
        _source(source_revision=source_revision),
        source_key="sunnyvale-legistar-meetings",
        display_name="Sunnyvale Public Meetings",
        publisher="City of Sunnyvale City Clerk",
        seed_url="https://webapi.legistar.com/v1/SunnyvaleCA/Events",
        mode=CatalogSourceMode.SUNNYVALE_LEGISTAR,
    )


def _alameda_source(*, source_revision: int = 2) -> CatalogSource:
    """Build P15d's separately reviewed Alameda page contract (FR-10.3/10.4)."""
    return replace(
        _source(source_revision=source_revision),
        source_key="alameda-legistar-meetings",
        display_name="Alameda Public Meetings",
        publisher="City of Alameda City Clerk",
        seed_url="https://webapi.legistar.com/v1/Alameda/Events",
        mode=CatalogSourceMode.ALAMEDA_LEGISTAR,
    )


def _oakland_source(*, source_revision: int = 2) -> CatalogSource:
    """Build P15e's separately reviewed Oakland page contract (FR-10.3/10.4)."""
    return replace(
        _source(source_revision=source_revision),
        source_key="oakland-legistar-meetings",
        display_name="Oakland Public Meetings",
        publisher="City of Oakland City Clerk",
        seed_url="https://webapi.legistar.com/v1/Oakland/Events",
        mode=CatalogSourceMode.OAKLAND_LEGISTAR,
    )


def _candidate(event_id: int) -> CandidateEvent:
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=(
            f"san-jose-legistar:san-jose-legistar-meetings:{event_id}:2026-07-20T17:30:00-07:00"
        ),
        title=f"Public meeting {event_id}",
        start_at=datetime(2026, 7, 20, 17, 30, tzinfo=UTC) + timedelta(minutes=event_id),
        registration_url=f"https://sanjose.legistar.com/MeetingDetail.aspx?LEGID={event_id}",
        venue_name="City Hall",
    )


def _page(page_number: int, raw_count: int, event_id: int) -> CatalogSourcePage:
    candidates = (_candidate(event_id),) if raw_count else ()
    source_event_ids = (str(event_id),) if raw_count else ()
    return CatalogSourcePage(page_number, raw_count, candidates, source_event_ids)


def _service(
    sources: _Sources,
    progress: _Progress,
    promoter: _Promoter,
    fetcher: _Fetcher,
    pacer: _Pacer,
) -> PagedCatalogRefreshService:
    return PagedCatalogRefreshService(
        sources,
        progress,
        promoter,
        {sources.source.mode: fetcher},
        pacer,
        _Gate(),
        now=lambda: datetime(2026, 7, 18, 12, 0, tzinfo=UTC),
    )


async def test_full_page_stages_releases_then_next_activity_reclaims_and_promotes_once() -> None:
    """Two physical pages have two Pacer leases and one terminal catalog effect (ADR-003/005)."""
    sources = _Sources(_source())
    progress = _Progress(sources)
    promoter = _Promoter(sources, progress)
    fetcher = _Fetcher({0: _page(0, 100, 101), 1: _page(1, 1, 202)})
    pacer = _Pacer()
    service = _service(sources, progress, promoter, fetcher, pacer)

    first = await service.refresh_page(sources.source.source_key, "manual:paged-once")
    second = await service.refresh_page(sources.source.source_key, "manual:paged-once")
    third = await service.refresh_page(sources.source.source_key, "manual:paged-once")

    assert first.outcome is CatalogRefreshOutcome.PROGRESSED
    assert second.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert third.outcome is CatalogRefreshOutcome.ALREADY_SUCCEEDED
    assert fetcher.calls == [(date(2026, 7, 18), 0), (date(2026, 7, 18), 1)]
    assert progress.staged_pages == [0, 1]
    assert promoter.calls == [(sources.source.source_key, "manual:paged-once", 1)]
    assert [request.queue_item_id for request in pacer.requests] == [
        "manual:paged-once:get:0",
        "manual:paged-once:get:1",
    ]
    assert {request.bucket_key for request in pacer.requests} == {
        "public_jsonld:catalog-origin:webapi.legistar.com"
    }


async def test_prepare_lease_loss_returns_busy_before_pacer_or_source_get() -> None:
    """A P30 database prepare fence makes zero egress-admission or source-read attempt (NFR-8)."""
    sources = _Sources(_source())
    progress = _LeaseLostPrepareProgress(sources)
    promoter = _Promoter(sources, progress)
    fetcher = _Fetcher({0: _page(0, 1, 101)})
    pacer = _Pacer()
    service = _service(sources, progress, promoter, fetcher, pacer)

    result = await service.refresh_page(sources.source.source_key, "manual:prepare-lease-lost")

    assert result.outcome is CatalogRefreshOutcome.BUSY
    assert pacer.requests == []
    assert fetcher.calls == []
    assert promoter.calls == []


async def test_post_pacer_lease_loss_returns_busy_before_source_get() -> None:
    """P32 revalidates P30's lease after Pacer admission and before egress (NFR-8)."""
    sources = _Sources(_source())
    progress = _Progress(sources)
    promoter = _Promoter(sources, progress)
    fetcher = _Fetcher({0: _page(0, 1, 101)})
    pacer = _Pacer(on_acquire=sources.active.clear)
    service = _service(sources, progress, promoter, fetcher, pacer)

    result = await service.refresh_page(sources.source.source_key, "manual:post-pacer-lease-lost")

    assert result.outcome is CatalogRefreshOutcome.BUSY
    assert len(pacer.requests) == 1
    assert fetcher.calls == []
    assert progress.staged_pages == []
    assert promoter.calls == []


async def test_post_pacer_terminal_revalidation_promotes_without_another_get() -> None:
    """P32 promotes a retained terminal stage found after admission instead of refetching (NFR-8)."""
    sources = _Sources(_source())
    progress = _Progress(sources)
    promoter = _Promoter(sources, progress)
    fetcher = _Fetcher({0: _page(0, 1, 101)})

    def retain_terminal_stage() -> None:
        assert progress.progress is not None
        progress.progress = replace(
            progress.progress,
            next_page=1,
            terminal_page=0,
            staged_raw_count=1,
            staged_candidate_count=1,
        )

    pacer = _Pacer(on_acquire=retain_terminal_stage)
    service = _service(sources, progress, promoter, fetcher, pacer)

    result = await service.refresh_page(sources.source.source_key, "manual:post-pacer-terminal")

    assert result.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert len(pacer.requests) == 1
    assert fetcher.calls == []
    assert progress.staged_pages == []
    assert promoter.calls == [(sources.source.source_key, "manual:post-pacer-terminal", 1)]


async def test_pacer_wait_preserves_the_same_cursor_without_a_source_get() -> None:
    """A Pacer wait pauses the run; Temporal can reclaim page zero without duplicating a stage."""
    sources = _Sources(_source())
    progress = _Progress(sources)
    promoter = _Promoter(sources, progress)
    fetcher = _Fetcher({0: _page(0, 1, 101)})
    pacer = _Pacer([PacerLease(PacerLeaseStatus.WAIT, 30.0, "fixture wait")])
    service = _service(sources, progress, promoter, fetcher, pacer)

    deferred = await service.refresh_page(sources.source.source_key, "manual:pacer-wait")
    recovered = await service.refresh_page(sources.source.source_key, "manual:pacer-wait")

    assert deferred.outcome is CatalogRefreshOutcome.DEFERRED
    assert deferred.retry_after_seconds == 30.0
    assert recovered.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert progress.pauses == ["Pacer wait: fixture wait"]
    assert fetcher.calls == [(date(2026, 7, 18), 0)]
    assert progress.staged_pages == [0]


async def test_source_revision_change_after_page_zero_discards_stage_before_another_get() -> None:
    """A post-admission owner edit resets P15b's stage and never mixes page contracts (FR-10.3)."""
    sources = _Sources(_source())
    progress = _Progress(sources)
    promoter = _Promoter(sources, progress)
    fetcher = _Fetcher({0: _page(0, 100, 101), 1: _page(1, 1, 202)})
    pacer = _Pacer()
    service = _service(sources, progress, promoter, fetcher, pacer)

    first = await service.refresh_page(sources.source.source_key, "manual:revision-race")
    pacer._on_acquire = lambda: setattr(sources, "source", _source(source_revision=2))
    deferred = await service.refresh_page(sources.source.source_key, "manual:revision-race")

    assert first.outcome is CatalogRefreshOutcome.PROGRESSED
    assert deferred.outcome is CatalogRefreshOutcome.DEFERRED
    assert deferred.retry_after_seconds == 1.0
    assert fetcher.calls == [(date(2026, 7, 18), 0)]
    assert progress.resets == 1
    assert progress.progress is not None
    assert progress.progress.source_revision == 2
    assert progress.progress.next_page == 0


async def test_source_429_pauses_after_prior_page_and_retries_the_same_next_page() -> None:
    """A provider throttle retains staged page zero and has no hidden local retry (AC-73/NFR-8)."""
    sources = _Sources(_source())
    progress = _Progress(sources)
    promoter = _Promoter(sources, progress)
    fetcher = _Fetcher(
        {
            0: _page(0, 100, 101),
            1: SourceRateLimitedError("fixture 429", retry_after_seconds=45.0),
        }
    )
    pacer = _Pacer()
    service = _service(sources, progress, promoter, fetcher, pacer)

    advanced = await service.refresh_page(sources.source.source_key, "manual:provider-429")
    throttled = await service.refresh_page(sources.source.source_key, "manual:provider-429")

    assert advanced.outcome is CatalogRefreshOutcome.PROGRESSED
    assert throttled.outcome is CatalogRefreshOutcome.DEFERRED
    assert throttled.retry_after_seconds == 45.0
    assert progress.progress is not None
    assert progress.progress.next_page == 1
    assert progress.staged_pages == [0]
    assert pacer.backoffs == [45.0]


async def test_untyped_source_failure_preserves_staged_pages_for_a_failed_workflow_retry() -> None:
    """A malformed/headerless 429 cannot discard page zero or invent a provider backoff (NFR-8)."""
    sources = _Sources(_source())
    progress = _Progress(sources)
    promoter = _Promoter(sources, progress)
    fetcher = _Fetcher({0: _page(0, 100, 101), 1: RuntimeError("fixture malformed 429")})
    pacer = _Pacer()
    service = _service(sources, progress, promoter, fetcher, pacer)

    advanced = await service.refresh_page(
        sources.source.source_key, "manual:untyped-source-failure"
    )
    with pytest.raises(RuntimeError, match="fixture malformed 429"):
        await service.refresh_page(sources.source.source_key, "manual:untyped-source-failure")

    assert advanced.outcome is CatalogRefreshOutcome.PROGRESSED
    assert progress.progress is not None
    assert progress.progress.next_page == 1
    assert progress.staged_pages == [0]
    assert progress.pauses[-1] == "Legistar page fetch failed: RuntimeError"
    assert pacer.backoffs == []

    fetcher._pages[1] = _page(1, 1, 202)
    recovered = await service.refresh_page(
        sources.source.source_key, "manual:untyped-source-failure"
    )

    assert recovered.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert fetcher.calls == [
        (date(2026, 7, 18), 0),
        (date(2026, 7, 18), 1),
        (date(2026, 7, 18), 1),
    ]
    assert progress.staged_pages == [0, 1]


async def test_sunnyvale_uses_the_same_durable_page_spine_without_widening_its_origin_scope() -> (
    None
):
    """P15c keeps Sunnyvale on its own cursor/run but the shared Legistar origin bucket (ADR-005)."""
    sources = _Sources(_sunnyvale_source())
    progress = _Progress(sources)
    promoter = _Promoter(sources, progress)
    fetcher = _Fetcher({0: _page(0, 1, 501)})
    pacer = _Pacer()
    service = _service(sources, progress, promoter, fetcher, pacer)

    result = await service.refresh_page(sources.source.source_key, "manual:sunnyvale-paged")

    assert result.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert sources.source.has_paged_http_get
    assert fetcher.calls == [(date(2026, 7, 18), 0)]
    assert [request.queue_item_id for request in pacer.requests] == ["manual:sunnyvale-paged:get:0"]
    assert [request.bucket_key for request in pacer.requests] == [
        "public_jsonld:catalog-origin:webapi.legistar.com"
    ]


async def test_alameda_uses_the_same_durable_page_spine_without_widening_its_origin_scope() -> None:
    """P15d keeps Alameda on its own cursor/run and the shared Legistar origin bucket (ADR-005)."""
    sources = _Sources(_alameda_source())
    progress = _Progress(sources)
    promoter = _Promoter(sources, progress)
    fetcher = _Fetcher({0: _page(0, 1, 601)})
    pacer = _Pacer()
    service = _service(sources, progress, promoter, fetcher, pacer)

    result = await service.refresh_page(sources.source.source_key, "manual:alameda-paged")

    assert result.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert sources.source.has_paged_http_get
    assert fetcher.calls == [(date(2026, 7, 18), 0)]
    assert [request.queue_item_id for request in pacer.requests] == ["manual:alameda-paged:get:0"]
    assert [request.bucket_key for request in pacer.requests] == [
        "public_jsonld:catalog-origin:webapi.legistar.com"
    ]


async def test_oakland_uses_the_same_durable_page_spine_without_widening_its_origin_scope() -> None:
    """P15e keeps Oakland on its own cursor/run and the shared Legistar origin bucket (ADR-005)."""
    sources = _Sources(_oakland_source())
    progress = _Progress(sources)
    promoter = _Promoter(sources, progress)
    fetcher = _Fetcher({0: _page(0, 1, 701)})
    pacer = _Pacer()
    service = _service(sources, progress, promoter, fetcher, pacer)

    result = await service.refresh_page(sources.source.source_key, "manual:oakland-paged")

    assert result.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert sources.source.has_paged_http_get
    assert fetcher.calls == [(date(2026, 7, 18), 0)]
    assert [request.queue_item_id for request in pacer.requests] == ["manual:oakland-paged:get:0"]
    assert [request.bucket_key for request in pacer.requests] == [
        "public_jsonld:catalog-origin:webapi.legistar.com"
    ]


async def test_crash_after_terminal_stage_retries_promotion_without_repeating_the_source_page() -> (
    None
):
    """A post-stage crash reuses P15b's terminal cursor rather than fetching page zero twice."""
    sources = _Sources(_source())
    progress = _Progress(sources)
    promoter = _Promoter(sources, progress, RuntimeError("fixture promotion crash"))
    fetcher = _Fetcher({0: _page(0, 1, 101)})
    pacer = _Pacer()
    service = _service(sources, progress, promoter, fetcher, pacer)

    with pytest.raises(RuntimeError, match="fixture promotion crash"):
        await service.refresh_page(sources.source.source_key, "manual:promotion-crash")

    token = sources.active[(sources.source.source_key, "manual:promotion-crash")]
    assert sources.release(sources.source.source_key, "manual:promotion-crash", token)
    promoter.error = None
    recovered = await service.refresh_page(sources.source.source_key, "manual:promotion-crash")

    assert recovered.outcome is CatalogRefreshOutcome.SUCCEEDED
    assert fetcher.calls == [(date(2026, 7, 18), 0)]
    assert progress.staged_pages == [0]
    assert len(promoter.calls) == 2
