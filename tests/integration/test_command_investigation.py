"""Isolated PostgreSQL proofs for durable plans, crash fencing, fairness, and event authority."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession
from tests.integration.test_operator_management import _denied, _owner_transaction, _role, _source

from events_concierge.adapters.postgres.command_investigation import CommandInvestigationStore
from events_concierge.adapters.postgres.ingestion_admin import PostgresIngestionAdminRepository
from events_concierge.api.admin import IngestionRunOut
from events_concierge.application.catalog_refresh import CatalogRefreshOutcome, CatalogRefreshResult
from events_concierge.application.ingestion_command_execution import (
    ResumableIngestionCommandProcessor,
)
from events_concierge.application.ingestion_telemetry import emit_ingestion_event
from events_concierge.domain.ingestion_admin import (
    IngestionCommandAction,
    IngestionCommandLease,
    IngestionCommandRunTarget,
)
from events_concierge.domain.ingestion_investigation import CommandInvestigationLeaseLostError

pytestmark = pytest.mark.integration


def _scope(connection: AsyncConnection, role: str):  # type: ignore[no-untyped-def]
    @asynccontextmanager
    async def scope() -> AsyncIterator[AsyncSession]:
        async with (
            AsyncSession(bind=connection, join_transaction_mode="create_savepoint") as session,
            session.begin(),
        ):
            await session.execute(text(f"SET LOCAL ROLE {role}"))
            yield session

    return scope


async def _command(connection: AsyncConnection, source: str | None = None) -> UUID:
    await _role(connection, None)
    identifier = uuid4()
    await connection.execute(
        text("""
      INSERT INTO public.ingestion_admin_commands(command_id,action,source_key,requested_by)
      VALUES(:id,:action,:source,'investigation-test')
    """),
        {
            "id": identifier,
            "action": "refresh_source" if source else "refresh_due",
            "source": source,
        },
    )
    return identifier


async def _claim(repository: PostgresIngestionAdminRepository) -> IngestionCommandLease:
    [lease] = await repository.claim_batch(1, 300, "test-build", None)
    return lease


async def test_plan_is_fixed_empty_plan_is_distinct_and_viewer_cannot_execute() -> None:
    async with _owner_transaction() as connection:
        first, second = await _source(connection), await _source(connection)
        identifier = await _command(connection)
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_ingestion_executor")
        )
        store = CommandInvestigationStore(session_scope=_scope(connection, "ec_ingestion_executor"))
        reader = CommandInvestigationStore(session_scope=_scope(connection, "ec_operator_viewer"))
        lease = await _claim(repo)
        assert lease.command_id == identifier
        assert await store.get_plan(lease) is None
        targets = (IngestionCommandRunTarget(0, first, f"cadence:{first}:fixed"),)
        plan = await store.ensure_plan(lease, targets)
        assert len(plan) == 1 and plan[0].source_key == first
        assert plan[0].retry_after_seconds == 0
        again = await store.ensure_plan(
            lease, (IngestionCommandRunTarget(0, second, f"cadence:{second}:changed"),)
        )
        assert again == plan
        await _role(connection, None)
        await connection.execute(
            text(
                "UPDATE public.ingestion_command_tasks SET available_at=clock_timestamp()+interval '2 minutes' WHERE command_id=:id"
            ),
            {"id": identifier},
        )
        delayed = await store.get_plan(lease)
        assert delayed is not None
        assert delayed[0].retry_after_seconds is not None
        assert 119 <= delayed[0].retry_after_seconds <= 120
        snapshot = await reader.investigation(identifier)
        assert snapshot is not None and snapshot["plan"]["status"] == "fixed"
        await _role(connection, "ec_operator_viewer")
        await _denied(connection, "SELECT public.fn_command_execution_v1(NULL,NULL,NULL,NULL,NULL)")
        await _role(connection, "ec_app")
        await _denied(connection, "SELECT public.fn_get_command_investigation_v1(NULL,0,10)")
        await _denied(connection, "SELECT * FROM public.ingestion_command_events")
        assert await repo.complete(
            lease,
            {
                "action": "refresh_due",
                "due_sources": 0,
                "attempted": 0,
                "succeeded": 0,
                "queued": 0,
                "skipped": 0,
                "deferred": 0,
                "already_succeeded": 0,
                "busy": 0,
                "progressed": 0,
                "failed": 0,
            },
        )
        await _command(connection)
        empty_lease = await _claim(repo)
        assert await store.ensure_plan(empty_lease, ()) == ()
        assert await store.get_plan(empty_lease) == ()


async def test_yielded_fleet_is_fair_to_waiting_manual_and_task_result_is_idempotently_fenced() -> (
    None
):
    async with _owner_transaction() as connection:
        source = await _source(connection)
        fleet = await _command(connection)
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_ingestion_executor")
        )
        store = CommandInvestigationStore(session_scope=_scope(connection, "ec_ingestion_executor"))
        lease = await _claim(repo)
        [task] = await store.ensure_plan(
            lease, (IngestionCommandRunTarget(0, source, f"cadence:{source}:fair"),)
        )
        started = await store.start_task(
            lease,
            task,
            worker_id="fixture-worker",
            release_revision="test-build",
            image_digest=None,
        )
        assert started is not None and started.attempt_count == 1
        assert started.retry_after_seconds == 0
        assert await store.finish_task(
            lease, started, "succeeded", candidate_count=3, canonical_count=2
        )
        assert not await store.finish_task(lease, started, "failed")
        assert (
            await store.start_task(
                lease,
                started,
                worker_id="fixture-worker",
                release_revision="test-build",
                image_digest=None,
            )
            is None
        )
        manual = await _command(connection, source)
        assert await repo.defer(lease, 1)
        # Make both eligible without sleeping: the fleet's new scheduling position still follows
        # the already-waiting manual command; original requested_at remains older on the fleet.
        await _role(connection, None)
        await connection.execute(
            text(
                "UPDATE public.ingestion_admin_commands SET available_at=clock_timestamp()-interval '1 second' WHERE command_id=:fleet"
            ),
            {"fleet": fleet},
        )
        await connection.execute(
            text(
                "UPDATE public.ingestion_admin_commands SET available_at=clock_timestamp()-interval '2 seconds' WHERE command_id=:manual"
            ),
            {"manual": manual},
        )
        assert (await _claim(repo)).command_id == manual
        with pytest.raises(CommandInvestigationLeaseLostError):
            await store.get_plan(lease)


async def test_expired_attempt_diagnostic_survives_but_reclaimed_writer_cannot_advance_or_emit() -> (
    None
):
    async with _owner_transaction() as connection:
        source = await _source(connection)
        identifier = await _command(connection)
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_ingestion_executor")
        )
        store = CommandInvestigationStore(session_scope=_scope(connection, "ec_ingestion_executor"))
        reader = CommandInvestigationStore(session_scope=_scope(connection, "ec_operator_viewer"))
        lease = await _claim(repo)
        [task] = await store.ensure_plan(
            lease, (IngestionCommandRunTarget(0, source, f"cadence:{source}:crash"),)
        )
        started = await store.start_task(
            lease, task, worker_id="worker-one", release_revision="build-one", image_digest=None
        )
        assert started is not None
        event_id = uuid4()
        for _ in range(2):
            assert await store.record_event(
                lease,
                "progress",
                source_key=source,
                run_key=task.run_key,
                task_attempt=1,
                stage="collect",
                request_count=12,
                page_count=8,
                event_id=event_id,
            )
        await _role(connection, None)
        await connection.execute(
            text(
                "UPDATE public.ingestion_admin_commands SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE command_id=:id"
            ),
            {"id": identifier},
        )
        assert await store.record_event(lease, "lease_lost", error_type="timeout")
        assert not await store.record_event(lease, "progress", candidate_count=999)
        snapshot = await reader.investigation(identifier)
        assert snapshot is not None and snapshot["plan"]["tasks"][0]["lease_state"] == "expired"
        renewed = await _claim(repo)
        assert renewed.attempt_count == lease.attempt_count + 1
        assert not await store.record_event(lease, "lease_lost")
        with pytest.raises(CommandInvestigationLeaseLostError):
            await store.finish_task(lease, started, "succeeded")
        retried = await store.start_task(
            renewed,
            started,
            worker_id="worker-two",
            release_revision="build-two",
            image_digest=None,
        )
        assert retried is not None and retried.attempt_count == 2
        assert not await store.finish_task(renewed, replace(retried, attempt_count=1), "succeeded")
        snapshot = await reader.investigation(identifier)
        assert snapshot is not None
        assert (
            len([event for event in snapshot["events"] if event["event_code"] == "progress"]) == 1
        )
        assert [attempt["kind"] for attempt in snapshot["attempts"]] == ["initial", "reclaim"]
        assert "lease_token" not in str(snapshot)


async def test_initial_event_page_starts_at_live_tail_then_cursor_follows_without_gaps() -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        identifier = await _command(connection)
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_ingestion_executor")
        )
        store = CommandInvestigationStore(session_scope=_scope(connection, "ec_ingestion_executor"))
        reader = CommandInvestigationStore(session_scope=_scope(connection, "ec_operator_viewer"))
        lease = await _claim(repo)
        [task] = await store.ensure_plan(
            lease, (IngestionCommandRunTarget(0, source, f"cadence:{source}:tail"),)
        )
        started = await store.start_task(
            lease, task, worker_id="fixture", release_revision="build", image_digest=None
        )
        assert started is not None

        async def progress(count: int) -> None:
            assert await store.record_event(
                lease,
                "progress",
                source_key=source,
                run_key=task.run_key,
                task_attempt=started.attempt_count,
                stage="collect",
                request_count=count,
            )

        for count in range(1, 6):
            await progress(count)
        tail = await reader.investigation(identifier, limit=2)
        assert tail is not None
        assert [event["request_count"] for event in tail["events"]] == [4, 5]
        assert not tail["has_more"] and not tail["evidence"]["history_complete"]
        assert tail["next_event_id"] == tail["events"][-1]["event_id"]
        assert int(tail["events"][0]["event_id"]) < int(tail["next_event_id"])

        for count in range(6, 9):
            await progress(count)
        follow = await reader.investigation(
            identifier, after_event_id=int(tail["next_event_id"]), limit=2
        )
        assert follow is not None and follow["has_more"]
        assert [event["request_count"] for event in follow["events"]] == [6, 7]
        last = await reader.investigation(
            identifier, after_event_id=int(follow["next_event_id"]), limit=2
        )
        assert last is not None and not last["has_more"]
        assert [event["request_count"] for event in last["events"]] == [8]
        empty = await reader.investigation(
            identifier, after_event_id=int(last["next_event_id"]), limit=2
        )
        assert empty is not None and empty["events"] == [] and not empty["has_more"]


async def test_execution_capability_rejects_null_authority_wrong_intent_and_late_progress() -> None:
    async with _owner_transaction() as connection:
        first, second = await _source(connection), await _source(connection)
        identifier = await _command(connection, first)
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_ingestion_executor")
        )
        store = CommandInvestigationStore(session_scope=_scope(connection, "ec_ingestion_executor"))
        lease = await _claim(repo)
        for column in ("id", "attempt", "token", "operation"):
            payload = {
                "id": identifier,
                "attempt": lease.attempt_count,
                "token": lease.lease_token,
                "operation": "get_plan",
            }
            payload[column] = None
            async with connection.begin_nested() as savepoint:
                with pytest.raises(DBAPIError, match="invalid command execution operation"):
                    await connection.execute(
                        text(
                            "SELECT public.fn_command_execution_v1(:id,:attempt,:token,:operation,'{}')"
                        ),
                        payload,
                    )
                await savepoint.rollback()
        with pytest.raises(DBAPIError, match="does not match intent"):
            await store.ensure_plan(
                lease, (IngestionCommandRunTarget(0, second, f"admin:{identifier}"),)
            )
        [task] = await store.ensure_plan(
            lease, (IngestionCommandRunTarget(0, first, f"admin:{identifier}"),)
        )
        with pytest.raises(DBAPIError, match="invalid execution identity"):
            await store.start_task(
                lease,
                task,
                worker_id="raw secret text",
                release_revision="build",
                image_digest=None,
            )
        started = await store.start_task(
            lease, task, worker_id="fixture", release_revision="build", image_digest=None
        )
        assert started is not None
        with pytest.raises(DBAPIError, match="invalid command task result"):
            await store.finish_task(
                lease, started, "succeeded", candidate_count=1, canonical_count=2
            )
        with pytest.raises(DBAPIError, match="invalid structured command event"):
            await store.record_event(
                lease,
                "progress",
                source_key=first,
                run_key=task.run_key,
                task_attempt=1,
                stage="collect",
                request_count=-1,
            )
        assert not (
            await store._execute(
                lease,
                "finish_task",
                {"source_key": first, "run_key": task.run_key, "outcome_code": "succeeded"},
            )
        )["updated"]
        assert await store.finish_task(lease, started, "succeeded")
        assert not await store.record_event(
            lease,
            "progress",
            source_key=first,
            run_key=task.run_key,
            task_attempt=1,
            stage="collect",
            page_count=99,
        )


async def test_real_processor_yields_resumes_and_preserves_exact_run_evidence() -> None:
    async with _owner_transaction() as connection:
        first, second = await _source(connection), await _source(connection)
        fleet = await _command(connection)
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_ingestion_executor")
        )
        store = CommandInvestigationStore(session_scope=_scope(connection, "ec_ingestion_executor"))
        reader = CommandInvestigationStore(session_scope=_scope(connection, "ec_operator_viewer"))
        lease = await _claim(repo)
        await store.ensure_plan(
            lease,
            (
                IngestionCommandRunTarget(0, first, f"cadence:{first}:fixed"),
                IngestionCommandRunTarget(1, second, f"cadence:{second}:fixed"),
            ),
        )
        assert await repo.defer(lease, 1)
        await _role(connection, None)
        await connection.execute(
            text(
                "UPDATE public.ingestion_admin_commands SET available_at=clock_timestamp()-interval '1 second' WHERE command_id=:id"
            ),
            {"id": fleet},
        )
        calls: list[tuple[str, str]] = []

        class Router:
            async def refresh(self, source_key: str, run_key: str) -> CatalogRefreshResult:
                calls.append((source_key, run_key))
                await emit_ingestion_event("stage_started", stage="collect", outcome_code="started")
                await emit_ingestion_event(
                    "progress", stage="collect", request_count=3, page_count=2
                )
                await emit_ingestion_event(
                    "stage_completed",
                    stage="collect",
                    outcome_code="succeeded",
                    duration_ms=12,
                    candidate_count=3,
                )
                return CatalogRefreshResult(
                    source_key, run_key, CatalogRefreshOutcome.SUCCEEDED, 3, 2
                )

        processor = ResumableIngestionCommandProcessor(
            repo, Router(), store, worker_id="fixture-worker", release_revision="fixture-build"
        )
        assert (await processor.process_once(1)).deferred == 1
        manual = await _command(connection, first)
        assert (await processor.process_once(1)).completed == 1
        assert calls == [(first, f"cadence:{first}:fixed"), (first, f"admin:{manual}")]
        await _role(connection, None)
        await connection.execute(
            text(
                "UPDATE public.ingestion_admin_commands SET available_at=clock_timestamp()-interval '1 second' WHERE command_id=:id"
            ),
            {"id": fleet},
        )
        assert (await processor.process_once(1)).completed == 1
        assert calls[-1] == (second, f"cadence:{second}:fixed")
        snapshot = await reader.investigation(fleet)
        assert snapshot is not None
        assert [task["attempt_count"] for task in snapshot["plan"]["tasks"]] == [1, 1]
        assert sum(event["event_code"] == "progress" for event in snapshot["events"]) == 2
        assert snapshot["attempts_total"] == 3 and not snapshot["attempts_truncated"]
        await _role(connection, None)
        await connection.execute(
            text("""
          INSERT INTO public.catalog_refresh_runs(source_key,run_key,status,started_at,completed_at,candidate_count,canonical_count,attempt_count)
          VALUES(:source,:run,'succeeded',clock_timestamp()-interval '1 minute',clock_timestamp(),3,2,1),
                (:source,:other,'succeeded',clock_timestamp(),clock_timestamp(),99,99,1)
        """),
            {"source": first, "run": f"cadence:{first}:fixed", "other": f"cadence:{first}:newer"},
        )
        snapshot = await reader.investigation(fleet, source_key=first)
        assert snapshot is not None
        [selected] = [task for task in snapshot["plan"]["tasks"] if task["source_key"] == first]
        run = IngestionRunOut.model_validate(selected["run"])
        assert run.run_key == f"cadence:{first}:fixed" and run.candidate_count == 3
        assert run.command is not None and run.command.command_id == fleet
        assert run.execution is not None
        assert run.execution.worker_service == "ingestion-command-worker"


async def test_existing_command_adopts_latest_plan_without_inventing_old_attempts_and_normalizes_expired_sources() -> (
    None
):
    async with _owner_transaction() as connection:
        first, second = await _source(connection), await _source(connection)
        identifier, token = uuid4(), uuid4()
        await connection.execute(
            text("""
          INSERT INTO public.ingestion_admin_commands(command_id,action,requested_by,status,attempt_count,
            started_at,lease_token,lease_expires_at)
          VALUES(:id,'refresh_due','legacy-worker','running',4,clock_timestamp()-interval '2 hours',:token,
            clock_timestamp()+interval '5 minutes')
        """),
            {"id": identifier, "token": token},
        )
        await connection.execute(
            text("""
          INSERT INTO public.ingestion_admin_command_runs(command_id,command_attempt,position,source_key,run_key)
          VALUES(:id,3,0,:first,:first_run),(:id,3,1,:second,:second_run);
        """),
            {
                "id": identifier,
                "first": first,
                "second": second,
                "first_run": f"cadence:{first}:legacy",
                "second_run": f"cadence:{second}:legacy",
            },
        )
        await connection.execute(
            text("""
          INSERT INTO public.catalog_refresh_runs(source_key,run_key,status,started_at,completed_at,
            lease_token,lease_expires_at,candidate_count,canonical_count,attempt_count)
          VALUES(:first,:first_run,'succeeded',clock_timestamp()-interval '1 hour',clock_timestamp()-interval '30 minutes',NULL,NULL,3,2,3),
                (:second,:second_run,'running',clock_timestamp()-interval '1 hour',NULL,:token,clock_timestamp()-interval '1 minute',NULL,NULL,2)
        """),
            {
                "first": first,
                "second": second,
                "first_run": f"cadence:{first}:legacy",
                "second_run": f"cadence:{second}:legacy",
                "token": uuid4(),
            },
        )
        store = CommandInvestigationStore(session_scope=_scope(connection, "ec_ingestion_executor"))
        reader = CommandInvestigationStore(session_scope=_scope(connection, "ec_operator_viewer"))

        lease = IngestionCommandLease(
            identifier, IngestionCommandAction.REFRESH_DUE, None, 4, token
        )
        tasks = await store.get_plan(lease)
        assert tasks is not None and [task.status for task in tasks] == [
            "already_succeeded",
            "pending",
        ]
        snapshot = await reader.investigation(identifier, source_key=second)
        assert snapshot is not None and snapshot["plan"]["status"] == "adopted"
        assert len(snapshot["attempts"]) == 1 and snapshot["attempts"][0]["kind"] == "adopted"
        assert snapshot["attempts"][0]["claimed_at"] is None
        assert [event["event_code"] for event in snapshot["events"]] == ["plan_adopted"]
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        detail = await repo.get_command_detail(identifier)
        assert (
            detail is not None
            and detail.runs[1].status == "failed"
            and detail.runs[1].phase == "failed"
        )
