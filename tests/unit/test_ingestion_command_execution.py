"""Behavioral contracts for fixed fleet plans, fair yielding and fenced recovery."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from events_concierge.application import ingestion_command_execution as execution
from events_concierge.application.catalog_refresh import CatalogRefreshOutcome, CatalogRefreshResult
from events_concierge.application.ingestion_command_execution import (
    ResumableIngestionCommandProcessor,
)
from events_concierge.application.ingestion_telemetry import emit_ingestion_event
from events_concierge.domain.ingestion_admin import IngestionCommandAction, IngestionCommandLease
from events_concierge.domain.ingestion_investigation import CommandTask

NOW = datetime(2026, 9, 8, 16, tzinfo=UTC)


@pytest.mark.parametrize("skew_hours", [-24, 24])
def test_retry_wakeup_uses_database_delay_despite_worker_clock_skew(skew_hours):
    waiting = replace(
        task(0, "waiting-source", status="deferred", available_at=NOW + timedelta(seconds=120)),
        retry_after_seconds=120,
    )
    assert execution._next_wakeup([waiting], NOW + timedelta(hours=skew_hours)) == 120


def test_pending_source_yields_promptly_even_with_skewed_clock():
    pending = task(0, "pending-source")
    assert execution._next_wakeup([pending], NOW - timedelta(hours=24)) == 1


def lease(action=IngestionCommandAction.REFRESH_DUE, *, attempt=1, command_id=None):
    return IngestionCommandLease(
        command_id or uuid4(),
        action,
        "manual-source" if action == IngestionCommandAction.REFRESH_SOURCE else None,
        attempt,
        uuid4(),
    )


def task(position, key, *, status="pending", attempts=0, available_at=NOW):
    return CommandTask(
        position,
        key,
        f"cadence:{key}:fixed",
        status,
        attempts,
        available_at,
        None,
        None,
        None if status == "pending" else status,
        0,
        0,
    )


class Store:
    def __init__(self, tasks=None):
        self.tasks = tasks
        self.starts = []
        self.finished = []
        self.events = []
        self.ensure_calls = 0
        self.fail_events = False
        self.fail_finish = False

    async def get_plan(self, claim):
        return self.tasks

    async def ensure_plan(self, claim, targets):
        self.ensure_calls += 1
        if self.tasks is None:
            self.tasks = tuple(
                replace(task(t.position, t.source_key), run_key=t.run_key) for t in targets
            )
        return self.tasks

    async def start_task(self, claim, item, **kwargs):
        if item.terminal or item.available_at > NOW:
            return None
        started = replace(
            item, status="running", attempt_count=item.attempt_count + 1, started_at=NOW
        )
        self.tasks = tuple(started if t.position == item.position else t for t in self.tasks)
        self.starts.append((claim, started))
        return started

    async def finish_task(
        self,
        claim,
        item,
        outcome_code,
        *,
        candidate_count=0,
        canonical_count=0,
        retry_after_seconds=None,
    ):
        if self.fail_finish:
            raise ConnectionError("unknown commit result")
        self.finished.append((item, outcome_code, retry_after_seconds))
        status = (
            "deferred"
            if retry_after_seconds is not None
            else "failed"
            if outcome_code == "retry_exhausted"
            else outcome_code
        )
        updated = replace(
            item,
            status=status,
            last_outcome_code=outcome_code,
            candidate_count=candidate_count,
            canonical_count=canonical_count,
            available_at=NOW + timedelta(seconds=retry_after_seconds or 0),
        )
        self.tasks = tuple(updated if t.position == item.position else t for t in self.tasks)
        return True

    async def record_event(self, claim, code, **kwargs):
        if self.fail_events:
            raise ConnectionError("telemetry unavailable")
        self.events.append((code, kwargs))
        return True


class Repository:
    def __init__(self, claims, keys=("first-source", "second-source")):
        self.claims = list(claims)
        self.list_due_refreshes = AsyncMock(
            return_value=[
                SimpleNamespace(
                    source=SimpleNamespace(source_key=key),
                    due_at=NOW,
                    run_key=lambda key=key: f"cadence:{key}:fixed",
                )
                for key in keys
            ]
        )
        self.link_command_runs = AsyncMock()
        self.renew_lease = AsyncMock(return_value=True)
        self.defer = AsyncMock(return_value=True)
        self.complete = AsyncMock(return_value=True)
        self.fail = AsyncMock(return_value=True)

    async def claim_batch(self, *args):
        return (self.claims.pop(0),) if self.claims else ()


def successful_router():
    async def refresh(key, run):
        return CatalogRefreshResult(key, run, CatalogRefreshOutcome.SUCCEEDED, 10, 8)

    return SimpleNamespace(refresh=AsyncMock(side_effect=refresh))


def processor(repo, router, store, **kwargs):
    return ResumableIngestionCommandProcessor(repo, router, store, now=lambda: NOW, **kwargs)


async def test_fleet_yields_after_one_source_and_keeps_original_plan_on_continuation():
    first = lease()
    repo = Repository([first])
    store = Store()
    router = successful_router()
    worker = processor(repo, router, store)
    report = await worker.process_once(1)
    assert report.deferred == 1
    assert router.refresh.await_count == 1
    repo.defer.assert_awaited_once_with(first, 1)
    repo.complete.assert_not_awaited()

    # Due registry changing between claims cannot enlarge an already accepted plan.
    repo.list_due_refreshes.return_value = []
    repo.claims.append(replace(first, attempt_count=2, lease_token=uuid4()))
    report = await worker.process_once(1)
    assert report.completed == 1
    assert repo.list_due_refreshes.await_count == 1
    assert [c.args[0] for c in router.refresh.await_args_list] == ["first-source", "second-source"]
    assert repo.complete.await_args.args[1]["due_sources"] == 2
    assert repo.complete.await_args.args[1]["succeeded"] == 2
    assert store.ensure_calls == 1


async def test_reclaimed_parent_does_not_repeat_completed_source_work():
    repo = Repository([lease(attempt=700)])
    store = Store(
        (task(0, "first-source", status="succeeded", attempts=1), task(1, "second-source"))
    )
    router = successful_router()
    report = await processor(repo, router, store).process_once(1)
    assert report.completed == 1
    router.refresh.assert_awaited_once_with("second-source", "cadence:second-source:fixed")
    repo.list_due_refreshes.assert_not_awaited()


async def test_deferred_source_does_not_block_other_ready_sources_in_same_plan():
    repo = Repository([lease()])
    store = Store(
        (
            task(0, "first-source", status="deferred", available_at=NOW + timedelta(minutes=5)),
            task(1, "second-source"),
        )
    )
    router = successful_router()
    report = await processor(repo, router, store).process_once(1)
    assert report.deferred == 1
    router.refresh.assert_awaited_once_with("second-source", "cadence:second-source:fixed")
    assert repo.defer.await_args.args[1] == 300


async def test_empty_plan_is_persisted_and_completed_without_provider_work():
    repo = Repository([lease()], keys=())
    store = Store()
    router = successful_router()
    report = await processor(repo, router, store).process_once(1)
    assert report.completed == 1
    assert store.tasks == ()
    router.refresh.assert_not_awaited()


@pytest.mark.parametrize("attempts,expected", [(0, "deferred"), (4, "failed")])
async def test_manual_busy_source_retries_with_per_source_budget(attempts, expected):
    claim = lease(IngestionCommandAction.REFRESH_SOURCE, attempt=80)
    repo = Repository([claim])
    store = Store(
        (replace(task(0, "manual-source", attempts=attempts), run_key=f"admin:{claim.command_id}"),)
    )
    router = SimpleNamespace(
        refresh=AsyncMock(
            return_value=CatalogRefreshResult(
                "manual-source", f"admin:{claim.command_id}", CatalogRefreshOutcome.BUSY
            )
        )
    )
    report = await processor(repo, router, store).process_once(1)
    assert getattr(report, expected) == 1
    assert store.finished[0][1] == ("busy" if expected == "deferred" else "retry_exhausted")


@pytest.mark.parametrize("renew_error", [False, True])
async def test_lost_heartbeat_cancels_source_before_any_terminal_write(renew_error):
    cancelled = asyncio.Event()

    async def hanging(key, run):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    repo = Repository([lease()])
    if renew_error:
        repo.renew_lease.side_effect = ConnectionError("secret provider details")
    else:
        repo.renew_lease.return_value = False
    store = Store((task(0, "first-source"),))
    router = SimpleNamespace(refresh=AsyncMock(side_effect=hanging))
    report = await processor(repo, router, store, lease_heartbeat_seconds=0.001).process_once(1)
    assert report.lost_leases == 1
    assert cancelled.is_set()
    assert store.finished == []
    repo.complete.assert_not_awaited()
    repo.fail.assert_not_awaited()
    repo.defer.assert_not_awaited()
    assert any(
        code == ("lease_renewal_error" if renew_error else "lease_lost") for code, _ in store.events
    )
    assert "secret provider details" not in str(store.events)


async def test_unknown_task_commit_outcome_remains_reclaimable():
    repo = Repository([lease()])
    store = Store((task(0, "first-source"),))
    store.fail_finish = True
    report = await processor(repo, successful_router(), store).process_once(1)
    assert report.lost_leases == 1
    repo.complete.assert_not_awaited()
    repo.fail.assert_not_awaited()


async def test_observation_outage_does_not_fail_a_successful_source():
    async def refresh(key, run):
        await emit_ingestion_event("stage_started", stage="collect")
        return CatalogRefreshResult(key, run, CatalogRefreshOutcome.SUCCEEDED, 10, 8)

    repo = Repository([lease()])
    store = Store((task(0, "first-source"),))
    store.fail_events = True
    report = await processor(
        repo, SimpleNamespace(refresh=AsyncMock(side_effect=refresh)), store
    ).process_once(1)
    assert report.completed == 1
    assert store.tasks[0].canonical_count == 8


async def test_real_stage_event_carries_exact_command_and_source_attempt():
    claim = lease(attempt=7)
    repo = Repository([claim])
    store = Store((task(0, "first-source", attempts=2),))

    async def refresh(key, run):
        await emit_ingestion_event("stage_started", stage="collect")
        return CatalogRefreshResult(key, run, CatalogRefreshOutcome.SUCCEEDED)

    await processor(
        repo,
        SimpleNamespace(refresh=AsyncMock(side_effect=refresh)),
        store,
        worker_id="worker-a",
        release_revision="test-build",
    ).process_once(1)
    event = next(fields for code, fields in store.events if code == "stage_started")
    assert event["source_key"] == "first-source"
    assert event["run_key"] == "cadence:first-source:fixed"
    assert event["task_attempt"] == 3
    assert event["worker_id"] == "worker-a"
    assert event["release_revision"] == "test-build"


async def test_hung_renewal_has_deadline_and_cancels_provider(monkeypatch):
    monkeypatch.setattr(execution, "_RENEWAL_TIMEOUT_SECONDS", 0.01)
    cancelled = asyncio.Event()

    async def source(key, run):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def renewal(*args):
        await asyncio.Event().wait()

    repo = Repository([lease()])
    repo.renew_lease.side_effect = renewal
    store = Store((task(0, "first-source"),))
    worker = processor(
        repo,
        SimpleNamespace(refresh=AsyncMock(side_effect=source)),
        store,
        lease_heartbeat_seconds=0.001,
    )
    async with asyncio.timeout(1):
        report = await worker.process_once(1)
    assert report.lost_leases == 1
    assert cancelled.is_set()
    assert store.finished == []
    assert any(
        code == "lease_renewal_error" and fields["error_type"] == "timeout"
        for code, fields in store.events
    )


async def test_logger_outage_does_not_prevent_source_execution(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("log sink is unavailable")

    monkeypatch.setattr(execution, "_log", SimpleNamespace(info=broken, warning=broken))
    repo = Repository([lease()])
    store = Store((task(0, "first-source"),))
    store.fail_events = True
    report = await processor(repo, successful_router(), store).process_once(1)
    assert report.completed == 1


async def test_catalog_commit_acknowledgement_loss_reconciles_same_run_key():
    claim = lease()
    repo = Repository([claim])
    store = Store((task(0, "first-source"),))

    async def refresh(key, run):
        if router.refresh.await_count == 1:
            raise ConnectionError("catalog commit acknowledgement lost")
        return CatalogRefreshResult(key, run, CatalogRefreshOutcome.ALREADY_SUCCEEDED)

    router = SimpleNamespace(refresh=AsyncMock(side_effect=refresh))
    worker = processor(repo, router, store)
    first = await worker.process_once(1)
    assert first.deferred == 1
    repo.fail.assert_not_awaited()
    store.tasks = tuple(replace(t, available_at=NOW) for t in store.tasks)
    repo.claims.append(replace(claim, attempt_count=2, lease_token=uuid4()))
    assert (await worker.process_once(1)).completed == 1
    assert [c.args for c in router.refresh.await_args_list] == [
        ("first-source", "cadence:first-source:fixed"),
        ("first-source", "cadence:first-source:fixed"),
    ]
    assert store.tasks[0].status == "already_succeeded"
