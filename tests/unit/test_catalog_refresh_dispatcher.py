"""Bounded catalog-cadence dispatch contracts (NFR-1/NFR-8, ADR-001/004)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from events_concierge.application.catalog_refresh import (
    CatalogRefreshOutcome,
    CatalogRefreshResult,
)
from events_concierge.application.catalog_refresh_dispatcher import CatalogCadenceDispatcher
from events_concierge.domain.catalog_sources import CatalogRefreshDue, CatalogSource
from events_concierge.domain.enums import CatalogSourceMode


class _DueReader:
    """Fixture projection of the repository's bounded, due-ordered query."""

    def __init__(self, due: list[CatalogRefreshDue]) -> None:
        self._due = due
        self.calls: list[tuple[datetime, int]] = []

    async def list_due_refreshes(self, now: datetime, *, limit: int) -> list[CatalogRefreshDue]:
        self.calls.append((now, limit))
        return list(self._due[:limit])


class _Runner:
    """Record only dispatcher invocations; source/Pacer/policy behavior belongs to its service tests."""

    def __init__(
        self,
        outcomes: dict[str, CatalogRefreshOutcome] | None = None,
        failures: set[str] | None = None,
    ) -> None:
        self._outcomes = outcomes or {}
        self._failures = failures or set()
        self.calls: list[tuple[str, str]] = []

    async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
        self.calls.append((source_key, run_key))
        if source_key in self._failures:
            raise RuntimeError("private provider response must not enter dispatcher logs")
        return CatalogRefreshResult(
            source_key=source_key,
            run_key=run_key,
            outcome=self._outcomes.get(source_key, CatalogRefreshOutcome.SUCCEEDED),
        )


def _source(source_key: str, now: datetime) -> CatalogSource:
    """Make one reviewed public source with a stable cadence slot."""
    return CatalogSource(
        source_key=source_key,
        display_name=f"{source_key} events",
        publisher="Tests",
        seed_url=f"https://{source_key}.example.test/events",
        approved_origins=(f"https://{source_key}.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=now - timedelta(days=1),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1_500,
    )


def _due(source_key: str, due_at: datetime) -> CatalogRefreshDue:
    """Make a slot whose last success is exactly one cadence interval earlier."""
    source = _source(source_key, due_at)
    return CatalogRefreshDue(
        source=source,
        due_at=due_at,
        last_succeeded_at=due_at - timedelta(minutes=source.refresh_interval_minutes),
    )


async def test_dispatcher_orders_due_slots_and_uses_stable_source_slot_keys() -> None:
    """A pass is deterministic even if a repository fixture arrives out of order (NFR-8)."""
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    alpha = _due("alpha-source", now - timedelta(minutes=30))
    zulu = _due("zulu-source", now - timedelta(minutes=30))
    middle = _due("middle-source", now - timedelta(minutes=15))
    reader = _DueReader([zulu, middle, alpha])
    runner = _Runner(
        {
            "alpha-source": CatalogRefreshOutcome.SUCCEEDED,
            "zulu-source": CatalogRefreshOutcome.DEFERRED,
            "middle-source": CatalogRefreshOutcome.SKIPPED,
        }
    )

    report = await CatalogCadenceDispatcher(reader, runner, now=lambda: now).dispatch_once()

    assert reader.calls == [(now, 50)]
    assert runner.calls == [
        ("alpha-source", alpha.run_key()),
        ("zulu-source", zulu.run_key()),
        ("middle-source", middle.run_key()),
    ]
    assert report.due_sources == 3
    assert report.attempted == 3
    assert report.outcome_count(CatalogRefreshOutcome.SUCCEEDED) == 1
    assert report.outcome_count(CatalogRefreshOutcome.DEFERRED) == 1
    assert report.outcome_count(CatalogRefreshOutcome.SKIPPED) == 1
    assert report.failures == ()


async def test_dispatcher_enforces_its_repository_batch_boundary() -> None:
    """An operator-configured pass cannot hand more than its bound to source refresh (NFR-1)."""
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    due = [
        _due("first-source", now - timedelta(minutes=3)),
        _due("second-source", now - timedelta(minutes=2)),
        _due("third-source", now - timedelta(minutes=1)),
    ]
    reader = _DueReader(due)
    runner = _Runner()

    report = await CatalogCadenceDispatcher(
        reader, runner, batch_size=2, now=lambda: now
    ).dispatch_once()

    assert reader.calls == [(now, 2)]
    assert [source_key for source_key, _ in runner.calls] == [
        "first-source",
        "second-source",
    ]
    assert report.due_sources == 2
    assert report.attempted == 2


async def test_dispatcher_continues_after_one_failure_without_exposing_error_text() -> None:
    """One malformed source outcome cannot block later due sources or leak provider content."""
    now = datetime(2026, 7, 18, 12, 0, tzinfo=UTC)
    broken = _due("broken-source", now - timedelta(minutes=2))
    healthy = _due("healthy-source", now - timedelta(minutes=1))
    reader = _DueReader([broken, healthy])
    runner = _Runner(failures={"broken-source"})

    report = await CatalogCadenceDispatcher(reader, runner, now=lambda: now).dispatch_once()

    assert [source_key for source_key, _ in runner.calls] == ["broken-source", "healthy-source"]
    assert report.attempted == 2
    assert report.outcome_count(CatalogRefreshOutcome.SUCCEEDED) == 1
    assert len(report.failures) == 1
    assert report.failures[0].source_key == "broken-source"
    assert report.failures[0].error_type == "RuntimeError"
    assert "private provider response" not in repr(report.failures)


async def test_dispatcher_rejects_an_ambiguous_scheduling_clock() -> None:
    """A naive clock cannot mint a potentially divergent source-slot run key."""
    reader = _DueReader([])
    runner = _Runner()

    with pytest.raises(ValueError, match="timezone-aware"):
        await CatalogCadenceDispatcher(
            reader,
            runner,
            now=lambda: datetime(2026, 7, 18, 12, 0),
        ).dispatch_once()
