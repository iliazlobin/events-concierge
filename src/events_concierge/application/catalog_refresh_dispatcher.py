"""Bounded, schedule-compatible dispatch of reviewed catalog refresh slots.

The dispatcher deliberately owns no HTTP/client behavior. It reads due source slots from the
tenant-neutral control plane and invokes an already-routed refresh boundary once per source. P15a
queues its one-GET source into Temporal while legacy modes retain the existing lease/Pacer/policy
service. A future Temporal Schedule or deployment scheduler can call the same one-shot operation
without changing its idempotency semantics (FR-3.3/FR-3.9, NFR-1/NFR-8, ADR-001/003/004/005).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from ..ports.catalog_sources import CatalogRefreshDueReader
from .catalog_refresh import CatalogRefreshOutcome, CatalogRefreshResult


class CatalogRefreshRunner(Protocol):
    """The already-authoritative routed one-source operation used by scheduled dispatch."""

    async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
        """Refresh or queue one source/run-key through its safe durable boundary."""
        ...


@dataclass(frozen=True, slots=True)
class CatalogCadenceFailure:
    """A PII-free dispatcher failure projection; the refresh service owns durable error detail."""

    source_key: str
    run_key: str
    error_type: str


@dataclass(frozen=True, slots=True)
class CatalogCadenceDispatchReport:
    """Aggregate outcome of one bounded cadence pass, suitable for schedule-worker observability."""

    due_sources: int
    results: tuple[CatalogRefreshResult, ...]
    failures: tuple[CatalogCadenceFailure, ...]

    @property
    def attempted(self) -> int:
        """Count source slots handed to the authoritative refresh boundary."""
        return len(self.results) + len(self.failures)

    def outcome_count(self, outcome: CatalogRefreshOutcome) -> int:
        """Return an explicit result count without interpreting nonterminal retry timing."""
        return sum(result.outcome is outcome for result in self.results)


class CatalogCadenceDispatcher:
    """Dispatch due public-catalog slots sequentially and with a fixed per-pass bound.

    This is intentionally a one-shot application service. It neither sleeps nor creates a
    recurring external schedule, so a deployment must explicitly choose how/when to call it.
    The source-policy gate remains inside `CatalogRefreshService` and is re-read immediately
    before every potential source call; P15a's router makes the one-GET mode Temporal-only
    before the service is reached (ADR-003/004/005).
    """

    def __init__(
        self,
        due_sources: CatalogRefreshDueReader,
        refresh: CatalogRefreshRunner,
        *,
        batch_size: int = 50,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if batch_size <= 0:
            raise ValueError("catalog cadence batch_size must be positive")
        self._due_sources = due_sources
        self._refresh = refresh
        self._batch_size = batch_size
        self._now = now or (lambda: datetime.now(UTC))

    async def dispatch_once(self) -> CatalogCadenceDispatchReport:
        """Invoke one deterministic run key for every due source selected in this bounded pass."""
        now = _utc(self._now())
        due = await self._due_sources.list_due_refreshes(now, limit=self._batch_size)
        ordered_due = sorted(due, key=lambda item: (item.due_at, item.source.source_key))
        results: list[CatalogRefreshResult] = []
        failures: list[CatalogCadenceFailure] = []
        for item in ordered_due:
            run_key = item.run_key()
            try:
                results.append(await self._refresh.refresh(item.source.source_key, run_key))
            except Exception as error:
                # The source service records a failed claim before re-raising. Preserve only an
                # exception class here: exception strings can contain unreviewed provider text.
                failures.append(
                    CatalogCadenceFailure(
                        source_key=item.source.source_key,
                        run_key=run_key,
                        error_type=type(error).__name__,
                    )
                )
        return CatalogCadenceDispatchReport(
            due_sources=len(ordered_due),
            results=tuple(results),
            failures=tuple(failures),
        )


def _utc(value: datetime) -> datetime:
    """Reject a naive scheduling clock rather than giving a source an ambiguous interval slot."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("catalog cadence clock must return a timezone-aware datetime")
    return value.astimezone(UTC)
