"""P15a Temporal continuation coverage for the reviewed one-GET LibCal source.

The test uses a fixture refresh boundary rather than a network source.  Adapter-level tests prove
LibCal performs one closed GET; this test proves a Pacer projection frees the activity worker and
re-enters the exact durable source/run identity after Temporal's timer (NFR-8, ADR-003/005).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest
from temporalio.client import Client, WorkflowFailureError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from events_concierge.application.catalog_refresh import (
    CatalogRefreshOutcome,
    CatalogRefreshResult,
)
from events_concierge.composition import Container
from events_concierge.config import get_settings
from events_concierge.domain.ids import (
    catalog_paged_refresh_workflow_id,
    catalog_refresh_workflow_id,
)
from events_concierge.workflows import activities
from events_concierge.workflows.activities import (
    refresh_catalog_paged_legistar,
    refresh_catalog_single_get,
)
from events_concierge.workflows.dto import CatalogRefreshActivityResult, CatalogRefreshInput
from events_concierge.workflows.start import (
    TemporalCatalogPagedRefreshStarter,
    TemporalCatalogRefreshStarter,
)
from events_concierge.workflows.workflows import CatalogPagedRefreshWorkflow, CatalogRefreshWorkflow

pytestmark = pytest.mark.integration


class _DeferredThenSucceededRefresh:
    """Fixture service that releases once, then succeeds under the original run identity."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    async def refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        require_single_http_get: bool = False,
    ) -> CatalogRefreshResult:
        self.calls.append((source_key, run_key, require_single_http_get))
        if len(self.calls) == 1:
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.DEFERRED,
                detail="fixture Pacer wait",
                retry_after_seconds=2.0,
            )
        return CatalogRefreshResult(
            source_key,
            run_key,
            CatalogRefreshOutcome.SUCCEEDED,
            candidate_count=1,
            canonical_count=1,
        )


class _BusyThenSucceededRefresh:
    """Fixture worker that loses/reacquires a transient source-run lease (NFR-8)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    async def refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        require_single_http_get: bool = False,
    ) -> CatalogRefreshResult:
        self.calls.append((source_key, run_key, require_single_http_get))
        if len(self.calls) == 1:
            return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.BUSY)
        return CatalogRefreshResult(
            source_key,
            run_key,
            CatalogRefreshOutcome.SUCCEEDED,
            candidate_count=1,
            canonical_count=1,
        )


class _ProgressedDeferredThenSucceededPagedRefresh:
    """Fixture P15b service exercising page loop, durable timer, and terminal promotion result."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def refresh_page(self, source_key: str, run_key: str) -> CatalogRefreshResult:
        self.calls.append((source_key, run_key))
        if len(self.calls) == 1:
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.PROGRESSED,
                detail="fixture full page staged",
            )
        if len(self.calls) == 2:
            return CatalogRefreshResult(
                source_key,
                run_key,
                CatalogRefreshOutcome.DEFERRED,
                detail="fixture Pacer wait",
                retry_after_seconds=2.0,
            )
        return CatalogRefreshResult(
            source_key,
            run_key,
            CatalogRefreshOutcome.SUCCEEDED,
            candidate_count=2,
            canonical_count=2,
        )


class _SkippedThenSucceededRefresh:
    """Fixture authority correction: a skipped source must not consume its durable workflow ID."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []

    async def refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        require_single_http_get: bool = False,
    ) -> CatalogRefreshResult:
        self.calls.append((source_key, run_key, require_single_http_get))
        if len(self.calls) == 1:
            return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.SKIPPED)
        return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.SUCCEEDED)


class _SkippedThenSucceededPagedRefresh:
    """Fixture owner correction for a source-specific paged catalog profile (NFR-8)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def refresh_page(self, source_key: str, run_key: str) -> CatalogRefreshResult:
        self.calls.append((source_key, run_key))
        if len(self.calls) == 1:
            return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.SKIPPED)
        return CatalogRefreshResult(source_key, run_key, CatalogRefreshOutcome.SUCCEEDED)


async def test_catalog_refresh_starter_fails_closed_without_shared_pacer() -> None:
    """P15a never opens a source-egress workflow with process-local admission state (ADR-005)."""
    settings = get_settings().model_copy(update={"pacer_backend": "memory"})
    starter = TemporalCatalogRefreshStarter(cast(Client, SimpleNamespace()), settings)

    with pytest.raises(ValueError, match="shared Redis Pacer"):
        await starter.start("mountain-view-library-events", "manual:no-shared-pacer")


