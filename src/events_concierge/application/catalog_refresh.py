"""Durable, policy-gated refresh of one approved public catalog source (FR-3.1/FR-10.1/10.3/10.4).

This service is intentionally separate from request/feed handling. A caller supplies a stable
``run_key`` (a future Temporal Schedule will use a source/time bucket); the repository lease makes
crash/retry re-entry converge without repeating a completed catalog effect.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from ..domain.catalog_sources import CatalogRefreshCommit, CatalogSource
from ..domain.enums import CatalogRefreshClaimOutcome, CatalogSourceMode, Modality, Source
from ..domain.policy import PolicyDecision, PolicyDecisionCode
from ..ports.catalog_sources import (
    CatalogRefreshCommitter,
    CatalogRunEvidenceRecorder,
    CatalogSourceFetcher,
    CatalogSourceRepository,
)
from ..ports.discovery_policy import DiscoveryPolicyGate
from ..ports.policy import Pacer, PacerOperation, PacerRequest, SourceQuarantinePort
from ..ports.sources import (
    SourceAccessDeniedError,
    SourceRateLimitedError,
    SourceTransientError,
)
from .catalog_run_evidence import CatalogRunEvidenceSession

_MAX_CATALOG_REFRESH_LEASE_SECONDS = 3_600
_LEASE_COMPLETION_BUFFER_SECONDS = 60
_BIBLIOCOMMONS_MAX_ITEMS_PER_PAGE = 25
_BIBLIOCOMMONS_PERSISTENCE_BUDGET_MS_PER_EVENT = 250
#: Both Luma modes issue one further paced detail GET per retained event, so their worst case is
#: pages *plus* the events those pages can carry -- not pages alone.
_LUMA_DETAIL_MODES = frozenset(
    {CatalogSourceMode.LUMA_DISCOVER_JSON, CatalogSourceMode.LUMA_CALENDAR_JSON}
)
_LUMA_MAX_ITEMS_PER_PAGE = 25


class CatalogRefreshOutcome(StrEnum):
    """Non-exceptional result of a refresh attempt or a durable refresh launch."""

    SUCCEEDED = "succeeded"
    QUEUED = "queued"
    SKIPPED = "skipped"
    BUSY = "busy"
    ALREADY_SUCCEEDED = "already_succeeded"
    DEFERRED = "deferred"
    PROGRESSED = "progressed"


@dataclass(frozen=True, slots=True)
class CatalogRefreshResult:
    """Counts and reason suitable for a worker log or schedule monitor (NFR-1)."""

    source_key: str
    run_key: str
    outcome: CatalogRefreshOutcome
    candidate_count: int = 0
    canonical_count: int = 0
    detail: str = ""
    retry_after_seconds: float | None = None


class CatalogRefreshService:
    """Refresh approved sources off the user request path under a durable lease (NFR-8).

    All current reviewed catalog modes are public JSON-LD/browser discovery.  The source-policy
    gate and owner-reviewed source row are each evaluated once before any Pacer claim and again
    immediately before `fetch`. The final database-clock lease capability also runs at that boundary:
    when it observes lost authority, the source fetch does not start (FR-3.9/FR-10.1/10.3, AC-23,
    NFR-8).
    """

    def __init__(
        self,
        sources: CatalogSourceRepository,
        committer: CatalogRefreshCommitter,
        fetchers: dict[CatalogSourceMode, CatalogSourceFetcher],
        pacer: Pacer,
        policy_gate: DiscoveryPolicyGate,
        source_quarantine: SourceQuarantinePort | None = None,
        run_evidence: CatalogRunEvidenceRecorder | None = None,
        *,
        lease_seconds: int = 300,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("catalog refresh lease_seconds must be positive")
        self._sources = sources
        self._committer = committer
        self._fetchers = dict(fetchers)
        self._pacer = pacer
        self._policy_gate = policy_gate
        self._source_quarantine = source_quarantine
        self._run_evidence = run_evidence
        self._lease_seconds = lease_seconds
        self._now = now or (lambda: datetime.now(UTC))

    async def refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        require_single_http_get: bool = False,
    ) -> CatalogRefreshResult:
        """Fetch, normalize, and persist one approved source exactly once per durable run key.

        ``require_single_http_get`` is P15a's narrow Temporal-continuation fence.  Only LibCal's
        reviewed one-document source may use it: a Pacer defer can then release this durable run,
        sleep in Temporal, and retry the same run key without losing pagination state.  Its direct
        path is refused, so a process-local/manual caller can never bypass that timer or its shared
        Pacer admission. Paged and redirecting sources stay on the legacy one-shot path until their
        source-specific durable staging/cursor profile is reviewed (FR-10.3/10.4, NFR-8, ADR-003/005).
        """
        evidence = CatalogRunEvidenceSession(
            self._run_evidence,
            source_key,
            run_key,
            # P15a executes in a shared Temporal activity worker. Direct legacy refreshes are
            # processed sequentially by the command worker and may expose explicitly qualified
            # process boundary samples.
            include_process_metrics=not require_single_http_get,
        )
        try:
            result = await self._refresh_observed(
                source_key,
                run_key,
                require_single_http_get=require_single_http_get,
                evidence=evidence,
            )
        except Exception:
            await evidence.finish("failed")
            raise
        await evidence.finish(result.outcome.value)
        return result

    async def _refresh_observed(
        self,
        source_key: str,
        run_key: str,
        *,
        require_single_http_get: bool,
        evidence: CatalogRunEvidenceSession,
    ) -> CatalogRefreshResult:
        """Execute the guarded refresh while recording only typed stage timings."""
        admission = evidence.start_stage("admission")
        now = self._now()
        prepared = await self._preflight(
            source_key,
            run_key,
            now,
            require_single_http_get=require_single_http_get,
        )
        if isinstance(prepared, CatalogRefreshResult):
            await admission.finish(prepared.outcome.value)
            return prepared
        source, fetcher = prepared

        lease_seconds = self._lease_seconds_for(source)
        if lease_seconds is None:
            await admission.finish(CatalogRefreshOutcome.SKIPPED.value)
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.SKIPPED,
                detail="source pacing and page cap exceed the maximum catalog refresh lease",
            )
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
            source_key,
            run_key,
            source,
            fetcher,
            claim.lease_token,
            lease_seconds,
            evidence,
            require_single_http_get=require_single_http_get,
        )

    async def _refresh_claimed(
        self,
        source_key: str,
        run_key: str,
        source: CatalogSource,
        fetcher: CatalogSourceFetcher,
        lease_token: UUID,
        claimed_lease_seconds: int,
        evidence: CatalogRunEvidenceSession,
        *,
        require_single_http_get: bool,
    ) -> CatalogRefreshResult:
        """Execute one already-claimed refresh while preserving its source/policy fences."""

        admitted_source = source
        pacer_request = self._pacer_request(
            source,
            run_key,
            require_single_http_get=require_single_http_get,
        )
        lease = await self._pacer.acquire(pacer_request)
        if not lease.granted:
            return await self._pause_for_pacer(
                source_key,
                run_key,
                lease_token,
                status=lease.status.value,
                detail=lease.detail,
                retry_after_seconds=lease.retry_after_seconds,
            )

        # Re-read after the potentially delayed Pacer acquisition and immediately before the only
        # external source call. The registry is the egress authority, so a mid-lease owner change
        # must be just as fail-closed as the policy re-read (FR-10.3, AC-23).
        refreshed = await self._recheck_source_after_pacer(
            source_key,
            run_key,
            lease_token,
            require_single_http_get=require_single_http_get,
        )
        if isinstance(refreshed, CatalogRefreshResult):
            return refreshed
        source, fetcher = refreshed
        pre_fetch_result: CatalogRefreshResult | None
        if require_single_http_get and _single_http_get_contract_changed(admitted_source, source):
            pre_fetch_result = await self._release_after_single_get_contract_change(
                source_key,
                run_key,
                lease_token,
            )
        else:
            pre_fetch_result = await self._release_if_source_exceeds_claimed_lease(
                source_key,
                run_key,
                lease_token,
                source,
                claimed_lease_seconds,
            )
        if pre_fetch_result is not None:
            return pre_fetch_result

        dispatch_policy = await self._dispatch_policy()
        if not dispatch_policy.allowed:
            await self._sources.fail_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error=f"Discovery policy denied: {dispatch_policy.reason}",
            )
            return self._policy_denied_result(source_key, run_key, dispatch_policy)

        authority_result = await self._live_lease_result(source_key, run_key, lease_token)
        if authority_result is not None:
            return authority_result

        return await self._fetch_and_commit(
            source_key,
            run_key,
            source,
            fetcher,
            lease_token,
            pacer_request,
            evidence,
        )

    async def _pause_for_pacer(
        self,
        source_key: str,
        run_key: str,
        lease_token: UUID,
        *,
        status: str,
        detail: str,
        retry_after_seconds: float,
    ) -> CatalogRefreshResult:
        """Pause one no-egress claim only while its exact database lease remains live."""
        # The durable source-run claim must not remain RUNNING while a non-reserved Pacer
        # projection expires. Pause it for an explicit/scheduled retry; no worker sleeps, no
        # catalog wire call is made, and admin reliability metrics do not misclassify ordinary
        # throttle coordination as a provider failure (ADR-005, NFR-8).
        paused = await self._sources.pause_refresh(
            source_key,
            run_key,
            lease_token=lease_token,
            error=f"Pacer {status}: {detail}",
        )
        if not paused:
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.BUSY,
                detail="catalog refresh lease was lost before the Pacer defer could be recorded",
            )
        return CatalogRefreshResult(
            source_key,
            run_key,
            CatalogRefreshOutcome.DEFERRED,
            detail=detail,
            retry_after_seconds=retry_after_seconds,
        )

    async def _fetch_and_commit(
        self,
        source_key: str,
        run_key: str,
        source: CatalogSource,
        fetcher: CatalogSourceFetcher,
        lease_token: UUID,
        pacer_request: PacerRequest,
        evidence: CatalogRunEvidenceSession,
    ) -> CatalogRefreshResult:
        """Issue the already-authorized source fetch and atomically publish its normalized result."""

        # The provider adapter owns transport and response parsing as one call.  Measure that real
        # adapter boundary as collection; the admin projection labels extraction/enrichment as
        # included instead of fabricating an independent duration.
        collect = evidence.start_stage("collect")
        try:
            candidates = await fetcher.fetch(source)
        except (SourceRateLimitedError, SourceAccessDeniedError) as source_error:
            await collect.finish(CatalogRefreshOutcome.DEFERRED.value)
            return await self._source_dispatch_error(
                source_key,
                run_key,
                lease_token,
                pacer_request,
                source_error,
            )
        except SourceTransientError as source_error:
            await collect.finish(CatalogRefreshOutcome.DEFERRED.value)
            await self._sources.fail_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error=f"Source transient failure: {source_error}",
            )
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.DEFERRED,
                detail=str(source_error),
                retry_after_seconds=source_error.retry_after_seconds,
            )
        except Exception as exc:
            await collect.finish("failed")
            await self._sources.fail_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error=str(exc),
            )
            raise
        await collect.finish(CatalogRefreshOutcome.SUCCEEDED.value)

        # Normalization/deduplication and publication share one lease-fenced commit capability.
        # Time the observable commit boundary as catalog publication and expose the inseparable
        # normalize/dedupe phase honestly in the structured stage projection.
        publish = evidence.start_stage("catalog_publish")
        try:
            committed = await self._committer.commit_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                candidates=candidates,
            )
        except Exception as exc:
            await publish.finish("failed")
            await self._sources.fail_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error=str(exc),
            )
            raise
        commit_outcome = (
            CatalogRefreshOutcome.SUCCEEDED if committed is not None else CatalogRefreshOutcome.BUSY
        )
        await publish.finish(commit_outcome.value)

        return self._commit_result(source_key, run_key, committed)

    async def _live_lease_result(
        self,
        source_key: str,
        run_key: str,
        lease_token: UUID,
    ) -> CatalogRefreshResult | None:
        """Fail closed when the final database-clock generic-fetch check loses authority."""
        try:
            live = await self._sources.has_live_refresh_lease(
                source_key,
                run_key,
                lease_token=lease_token,
            )
        except Exception:
            return await self._release_after_source_recheck(
                source_key,
                run_key,
                lease_token,
                detail="catalog refresh lease authority unavailable before fetch",
                error="catalog refresh lease authority unavailable before fetch",
            )
        if not live:
            return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.BUSY)
        return None

    @staticmethod
    def _commit_result(
        source_key: str,
        run_key: str,
        committed: CatalogRefreshCommit | None,
    ) -> CatalogRefreshResult:
        """Project an all-or-nothing publication result without retrying a rejected lease."""
        if committed is None:
            return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.BUSY)
        return CatalogRefreshResult(
            source_key,
            run_key,
            CatalogRefreshOutcome.SUCCEEDED,
            candidate_count=committed.candidate_count,
            canonical_count=committed.canonical_count,
        )

    def _lease_seconds_for(self, source: CatalogSource) -> int | None:
        """Reserve enough time for every bounded, source-paced request plus final persistence."""
        return catalog_refresh_lease_seconds(source, floor_seconds=self._lease_seconds)


    async def _release_if_source_exceeds_claimed_lease(
        self,
        source_key: str,
        run_key: str,
        lease_token: UUID,
        source: CatalogSource,
        claimed_lease_seconds: int,
    ) -> CatalogRefreshResult | None:
        """Release rather than egress when a post-Pacer registry edit outgrows its lease."""
        refreshed_lease_seconds = self._lease_seconds_for(source)
        if refreshed_lease_seconds is None:
            return await self._release_after_source_recheck(
                source_key,
                run_key,
                lease_token,
                detail="source pacing and page cap exceed the maximum catalog refresh lease before fetch",
                error="catalog source pacing and page cap exceed the maximum refresh lease",
            )
        if refreshed_lease_seconds > claimed_lease_seconds:
            return await self._release_after_source_recheck(
                source_key,
                run_key,
                lease_token,
                detail="source pacing and page cap changed beyond the claimed refresh lease",
                error="catalog source pacing and page cap changed beyond the claimed refresh lease",
            )
        return None

    async def _preflight(
        self,
        source_key: str,
        run_key: str,
        now: datetime,
        *,
        require_single_http_get: bool,
    ) -> tuple[CatalogSource, CatalogSourceFetcher] | CatalogRefreshResult:
        """Return only a reviewed handoff-only source and its adapter before a network lease (FR-10.3)."""
        source = await self._sources.get(source_key)
        if source is None:
            return CatalogRefreshResult(
                source_key, run_key, CatalogRefreshOutcome.SKIPPED, detail="unknown source"
            )
        if not source.is_refreshable_at(now):
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.SKIPPED,
                detail="source is disabled, unreviewed, or expired",
            )
        source_posture_error: str | None = None
        if not source.handoff_only:
            source_posture_error = "registry source is not handoff-only"
        elif require_single_http_get and not source.has_single_http_get:
            source_posture_error = "source is not approved for the single-GET catalog workflow"
        elif not require_single_http_get and source.has_single_http_get:
            source_posture_error = "source requires the single-GET catalog workflow"
        elif not require_single_http_get and source.has_paged_http_get:
            source_posture_error = "source requires the paged catalog workflow"
        if source_posture_error is not None:
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.SKIPPED,
                detail=source_posture_error,
            )
        fetcher = self._fetchers.get(source.mode)
        if fetcher is None:
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.SKIPPED,
                detail=f"no fetcher for {source.mode.value}",
            )
        dispatch_policy = await self._dispatch_policy()
        if not dispatch_policy.allowed:
            return self._policy_denied_result(source_key, run_key, dispatch_policy)
        return source, fetcher

    async def _recheck_source_after_pacer(
        self,
        source_key: str,
        run_key: str,
        lease_token: UUID,
        *,
        require_single_http_get: bool,
    ) -> tuple[CatalogSource, CatalogSourceFetcher] | CatalogRefreshResult:
        """Fence the only source call against a registry edit made while the Pacer was pending.

        The database capability grants the durable lease only to a currently reviewed source, but
        an owner can still intentionally disable or revise it after that claim.  Re-reading here
        ensures the updated seed/origin/mode is either authoritative for this fetch or prevents it
        altogether (FR-10.3, NFR-8).
        """
        try:
            source = await self._sources.get(source_key)
        except Exception:
            return await self._release_after_source_recheck(
                source_key,
                run_key,
                lease_token,
                detail="source registry unavailable",
                error="catalog source registry unavailable before fetch",
            )
        if source is None:
            return await self._release_after_source_recheck(
                source_key,
                run_key,
                lease_token,
                detail="source removed from registry",
                error="catalog source removed before fetch",
            )
        if not source.is_refreshable_at(self._now()):
            return await self._release_after_source_recheck(
                source_key,
                run_key,
                lease_token,
                detail="source disabled, unreviewed, or expired before fetch",
                error="catalog source no longer refreshable before fetch",
            )
        source_posture_error: tuple[str, str] | None = None
        if not source.handoff_only:
            source_posture_error = (
                "registry source is not handoff-only before fetch",
                "catalog source no longer handoff-only before fetch",
            )
        elif require_single_http_get and not source.has_single_http_get:
            source_posture_error = (
                "source is no longer approved for the single-GET catalog workflow",
                "catalog source no longer approved for single-GET catalog workflow",
            )
        elif not require_single_http_get and source.has_single_http_get:
            source_posture_error = (
                "source requires the single-GET catalog workflow before fetch",
                "catalog source requires the single-GET catalog workflow before fetch",
            )
        elif not require_single_http_get and source.has_paged_http_get:
            source_posture_error = (
                "source requires the paged catalog workflow before fetch",
                "catalog source requires the paged catalog workflow before fetch",
            )
        if source_posture_error is not None:
            return await self._release_after_source_recheck(
                source_key,
                run_key,
                lease_token,
                detail=source_posture_error[0],
                error=source_posture_error[1],
            )
        fetcher = self._fetchers.get(source.mode)
        if fetcher is None:
            return await self._release_after_source_recheck(
                source_key,
                run_key,
                lease_token,
                detail=f"no fetcher for {source.mode.value}",
                error="catalog source mode has no configured fetcher before fetch",
            )
        return source, fetcher

    @staticmethod
    def _pacer_request(
        source: CatalogSource,
        run_key: str,
        *,
        require_single_http_get: bool,
    ) -> PacerRequest:
        """Create the source-call admission immediately preceding P15a's one physical GET.

        The LibCal source has a single closed request, so its source key is the complete shared
        quota scope for this slice.  P15b will introduce origin-level progress/staging for modes
        that can issue more than one physical request (FR-10.4, ADR-005).
        """
        if require_single_http_get:
            return PacerRequest(
                source=Source.PUBLIC_JSONLD,
                quota_scope=f"catalog:{source.source_key}",
                operation=PacerOperation.CATALOG_HTTP_GET,
                queue_item_id=f"{run_key}:get:0",
                catalog_min_interval_ms=source.min_interval_ms,
            )
        return PacerRequest(
            source=Source.PUBLIC_JSONLD,
            quota_scope=f"catalog:{source.source_key}",
            operation=PacerOperation.CATALOG_REFRESH,
            queue_item_id=run_key,
        )

    async def _release_after_source_recheck(
        self,
        source_key: str,
        run_key: str,
        lease_token: UUID,
        *,
        detail: str,
        error: str,
    ) -> CatalogRefreshResult:
        """Release a lease without a wire call when its source authority disappears."""
        await self._sources.fail_refresh(
            source_key,
            run_key,
            lease_token=lease_token,
            error=error,
        )
        return CatalogRefreshResult(
            source_key, run_key, CatalogRefreshOutcome.SKIPPED, detail=detail
        )

    async def _release_after_single_get_contract_change(
        self,
        source_key: str,
        run_key: str,
        lease_token: UUID,
    ) -> CatalogRefreshResult:
        """Reclaim after a P15a request contract edit instead of using a stale admission.

        The previous token was minted from an earlier reviewed interval/end-point contract.  A
        short durable timer frees the activity and lets the next pass re-read the registry, mint a
        fresh shared-Pacer request, and only then make LibCal's sole GET (FR-10.3/10.4, NFR-8,
        ADR-003/005).
        """
        await self._sources.fail_refresh(
            source_key,
            run_key,
            lease_token=lease_token,
            error="single-GET catalog request contract changed while awaiting Pacer",
        )
        return CatalogRefreshResult(
            source_key,
            run_key,
            CatalogRefreshOutcome.DEFERRED,
            detail="single-GET catalog request contract changed; retry with a fresh Pacer admission",
            retry_after_seconds=1.0,
        )

    async def _dispatch_policy(self) -> PolicyDecision:
        """Read the public-catalog discovery policy, treating any injected-gate error as DENY."""
        try:
            return await self._policy_gate.evaluate_discovery(
                Source.PUBLIC_JSONLD, Modality.BROWSER
            )
        except Exception:
            return PolicyDecision.deny(
                "discovery policy gate unavailable: fail closed",
                PolicyDecisionCode.POLICY_STORE_UNAVAILABLE,
            )

    async def _source_dispatch_error(
        self,
        source_key: str,
        run_key: str,
        lease_token: UUID,
        pacer_request: PacerRequest,
        error: SourceRateLimitedError | SourceAccessDeniedError,
    ) -> CatalogRefreshResult:
        """Release one durable run safely after a typed source boundary signal."""
        if isinstance(error, SourceRateLimitedError):
            # A normalized source 429/reset is shared with every worker before this durable claim
            # is relinquished. The next scheduled/manual retry re-acquires rather than assuming a
            # stale projection is a reservation (FR-10.4, AC-73, NFR-8).
            await self._pacer.observe_backoff(
                pacer_request,
                retry_after_seconds=error.retry_after_seconds,
                reset_at=error.reset_at,
            )
            feedback = await self._pacer.acquire(pacer_request)
            feedback_delay = feedback.retry_after_seconds if not feedback.granted else 1.0
            retry_after_seconds = max(_source_retry_floor(error, self._now()), feedback_delay)
            await self._sources.fail_refresh(
                source_key,
                run_key,
                lease_token=lease_token,
                error=f"Source rate limited: {error}",
            )
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.DEFERRED,
                detail=str(error),
                retry_after_seconds=retry_after_seconds,
            )

        await self._quarantine_after_access_denied(Source.PUBLIC_JSONLD, error)
        await self._sources.fail_refresh(
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

    @staticmethod
    def _policy_denied_result(
        source_key: str, run_key: str, decision: PolicyDecision
    ) -> CatalogRefreshResult:
        """Project a policy denial without issuing a source request (FR-3.9/FR-10.1)."""
        return CatalogRefreshResult(
            source_key,
            run_key,
            CatalogRefreshOutcome.SKIPPED,
            detail=f"discovery policy denied: {decision.reason}",
        )

    async def _quarantine_after_access_denied(
        self, source: Source, denied: SourceAccessDeniedError
    ) -> None:
        """Make a raw discovery ban/403 durable without storing provider response data."""
        actuator = self._source_quarantine
        if actuator is None:
            return
        try:
            await actuator.quarantine(source, denied.signal)
        except Exception:
            # The run still stops immediately. A policy-store outage independently makes future
            # dispatch checks fail closed, so an actuator failure never becomes a provider retry.
            return


def _source_retry_floor(error: SourceRateLimitedError, now: datetime) -> float:
    """Retain the provider's explicit throttle even if a shared-Pacer update was unavailable.

    Redis backoff feedback is best effort during an outage, but P15a's durable Temporal timer must
    never turn an acknowledged provider ``Retry-After`` or reset into an earlier retry.  The next
    activity still acquires the shared Pacer immediately before egress (FR-10.4, AC-73, ADR-005).
    """
    retry_after = error.retry_after_seconds or 0.0
    reset_after = 0.0
    if error.reset_at is not None:
        reset_after = max((error.reset_at - now).total_seconds(), 0.0)
    return max(retry_after, reset_after)


def _single_http_get_contract_changed(before: CatalogSource, after: CatalogSource) -> bool:
    """Reject a P15a egress if its reviewed request/pacing contract changed after admission.

    All compared fields shape either the one exact LibCal request or the shared token budget.  A
    display-label/editorial update does not matter, but a new URL/origin, mode, page contract, or
    interval must be admitted again before a wire call (FR-10.3/10.4, ADR-005).
    """
    return (
        before.seed_url,
        before.approved_origins,
        before.mode,
        before.page_limit,
        before.min_interval_ms,
    ) != (
        after.seed_url,
        after.approved_origins,
        after.mode,
        after.page_limit,
        after.min_interval_ms,
    )


def catalog_refresh_lease_seconds(source: CatalogSource, *, floor_seconds: int) -> int | None:
    """Return the lease one refresh of ``source`` needs, or None when it cannot fit in one.

    ``page_limit`` is the reviewed upper bound on one source refresh's pagination units.  A source
    that has to make that many same-host reads cannot safely inherit a shorter process-default
    lease: otherwise a correct, deliberately slow fetch loses ownership before its single catalog
    write.  The database capability limits a lease to one hour, so an over-budget registry row
    fails closed before Pacer or network activity (FR-10.3/NFR-8).

    Pages are not the whole request count for every mode.  The Luma modes follow their listing with
    one paced detail GET per retained event, so their reservation counts the events their reviewed
    page cap can carry as well.

    This is a module-level function so the registry can be checked against it directly: a row whose
    reservation returns None is SKIPPED with no run recorded, which is coverage disappearing with
    no failure to look at.
    """
    request_units = source.page_limit
    if source.mode in _LUMA_DETAIL_MODES:
        # Every retained event costs one further same-host detail GET at the same cadence floor.
        # Reserving pages alone left these sources relying on the process-default lease, which a
        # large calendar outgrows silently: the fetch keeps running, the lease expires, and the
        # commit is fenced out after all the egress has already happened.
        request_units += source.page_limit * _LUMA_MAX_ITEMS_PER_PAGE
    paced_seconds = (request_units * source.min_interval_ms + 999) // 1_000
    persistence_seconds = 0
    if source.mode is CatalogSourceMode.BIBLIOCOMMONS_RSS:
        # These reviewed feeds can publish thousands of rows. Their atomic catalog merge is
        # intentionally fenced by the same lease as fetch, so reserve a bounded per-event
        # persistence allowance in addition to the worst-case page pacing.
        persistence_milliseconds = (
            source.page_limit
            * _BIBLIOCOMMONS_MAX_ITEMS_PER_PAGE
            * _BIBLIOCOMMONS_PERSISTENCE_BUDGET_MS_PER_EVENT
        )
        persistence_seconds = (persistence_milliseconds + 999) // 1_000
    required_seconds = paced_seconds + persistence_seconds + _LEASE_COMPLETION_BUFFER_SECONDS
    lease_seconds = max(floor_seconds, required_seconds)
    if lease_seconds > _MAX_CATALOG_REFRESH_LEASE_SECONDS:
        return None
    return lease_seconds
