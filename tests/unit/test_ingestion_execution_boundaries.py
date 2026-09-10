"""Exercise real catalog routing beneath the resumable command processor, without services."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as DatabaseTimeoutError
from tests.unit import test_ingestion_command_execution as command_harness
from tests.unit.test_catalog_refresh import (
    _candidate,
    _MutableDiscoveryPolicyGate,
    _RecordingPacer,
    _source,
)

from events_concierge.application.catalog_refresh import CatalogRefreshService
from events_concierge.application.catalog_refresh_router import CatalogRefreshRouter
from events_concierge.domain.catalog_sources import CatalogRefreshClaim, CatalogSource
from events_concierge.domain.enums import CatalogRefreshClaimOutcome, CatalogSourceMode
from events_concierge.domain.events import CandidateEvent
from events_concierge.domain.ingestion_admin import IngestionCommandAction
from events_concierge.ports.catalog_sources import CatalogSourceRepository
from events_concierge.ports.ingestion_command_execution import CommandExecutionUncertainError
from events_concierge.workers.catalog_refresh_routing import _PersistenceAwareRouter


class _SourceLedger:
    """Model the source lease and atomic publication outcome separately from its acknowledgement.

    Existing command queue fixtures handle continuations. This source fixture deliberately clears
    the lease on commit, so the service's subsequent failure report cannot undo committed success.
    """

    def __init__(self, source: CatalogSource) -> None:
        self.source = source
        self.live: dict[str, UUID] = {}
        self.published: dict[str, tuple[CandidateEvent, ...]] = {}
        self.claims: list[tuple[str, str]] = []
        self.rejected_failures: list[str] = []

    async def get(self, source_key: str) -> CatalogSource | None:
        return self.source if source_key == self.source.source_key else None

    async def claim_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_seconds: int,
    ) -> CatalogRefreshClaim:
        assert source_key == self.source.source_key and lease_seconds > 0
        self.claims.append((source_key, run_key))
        if run_key in self.published:
            return CatalogRefreshClaim(CatalogRefreshClaimOutcome.SUCCEEDED)
        if self.live:
            return CatalogRefreshClaim(CatalogRefreshClaimOutcome.BUSY)
        token = uuid4()
        self.live[run_key] = token
        return CatalogRefreshClaim(CatalogRefreshClaimOutcome.ACQUIRED, token)

    async def has_live_refresh_lease(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
    ) -> bool:
        return source_key == self.source.source_key and self.live.get(run_key) == lease_token

    async def fail_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        error: str,
    ) -> bool:
        assert source_key == self.source.source_key
        if self.live.get(run_key) != lease_token:
            self.rejected_failures.append(run_key)
            return False
        self.live.pop(run_key)
        return True


class _CommitThenLoseAcknowledgement:
    def __init__(self, ledger: _SourceLedger) -> None:
        self.ledger = ledger
        self.calls = 0

    async def commit_refresh(
        self,
        source_key: str,
        run_key: str,
        *,
        lease_token: UUID,
        candidates: list[CandidateEvent],
    ) -> None:
        assert source_key == self.ledger.source.source_key
        assert self.ledger.live[run_key] == lease_token
        assert run_key not in self.ledger.published
        self.calls += 1
        # One atomic server outcome is durable before the client loses the COMMIT response.
        self.ledger.published[run_key] = tuple(candidates)
        self.ledger.live.pop(run_key)
        raise OperationalError("COMMIT", None, ConnectionError("acknowledgement lost"))


async def test_committed_catalog_with_lost_ack_reconciles_same_run_without_refetching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = command_harness.NOW
    claim = command_harness.lease(IngestionCommandAction.REFRESH_SOURCE)
    source = replace(_source(now), source_key=claim.source_key)
    ledger = _SourceLedger(source)
    fetcher = SimpleNamespace(fetch=AsyncMock(return_value=[_candidate(now)]))
    committer = _CommitThenLoseAcknowledgement(ledger)
    pacer = _RecordingPacer()
    service = CatalogRefreshService(
        cast(CatalogSourceRepository, ledger),
        committer,
        {source.mode: fetcher},
        pacer,
        _MutableDiscoveryPolicyGate(),
        now=lambda: command_harness.NOW,
    )
    router = _PersistenceAwareRouter(
        CatalogRefreshRouter(cast(CatalogSourceRepository, ledger), service, None),
    )
    repository = command_harness.Repository([claim])
    store = command_harness.Store()
    processor = command_harness.processor(repository, router, store)
    run_key = f"admin:{claim.command_id}"

    first = await processor.process_once(1)

    assert first.deferred == 1 and first.failed == 0
    assert len(ledger.published[run_key]) == 1
    assert ledger.rejected_failures == [run_key]
    assert store.tasks[0].status == "deferred"
    assert store.tasks[0].run_key == run_key
    repository.fail.assert_not_awaited()
    repository.complete.assert_not_awaited()

    # Advance only the deterministic test clock past the existing task's durable retry time.
    monkeypatch.setattr(command_harness, "NOW", now + timedelta(seconds=61))
    repository.claims.append(replace(claim, attempt_count=2, lease_token=uuid4()))
    second = await processor.process_once(1)

    assert second.completed == 1 and second.failed == 0
    assert store.tasks[0].status == "already_succeeded"
    assert store.tasks[0].run_key == run_key
    assert store.tasks[0].attempt_count == 2
    assert ledger.claims == [(source.source_key, run_key), (source.source_key, run_key)]
    assert fetcher.fetch.await_count == 1
    assert committer.calls == 1
    assert len(pacer.requests) == 1
    assert len(ledger.published) == 1
    assert repository.complete.await_args.args[1]["outcome"] == "already_succeeded"
    publication_starts = [
        fields
        for code, fields in store.events
        if code == "stage_started" and fields.get("stage") == "catalog_publish"
    ]
    assert len(publication_starts) == 1


@pytest.mark.parametrize(
    "action",
    [IngestionCommandAction.REFRESH_SOURCE, IngestionCommandAction.REFRESH_DUE],
)
async def test_temporal_queued_task_is_dispatch_receipt_without_publication(
    action: IngestionCommandAction,
) -> None:
    claim = command_harness.lease(action)
    source_key = claim.source_key or "single-get-source"
    source = replace(
        _source(command_harness.NOW),
        source_key=source_key,
        mode=CatalogSourceMode.LIBCAL_ICS,
    )
    ledger = _SourceLedger(source)
    fetcher = SimpleNamespace(fetch=AsyncMock())
    committer = SimpleNamespace(commit_refresh=AsyncMock())
    service = CatalogRefreshService(
        cast(CatalogSourceRepository, ledger),
        committer,
        {source.mode: fetcher},
        _RecordingPacer(),
        _MutableDiscoveryPolicyGate(),
        now=lambda: command_harness.NOW,
    )
    starter = SimpleNamespace(start=AsyncMock())
    router = _PersistenceAwareRouter(
        CatalogRefreshRouter(cast(CatalogSourceRepository, ledger), service, starter),
    )
    repository = command_harness.Repository([claim], keys=(source_key,))
    store = command_harness.Store()

    report = await command_harness.processor(repository, router, store).process_once(1)

    assert report.completed == 1  # The command's dispatch intent has been fulfilled.
    assert store.tasks[0].status == "queued"
    assert store.tasks[0].last_outcome_code == "queued"
    starter.start.assert_awaited_once_with(source_key, store.tasks[0].run_key)
    fetcher.fetch.assert_not_awaited()
    committer.commit_refresh.assert_not_awaited()
    assert ledger.claims == [] and ledger.published == {}
    assert not any(
        code in {"stage_started", "stage_completed", "progress"} for code, _ in store.events
    )
    result = repository.complete.await_args.args[1]
    if action is IngestionCommandAction.REFRESH_SOURCE:
        assert result["outcome"] == "queued"
    else:
        assert result["queued"] == 1 and result["succeeded"] == 0
    assert store.tasks[0].candidate_count == store.tasks[0].canonical_count == 0


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("COMMIT", None, ConnectionError("disconnected")),
        DatabaseTimeoutError("pool timed out"),
        InterfaceError("COMMIT", None, ConnectionError("interface disconnected")),
        DisconnectionError("connection invalidated"),
    ],
)
async def test_database_uncertainty_wrapper_preserves_original_cause(error: Exception) -> None:
    underlying = SimpleNamespace(refresh=AsyncMock(side_effect=error))
    router = _PersistenceAwareRouter(underlying)
    with pytest.raises(CommandExecutionUncertainError) as raised:
        await router.refresh("source-events", "admin:stable-run")
    assert raised.value.__cause__ is error
    underlying.refresh.assert_awaited_once_with("source-events", "admin:stable-run")


async def test_database_uncertainty_wrapper_does_not_reclassify_deterministic_validation() -> None:
    error = ValueError("source contract rejected")
    underlying = SimpleNamespace(refresh=AsyncMock(side_effect=error))
    with pytest.raises(ValueError) as raised:
        await _PersistenceAwareRouter(underlying).refresh("source-events", "admin:stable-run")
    assert raised.value is error
    assert raised.value.__cause__ is None