async def test_catalog_refresh_workflow_reacquires_the_same_single_get_run_after_pacer_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Pacer wait uses a Temporal timer and never changes P15a's logical cursor (ADR-003/005)."""
    refresh = _DeferredThenSucceededRefresh()
    settings = get_settings().model_copy(
        update={"temporal_task_queue": "catalog-refresh-p15a", "pacer_backend": "redis"}
    )
    container = cast(Container, SimpleNamespace(catalog_refresh=refresh, settings=settings))
    monkeypatch.setattr(activities, "_container", container)
    task_queue = settings.temporal_task_queue
    inp = CatalogRefreshInput(
        source_key="mountain-view-library-events",
        run_key="manual:libcal-pacer-retry",
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=task_queue,
            workflows=[CatalogRefreshWorkflow],
            activities=[refresh_catalog_single_get],
        ),
    ):
        starter = TemporalCatalogRefreshStarter(environment.client, settings)
        await starter.start(inp.source_key, inp.run_key)
        await starter.start(inp.source_key, inp.run_key)
        handle = environment.client.get_workflow_handle(
            catalog_refresh_workflow_id(inp.source_key, inp.run_key)
        )
        raw_result = cast(dict[str, object], await handle.result())
        result = CatalogRefreshActivityResult(
            source_key=cast(str, raw_result["source_key"]),
            run_key=cast(str, raw_result["run_key"]),
            outcome=cast(str, raw_result["outcome"]),
            candidate_count=cast(int, raw_result["candidate_count"]),
            canonical_count=cast(int, raw_result["canonical_count"]),
            detail=cast(str, raw_result["detail"]),
            retry_after_seconds=cast(float | None, raw_result["retry_after_seconds"]),
        )

    assert result.outcome == CatalogRefreshOutcome.SUCCEEDED.value
    assert result.candidate_count == 1
    assert refresh.calls == [
        ("mountain-view-library-events", "manual:libcal-pacer-retry", True),
        ("mountain-view-library-events", "manual:libcal-pacer-retry", True),
    ]


async def test_catalog_refresh_workflow_retries_busy_without_consuming_its_source_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient P15a lease loss sleeps durably and completes the same source/run once (NFR-8)."""
    refresh = _BusyThenSucceededRefresh()
    settings = get_settings().model_copy(
        update={"temporal_task_queue": "catalog-refresh-p15a-busy", "pacer_backend": "redis"}
    )
    container = cast(Container, SimpleNamespace(catalog_refresh=refresh, settings=settings))
    monkeypatch.setattr(activities, "_container", container)
    inp = CatalogRefreshInput(
        source_key="mountain-view-library-events",
        run_key="manual:libcal-busy-retry",
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=settings.temporal_task_queue,
            workflows=[CatalogRefreshWorkflow],
            activities=[refresh_catalog_single_get],
        ),
    ):
        starter = TemporalCatalogRefreshStarter(environment.client, settings)
        await starter.start(inp.source_key, inp.run_key)
        handle = environment.client.get_workflow_handle(
            catalog_refresh_workflow_id(inp.source_key, inp.run_key)
        )
        raw_result = cast(dict[str, object], await handle.result())

    assert raw_result["outcome"] == CatalogRefreshOutcome.SUCCEEDED.value
    assert (raw_result["candidate_count"], raw_result["canonical_count"]) == (1, 1)
    assert refresh.calls == [
        ("mountain-view-library-events", "manual:libcal-busy-retry", True),
        ("mountain-view-library-events", "manual:libcal-busy-retry", True),
    ]


async def test_single_get_skipped_workflow_can_reuse_the_same_cadence_slot_after_correction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed authority preflight does not permanently consume P15a's source/slot identity."""
    refresh = _SkippedThenSucceededRefresh()
    settings = get_settings().model_copy(
        update={"temporal_task_queue": "catalog-refresh-p15a-skipped", "pacer_backend": "redis"}
    )
    container = cast(Container, SimpleNamespace(catalog_refresh=refresh, settings=settings))
    monkeypatch.setattr(activities, "_container", container)
    inp = CatalogRefreshInput(
        source_key="mountain-view-library-events",
        run_key="cadence:mountain-view-library-events:20260718T120000.000000Z",
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=settings.temporal_task_queue,
            workflows=[CatalogRefreshWorkflow],
            activities=[refresh_catalog_single_get],
        ),
    ):
        starter = TemporalCatalogRefreshStarter(environment.client, settings)
        await starter.start(inp.source_key, inp.run_key)
        first = environment.client.get_workflow_handle(
            catalog_refresh_workflow_id(inp.source_key, inp.run_key)
        )
        with pytest.raises(WorkflowFailureError):
            await first.result()

        await starter.start(inp.source_key, inp.run_key)
        second = environment.client.get_workflow_handle(
            catalog_refresh_workflow_id(inp.source_key, inp.run_key)
        )
        raw_result = cast(dict[str, object], await second.result())

    assert raw_result["outcome"] == CatalogRefreshOutcome.SUCCEEDED.value
    assert refresh.calls == [
        (inp.source_key, inp.run_key, True),
        (inp.source_key, inp.run_key, True),
    ]


