"""Durable per-page approved Legistar catalog refresh (P15b/P15c/P15d/P15e).

Each activity claims a short database lease, reads or initializes a frozen query window, obtains a
shared origin Pacer admission immediately before one exact GET, and stages that one normalized
page. A full page releases the lease into a reclaimable ``paused`` state; a terminal short page is
promoted into canonical events, observations, and a successful run in one database transaction.
No raw provider document enters the workflow or staging ledger (FR-3.8/FR-10.3/10.4, NFR-1/NFR-8,
ADR-001/003/005).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from uuid import UUID

from ..domain.catalog_sources import CatalogPagedRefreshProgress, CatalogSource
from ..domain.catalog_window import in_collection_window
from ..domain.enums import CatalogRefreshClaimOutcome, CatalogSourceMode, Modality, Source
from ..domain.policy import PolicyDecision, PolicyDecisionCode
from ..ports.catalog_sources import (
    CatalogCollectionWindowRepository,
    CatalogCollectionWindowUnavailableError,
    CatalogPagedRefreshPromoter,
    CatalogPagedRefreshRepository,
    CatalogPagedSourceFetcher,
    CatalogRunEvidenceRecorder,
    CatalogSourceRepository,
)
from ..ports.discovery_policy import DiscoveryPolicyGate
from ..ports.policy import Pacer, PacerOperation, PacerRequest, SourceQuarantinePort
from ..ports.sources import SourceAccessDeniedError, SourceRateLimitedError
from .catalog_refresh import (
    CatalogRefreshOutcome,
    CatalogRefreshResult,
    collection_window_unavailable_detail,
)
from .catalog_run_evidence import CatalogRunEvidenceSession

_MAX_LEGISTAR_PAGES = 5
_MAX_CATALOG_REFRESH_LEASE_SECONDS = 3_600
_LEASE_COMPLETION_BUFFER_SECONDS = 60


@dataclass(frozen=True, slots=True)
class _PreparedPage:
    """One authority-checked source/fetcher/cursor triplet, never persisted in workflow history."""

    source: CatalogSource
    fetcher: CatalogPagedSourceFetcher
    progress: CatalogPagedRefreshProgress


class PagedCatalogRefreshService:
    """Run one P15b page or terminal promotion under independently recoverable effects (NFR-8)."""

    def __init__(
        self,
        sources: CatalogSourceRepository,
        progress: CatalogPagedRefreshRepository,
        promoter: CatalogPagedRefreshPromoter,
        fetchers: dict[CatalogSourceMode, CatalogPagedSourceFetcher],
        pacer: Pacer,
        policy_gate: DiscoveryPolicyGate,
        source_quarantine: SourceQuarantinePort | None = None,
        run_evidence: CatalogRunEvidenceRecorder | None = None,
        *,
        collection_windows: CatalogCollectionWindowRepository | None = None,
        lease_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not 0 < lease_seconds <= _MAX_CATALOG_REFRESH_LEASE_SECONDS:
            raise ValueError(
                "catalog paged refresh lease_seconds must be between one second and one hour"
            )
        self._sources = sources
        self._progress = progress
        self._promoter = promoter
        self._fetchers = dict(fetchers)
        self._pacer = pacer
        self._policy_gate = policy_gate
        self._source_quarantine = source_quarantine
        self._run_evidence = run_evidence
        self._collection_windows = collection_windows
        self._lease_seconds = lease_seconds
        self._now = now or (lambda: datetime.now(UTC))

    async def refresh_page(self, source_key: str, run_key: str) -> CatalogRefreshResult:
        """Execute one page-sized effect or a terminal staged promotion (P15b, ADR-003/005)."""
        evidence = CatalogRunEvidenceSession(
            self._run_evidence,
            source_key,
            run_key,
            # This path runs in a shared Temporal activity worker; process deltas would include
            # unrelated concurrent activities and are intentionally omitted.
            include_process_metrics=False,
        )
        try:
            result = await self._refresh_page_observed(source_key, run_key, evidence)
        except Exception as exc:
            await evidence.finish("failed", error=exc)
            raise
        await evidence.finish(result.outcome.value)
        return result

    async def _refresh_page_observed(
        self,
        source_key: str,
        run_key: str,
        evidence: CatalogRunEvidenceSession,
    ) -> CatalogRefreshResult:
        """Advance one page while recording closed, monotonic stage observations."""
        admission = await evidence.start_stage("admission")
        preflight = await self._preflight(source_key, run_key)
        if isinstance(preflight, CatalogRefreshResult):
            await admission.finish(preflight.outcome.value)
            return preflight
        source, fetcher = preflight
        lease_seconds = self._lease_seconds_for(source)
        claim = await self._sources.claim_refresh(source_key, run_key, lease_seconds=lease_seconds)
        if not claim.acquired:
            outcome = (
                CatalogRefreshOutcome.ALREADY_SUCCEEDED
                if claim.outcome is CatalogRefreshClaimOutcome.SUCCEEDED
                else CatalogRefreshOutcome.BUSY
            )
            await admission.finish(outcome.value)
            return CatalogRefreshResult(source_key, run_key, outcome)
        assert claim.lease_token is not None
        await admission.finish(CatalogRefreshOutcome.SUCCEEDED.value)
        return await self._refresh_claimed(
            source_key, run_key, source, fetcher, claim.lease_token, evidence
        )

    async def _refresh_claimed(
        self,
        source_key: str,
        run_key: str,
        source: CatalogSource,
        fetcher: CatalogPagedSourceFetcher,
        lease_token: UUID,
        evidence: CatalogRunEvidenceSession,
    ) -> CatalogRefreshResult:
        """Advance exactly one source cursor position while holding its fresh database lease."""
        prepared = await self._prepare(source_key, run_key, lease_token, source)
        if isinstance(prepared, CatalogRefreshResult):
            return prepared
        if prepared.progress.is_terminal:
            return await self._promote_if_current(
                source_key, run_key, lease_token, prepared, evidence
            )

        pacer_request = self._pacer_request(source, run_key, prepared.progress.next_page)
        lease = await self._pacer.acquire(pacer_request)
        if not lease.granted:
            paused = await self._progress.pause_paged_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error=f"Pacer {lease.status.value}: {lease.detail}",
            )
            if not paused:
                return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.BUSY)
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.DEFERRED,
                detail=lease.detail,
                retry_after_seconds=lease.retry_after_seconds,
            )

        reread = await self._recheck_after_pacer(
            source_key, run_key, lease_token, prepared, evidence
        )
        if isinstance(reread, CatalogRefreshResult):
            return reread
        prepared = reread
        # The paged provider adapter combines the HTTP request and response parsing.  Record the
        # observable adapter boundary as collection; extraction/enrichment remains explicitly
        # marked as included rather than receiving an invented timing.
        collect = await evidence.start_stage("collect")
        try:
            page = await prepared.fetcher.fetch_page(
                prepared.source,
                window_start_day=prepared.progress.window_start_day,
                page_number=prepared.progress.next_page,
            )
            page = replace(page, candidates=tuple(
                candidate for candidate in page.candidates
                if in_collection_window(prepared.source, candidate)
            ))
        except SourceRateLimitedError as error:
            await collect.finish(CatalogRefreshOutcome.DEFERRED.value, error=error)
            return await self._rate_limited(source_key, run_key, lease_token, pacer_request, error)
        except SourceAccessDeniedError as error:
            await collect.finish(CatalogRefreshOutcome.SKIPPED.value, error=error)
            await self._quarantine_after_access_denied(error)
            await self._progress.abort_paged_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error="Source access denied; source quarantine requested",
            )
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.SKIPPED,
                detail="source access denied; source quarantined",
            )
        except Exception as exc:
            await collect.finish("failed", error=exc)
            # An untyped transport/parser failure is not provider backoff, so do not invent a
            # shared throttle window.  Preserve already staged pages and let the failed Temporal
            # execution be restarted under the same source/run identity after the source recovers.
            # This keeps P15b/P15c/P15d/P15e's raw-free cursor durable across malformed/headerless
            # 429s and
            # ordinary transient failures without ever promoting a partial catalog (NFR-8, ADR-003/005).
            await self._progress.pause_paged_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error=f"Legistar page fetch failed: {type(exc).__name__}",
            )
            raise
        await collect.progress(candidate_count=len(page.candidates))
        await collect.finish(CatalogRefreshOutcome.SUCCEEDED.value)

        publish = await evidence.start_stage("catalog_publish")
        try:
            staged = await self._progress.stage_paged_page(
                source_key,
                run_key,
                lease_token=lease_token,
                source_revision=prepared.progress.source_revision,
                page=page,
            )
        except Exception as exc:
            await publish.finish("failed", error=exc)
            # A database contract fence can reject a page after its GET if an owner edit won the
            # race. Discard that old revision and fail the workflow so its stable source/slot ID
            # can restart against the newly reviewed contract; never leave a running lease behind.
            await self._progress.abort_paged_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error=f"Legistar staged page failed: {type(exc).__name__}",
            )
            raise
        if staged.status == "more":
            await publish.finish(CatalogRefreshOutcome.PROGRESSED.value)
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.PROGRESSED,
                detail=f"staged Legistar page {page.page_number}",
            )
        if staged.status == "terminal":
            await publish.finish(CatalogRefreshOutcome.PROGRESSED.value)
            return await self._promote_if_current(
                source_key, run_key, lease_token, prepared, evidence
            )
        if staged.status in {"lease_lost", "stale_cursor"}:
            await publish.finish(CatalogRefreshOutcome.BUSY.value)
            return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.BUSY)
        await self._progress.abort_paged_refresh(
            source_key,
            run_key,
            lease_token=lease_token,
            error=f"Legistar staged page rejected: {staged.status}",
        )
        detail = (
            "Legistar source exceeded its reviewed page cap"
            if staged.status == "cap_exceeded"
            else f"Legistar staged page rejected: {staged.status}"
        )
        await publish.finish(CatalogRefreshOutcome.SKIPPED.value)
        return CatalogRefreshResult(
            source_key, run_key, CatalogRefreshOutcome.SKIPPED, detail=detail
        )

    async def _prepare(
        self,
        source_key: str,
        run_key: str,
        lease_token: UUID,
        source: CatalogSource,
    ) -> _PreparedPage | CatalogRefreshResult:
        """Read/create a frozen P15b query plan; source revision mismatch never issues a GET."""
        preparation = await self._progress.prepare_paged_refresh(
            source_key,
            run_key,
            lease_token=lease_token,
            source_revision=source.source_revision,
        )
        if preparation.status == "ready":
            assert preparation.progress is not None
            if self._collection_windows is not None:
                try:
                    window = await self._collection_windows.prepare_collection_window(
                        source_key, run_key, lease_token=lease_token,
                        expected_revision=source.source_revision,
                    )
                except CatalogCollectionWindowUnavailableError as exc:
                    # Legacy/expired windows cannot become a fresh collection by retrying.
                    # Preserve staged rows, release the lease, and use the source-changed retry
                    # gate to park this run rather than repeatedly trying to promote it.
                    failed = await self._sources.fail_refresh(
                        source_key, run_key, lease_token=lease_token,
                        error=f"source changed: {exc.code}",
                    )
                    return CatalogRefreshResult(
                        source_key, run_key,
                        CatalogRefreshOutcome.SKIPPED if failed else CatalogRefreshOutcome.BUSY,
                        detail=collection_window_unavailable_detail(exc.code),
                    )
                if window is None:
                    released = await self._sources.fail_refresh(
                        source_key, run_key, lease_token=lease_token,
                        error="source changed: collection window admission changed",
                    )
                    return CatalogRefreshResult(
                        source_key, run_key,
                        CatalogRefreshOutcome.DEFERRED if released else CatalogRefreshOutcome.BUSY,
                        detail="collection window could not be admitted under the current source lease",
                        retry_after_seconds=1.0 if released else None,
                    )
                source = replace(source, collection_window=window)
            fetcher = self._fetchers.get(source.mode)
            if fetcher is None:
                await self._progress.abort_paged_refresh(
                    source_key,
                    run_key,
                    lease_token=lease_token,
                    error="Legistar source has no configured paged fetcher",
                )
                return CatalogRefreshResult(
                    source_key,
                    run_key,
                    CatalogRefreshOutcome.SKIPPED,
                    detail="Legistar source has no configured paged fetcher",
                )
            return _PreparedPage(source, fetcher, preparation.progress)
        if preparation.status == "lease_lost":
            return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.BUSY)
        if preparation.status == "source_changed":
            await self._progress.abort_paged_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error="source revision changed before paged refresh preparation",
            )
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.DEFERRED,
                detail="source revision changed before paged refresh preparation",
                retry_after_seconds=1.0,
            )
        await self._progress.abort_paged_refresh(
            source_key,
            run_key,
            lease_token=lease_token,
            error="source is not approved for the paged catalog workflow",
        )
        return CatalogRefreshResult(
            source_key,
            run_key,
            CatalogRefreshOutcome.SKIPPED,
            detail="source is not approved for the paged catalog workflow",
        )

    async def _recheck_after_pacer(
        self,
        source_key: str,
        run_key: str,
        lease_token: UUID,
        admitted: _PreparedPage,
        evidence: CatalogRunEvidenceSession,
    ) -> _PreparedPage | CatalogRefreshResult:
        """Fence the page GET after Pacer admission and before its one source effect (NFR-8).

        A Pacer grant reserves shared rate capacity, not the catalog run's database lease.  The
        existing P30 prepare capability therefore runs again after source/policy rechecks so its
        database-clock fence can stop an expired worker before the GET (FR-10.3/10.4, ADR-003/005).
        """
        source = await self._sources.get(source_key)
        source_result = self._source_preflight_result(source_key, run_key, source)
        if source_result is not None:
            await self._progress.abort_paged_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error=source_result.detail,
            )
            return source_result
        assert source is not None
        if source.source_revision != admitted.progress.source_revision:
            replacement = await self._prepare(source_key, run_key, lease_token, source)
            if isinstance(replacement, CatalogRefreshResult):
                return replacement
            paused = await self._progress.pause_paged_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error="source revision changed while awaiting Pacer",
            )
            if not paused:
                return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.BUSY)
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.DEFERRED,
                detail="source revision changed while awaiting Pacer; retry with fresh admission",
                retry_after_seconds=1.0,
            )
        decision = await self._dispatch_policy()
        if not decision.allowed:
            await self._progress.abort_paged_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error=f"Discovery policy denied: {decision.reason}",
            )
            return self._policy_denied_result(source_key, run_key, decision)
        fetcher = self._fetchers.get(source.mode)
        if fetcher is None:
            await self._progress.abort_paged_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error="Legistar source has no configured paged fetcher before GET",
            )
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.SKIPPED,
                detail="Legistar source has no configured paged fetcher",
            )
        # Re-run P30's capability at the actual egress boundary.  It is idempotent for a
        # current cursor, but observes the database clock after any run/cursor lock wait.  This
        # protects against a lease that expired while Pacer or the fresh owner/policy checks ran.
        revalidated = await self._prepare(source_key, run_key, lease_token, source)
        if isinstance(revalidated, CatalogRefreshResult):
            return revalidated
        if revalidated.progress.is_terminal:
            # A previous at-least-once activity can finish its recoverable terminal stage while
            # this one waits for Pacer. Promote that durable input rather than issue another GET
            # under a page-specific admission (NFR-8, ADR-003/005).
            return await self._promote_if_current(
                source_key, run_key, lease_token, revalidated, evidence
            )
        return revalidated

    async def _promote_if_current(
        self,
        source_key: str,
        run_key: str,
        lease_token: UUID,
        prepared: _PreparedPage,
        evidence: CatalogRunEvidenceSession,
    ) -> CatalogRefreshResult:
        """Recheck authority before final promotion; a changed source discards its old stage first."""
        source = await self._sources.get(source_key)
        source_result = self._source_preflight_result(source_key, run_key, source)
        if source_result is not None:
            await self._progress.abort_paged_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error=source_result.detail,
            )
            return source_result
        assert source is not None
        if source.source_revision != prepared.progress.source_revision:
            replacement = await self._prepare(source_key, run_key, lease_token, source)
            if isinstance(replacement, CatalogRefreshResult):
                return replacement
            paused = await self._progress.pause_paged_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error="source revision changed before paged promotion",
            )
            if not paused:
                return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.BUSY)
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.DEFERRED,
                detail="source revision changed before paged promotion; stage was reset",
                retry_after_seconds=1.0,
            )
        decision = await self._dispatch_policy()
        if not decision.allowed:
            await self._progress.abort_paged_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error=f"Discovery policy denied: {decision.reason}",
            )
            return self._policy_denied_result(source_key, run_key, decision)
        # Do not abort a terminal stage if its final transaction fails or the worker crashes. The
        # lease eventually becomes reclaimable; the next activity sees the same terminal cursor and
        # retries promotion without another source GET. The concrete promoter rolls back all of its
        # catalog writes on an error, so retaining this normalized stage is the safe recovery path
        # (NFR-8, ADR-001/003).
        publish = await evidence.start_stage("catalog_publish")
        try:
            promotion = await self._promoter.promote_paged_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                source_revision=prepared.progress.source_revision,
            )
        except Exception as exc:
            await publish.finish("failed", error=exc)
            raise
        await publish.finish(
            CatalogRefreshOutcome.SUCCEEDED.value,
            candidate_count=promotion.candidate_count,
            canonical_count=promotion.canonical_count,
        )
        return CatalogRefreshResult(
            source_key,
            run_key,
            CatalogRefreshOutcome.SUCCEEDED,
            candidate_count=promotion.candidate_count,
            canonical_count=promotion.canonical_count,
        )

    async def _rate_limited(
        self,
        source_key: str,
        run_key: str,
        lease_token: UUID,
        request: PacerRequest,
        error: SourceRateLimitedError,
    ) -> CatalogRefreshResult:
        """Share provider backoff, retain completed pages, and let Temporal own the next wait."""
        await self._pacer.observe_backoff(
            request,
            retry_after_seconds=error.retry_after_seconds,
            reset_at=error.reset_at,
        )
        feedback = await self._pacer.acquire(request)
        feedback_delay = feedback.retry_after_seconds if not feedback.granted else 1.0
        retry_after_seconds = max(_source_retry_floor(error, self._now()), feedback_delay)
        paused = await self._progress.pause_paged_refresh(
            source_key,
            run_key,
            lease_token=lease_token,
            error=f"Source rate limited: {error}",
        )
        if not paused:
            return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.BUSY)
        return CatalogRefreshResult(
            source_key,
            run_key,
            CatalogRefreshOutcome.DEFERRED,
            detail=str(error),
            retry_after_seconds=retry_after_seconds,
        )

    async def _preflight(
        self, source_key: str, run_key: str
    ) -> tuple[CatalogSource, CatalogPagedSourceFetcher] | CatalogRefreshResult:
        """Read only an exact reviewed P15b/P15c/P15d/P15e profile before a lease or source call."""
        source = await self._sources.get(source_key)
        source_result = self._source_preflight_result(source_key, run_key, source)
        if source_result is not None:
            return source_result
        assert source is not None
        fetcher = self._fetchers.get(source.mode)
        if fetcher is None:
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.SKIPPED,
                detail="Legistar source has no configured paged fetcher",
            )
        decision = await self._dispatch_policy()
        if not decision.allowed:
            return self._policy_denied_result(source_key, run_key, decision)
        return source, fetcher

    def _source_preflight_result(
        self, source_key: str, run_key: str, source: CatalogSource | None
    ) -> CatalogRefreshResult | None:
        """Fail closed for every source posture that could widen P15b egress (FR-10.3)."""
        if source is None:
            return CatalogRefreshResult(
                source_key, run_key, CatalogRefreshOutcome.SKIPPED, detail="unknown source"
            )
        if not source.is_refreshable_at(self._now()):
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.SKIPPED,
                detail="source is disabled, unreviewed, or expired",
            )
        if not source.handoff_only:
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.SKIPPED,
                detail="registry source is not handoff-only",
            )
        if not source.has_paged_http_get or source.page_limit > _MAX_LEGISTAR_PAGES:
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.SKIPPED,
                detail="source is not approved for the paged catalog workflow",
            )
        return None

    def _lease_seconds_for(self, source: CatalogSource) -> int:
        """Reserve one exact page GET plus its final stage/promotion transaction (NFR-8)."""
        paced_seconds = (source.min_interval_ms + 999) // 1_000
        return min(
            _MAX_CATALOG_REFRESH_LEASE_SECONDS,
            max(self._lease_seconds, paced_seconds + _LEASE_COMPLETION_BUFFER_SECONDS),
        )

    @staticmethod
    def _pacer_request(source: CatalogSource, run_key: str, page_number: int) -> PacerRequest:
        """Scope each Legistar page to its shared API origin, not one city/source key (ADR-005)."""
        return PacerRequest(
            source=Source.PUBLIC_JSONLD,
            quota_scope="catalog-origin:webapi.legistar.com",
            operation=PacerOperation.CATALOG_HTTP_GET,
            queue_item_id=f"{run_key}:get:{page_number}",
            catalog_min_interval_ms=source.min_interval_ms,
        )

    async def _dispatch_policy(self) -> PolicyDecision:
        """Read the discovery gate fail-closed at each P15b egress/promotion boundary."""
        try:
            return await self._policy_gate.evaluate_discovery(
                Source.PUBLIC_JSONLD, Modality.BROWSER
            )
        except Exception:
            return PolicyDecision.deny(
                "discovery policy gate unavailable: fail closed",
                PolicyDecisionCode.POLICY_STORE_UNAVAILABLE,
            )

    @staticmethod
    def _policy_denied_result(
        source_key: str, run_key: str, decision: PolicyDecision
    ) -> CatalogRefreshResult:
        """Project an authority denial without any source egress (FR-3.9/FR-10.1)."""
        return CatalogRefreshResult(
            source_key,
            run_key,
            CatalogRefreshOutcome.SKIPPED,
            detail=f"discovery policy denied: {decision.reason}",
        )

    async def _quarantine_after_access_denied(self, error: SourceAccessDeniedError) -> None:
        """Persist a closed 403 source signal; outage still leaves this run stopped (FR-10.3)."""
        if self._source_quarantine is None:
            return
        try:
            await self._source_quarantine.quarantine(Source.PUBLIC_JSONLD, error.signal)
        except Exception:
            return


def _source_retry_floor(error: SourceRateLimitedError, now: datetime) -> float:
    """Never shorten the provider's explicit throttle if shared Pacer feedback is unavailable."""
    retry_after = error.retry_after_seconds or 0.0
    reset_after = 0.0
    if error.reset_at is not None:
        reset_after = max((error.reset_at - now).total_seconds(), 0.0)
    return max(retry_after, reset_after)
