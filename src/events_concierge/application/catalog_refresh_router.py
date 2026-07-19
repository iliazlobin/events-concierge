"""Route reviewed catalog runs to the execution spine safe for their request contract.

P15a owns LibCal's one closed document and P15b/P15c/P15d/P15e own reviewed Legistar persisted
page cursors.
Both use Temporal continuations so a denied shared-Pacer admission becomes a durable timer rather
than a process-local retry. Other reviewed modes retain the bounded refresh service until they have
their own cursor/staging profile (FR-10.3/10.4, NFR-8, ADR-003/005).
"""

from __future__ import annotations

from typing import Protocol

from ..ports.catalog_sources import CatalogSourceRepository
from ..ports.workflows import CatalogPagedRefreshWorkflowStarter, CatalogRefreshWorkflowStarter
from .catalog_refresh import CatalogRefreshOutcome, CatalogRefreshResult


class CatalogRefreshDirectRunner(Protocol):
    """Execute a legacy bounded refresh after the router selected its safe execution mode."""

    async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
        """Refresh one non-P15a source/run through the existing guarded service."""
        ...


class CatalogRefreshRouter:
    """Select Temporal-only P15a/P15b/P15c/P15d/P15e execution without an unsafe direct fallback.

    A missing starter is an explicit safe skip rather than a call to the direct runner.  The
    activity repeats the source/registry/policy checks before egress, so this routing decision is
    only an outer dispatch choice and never an authorization grant (FR-10.3, NFR-8, ADR-003/005).
    """

    def __init__(
        self,
        sources: CatalogSourceRepository,
        direct_runner: CatalogRefreshDirectRunner,
        single_get_starter: CatalogRefreshWorkflowStarter | None,
        paged_starter: CatalogPagedRefreshWorkflowStarter | None = None,
    ) -> None:
        self._sources = sources
        self._direct_runner = direct_runner
        self._single_get_starter = single_get_starter
        self._paged_starter = paged_starter

    async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
        """Queue P15a/P15b/P15c/P15d/P15e sources or execute only a legacy source.

        Repository uncertainty propagates rather than being treated as a non-P15a classification;
        callers must never turn a failed routing read into an unpaced direct source call.
        """
        source = await self._sources.get(source_key)
        if source is None:
            return await self._direct_runner.refresh(source_key, run_key)
        if source.has_single_http_get:
            starter = self._single_get_starter
            if starter is None:
                return CatalogRefreshResult(
                    source_key,
                    run_key,
                    CatalogRefreshOutcome.SKIPPED,
                    detail="single-GET catalog workflow starter is unavailable",
                )
            await starter.start(source_key, run_key)
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.QUEUED,
                detail="single-GET catalog workflow queued",
            )
        if source.has_paged_http_get:
            starter = self._paged_starter
            if starter is None:
                return CatalogRefreshResult(
                    source_key,
                    run_key,
                    CatalogRefreshOutcome.SKIPPED,
                    detail="paged catalog workflow starter is unavailable",
                )
            await starter.start(source_key, run_key)
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.QUEUED,
                detail="paged catalog workflow queued",
            )
        return await self._direct_runner.refresh(source_key, run_key)