async def test_paged_catalog_refresh_workflow_advances_one_page_effect_then_reuses_same_run_after_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P15b has distinct activity effects for a full page, Pacer timer, and terminal promotion."""
    refresh = _ProgressedDeferredThenSucceededPagedRefresh()
    settings = get_settings().model_copy(
        update={"temporal_task_queue": "catalog-refresh-p15b", "pacer_backend": "redis"}
    )
    container = cast(Container, SimpleNamespace(catalog_paged_refresh=refresh, settings=settings))
    monkeypatch.setattr(activities, "_container", container)
    inp = CatalogRefreshInput(
        source_key="san-jose-legistar-meetings",
        run_key="manual:legistar-pager",
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=settings.temporal_task_queue,
            workflows=[CatalogPagedRefreshWorkflow],
            activities=[refresh_catalog_paged_legistar],
        ),
    ):
        starter = TemporalCatalogPagedRefreshStarter(environment.client, settings)
        await starter.start(inp.source_key, inp.run_key)
        handle = environment.client.get_workflow_handle(
            catalog_paged_refresh_workflow_id(inp.source_key, inp.run_key)
        )
        raw_result = cast(dict[str, object], await handle.result())
        result = CatalogRefreshActivityResult(
            source_key=cast(str, raw_result["source_key"]),
            run_key=cast(str, raw_result["run_key"]),
            outcome=cast(str, raw_result["outcome"]),
            candidate_count=cast(int, raw_result["candidate_count"]),
            canonical_count=cast(int, raw_result["canonical_count"]),
            detail=cast(str, raw_result["detail"]),
            retry_after_seconds=cast(float | None, raw_result["retry_after_seconds"]),
        )

    assert result.outcome == CatalogRefreshOutcome.SUCCEEDED.value
    assert (result.candidate_count, result.canonical_count) == (2, 2)
    assert refresh.calls == [
        ("san-jose-legistar-meetings", "manual:legistar-pager"),
        ("san-jose-legistar-meetings", "manual:legistar-pager"),
        ("san-jose-legistar-meetings", "manual:legistar-pager"),
    ]


@pytest.mark.parametrize(
    ("source_key", "phase"),
    [
        ("sunnyvale-legistar-meetings", "p15c"),
        ("alameda-legistar-meetings", "p15d"),
        ("oakland-legistar-meetings", "p15e"),
    ],
)
async def test_paged_skipped_workflow_can_reuse_the_same_city_cadence_slot_after_correction(
    monkeypatch: pytest.MonkeyPatch,
    source_key: str,
    phase: str,
) -> None:
    """P15c/P15d/P15e authority repair restarts an exact city source/slot workflow (NFR-8)."""
    refresh = _SkippedThenSucceededPagedRefresh()
    settings = get_settings().model_copy(
        update={
            "temporal_task_queue": f"catalog-refresh-{phase}-skipped",
            "pacer_backend": "redis",
        }
    )
    container = cast(Container, SimpleNamespace(catalog_paged_refresh=refresh, settings=settings))
    monkeypatch.setattr(activities, "_container", container)
    inp = CatalogRefreshInput(
        source_key=source_key,
        run_key=f"cadence:{source_key}:20260718T120000.000000Z",
    )

    async with (
        await WorkflowEnvironment.start_time_skipping() as environment,
        Worker(
            environment.client,
            task_queue=settings.temporal_task_queue,
            workflows=[CatalogPagedRefreshWorkflow],
            activities=[refresh_catalog_paged_legistar],
        ),
    ):
        starter = TemporalCatalogPagedRefreshStarter(environment.client, settings)
        await starter.start(inp.source_key, inp.run_key)
        first = environment.client.get_workflow_handle(
            catalog_paged_refresh_workflow_id(inp.source_key, inp.run_key)
        )
        with pytest.raises(WorkflowFailureError):
            await first.result()

        await starter.start(inp.source_key, inp.run_key)
        second = environment.client.get_workflow_handle(
            catalog_paged_refresh_workflow_id(inp.source_key, inp.run_key)
        )
        raw_result = cast(dict[str, object], await second.result())

    assert raw_result["outcome"] == CatalogRefreshOutcome.SUCCEEDED.value
    assert refresh.calls == [(inp.source_key, inp.run_key), (inp.source_key, inp.run_key)]
