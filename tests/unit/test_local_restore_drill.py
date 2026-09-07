"""The recovery rehearsal is isolated, evidence-only, and cleanup-safe."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from events_concierge.operations.local_restore import _migration_heads, run_local_restore_drill


class _SuccessfulRunner:
    def __init__(self) -> None:
        self.commands: list[tuple[list[str], bytes | None]] = []
        self.snapshot = {
            "schema_head": _migration_heads(Path.cwd())[0],
            "account_erasure_calendar_targets": 0,
            "account_erasure_requests": 0,
            "account_erasure_workflow_targets": 0,
            "calendar_bindings": 1,
            "canonical_alias": 1,
            "canonical_events": 4,
            "catalog_event_observations": 2,
            "catalog_refresh_progress": 1,
            "catalog_refresh_runs": 1,
            "catalog_refresh_stage_candidates": 0,
            "catalog_refresh_stage_event_ids": 0,
            "catalog_refresh_stage_pages": 0,
            "catalog_sources": 2,
            "event_change_calendar_repairs": 0,
            "event_change_deliveries": 1,
            "event_changes": 1,
            "event_requests": 3,
            "event_source_links": 5,
            "google_calendar_sync_state": 1,
            "handoff_completion_attempts": 1,
            "handoff_expiry_queue": 1,
            "handoff_reminder_ledger": 1,
            "handoff_tasks": 1,
            "lifecycle": 1,
            "lifecycle_organizer_change_ledger": 1,
            "lifecycle_watch_projection_outbox": 0,
            "notification_ledger": 1,
            "outbox": 1,
            "policy_global_control": 1,
            "provider_budget_daily": 1,
            "provider_budget_ledger": 1,
            "provider_budget_scope": 1,
            "registration_action_audit": 1,
            "request_outcome_links": 1,
            "request_start_outbox": 0,
            "request_terminal_ledger": 1,
            "source_policy": 1,
            "tenant_context_violation_audit": 0,
            "tenant_policy_control": 1,
            "tenant_ranking_feedback_receipts": 1,
            "tenant_ranking_profiles": 1,
            "tenant_source_consents": 1,
            "tenants": 2,
            "transition_ledger": 2,
            "watch_poll_state": 1,
            "watch_registry": 1,
            "watch_subscriptions": 1,
        }
        self.sequence_states = {
            "registration_action_audit_audit_id_seq": {
                "last_value": 7,
                "is_called": True,
            }
        }

    def __call__(  # noqa: PLR0911 - command double mirrors the bounded drill protocol
        self,
        command: Sequence[str],
        input_data: bytes | None,
        timeout: float,
    ) -> subprocess.CompletedProcess[bytes]:
        del timeout
        argv = list(command)
        self.commands.append((argv, input_data))
        if "pg_dump" in argv:
            return subprocess.CompletedProcess(argv, 0, b"custom-format-dump", b"")
        if "pg_restore" in argv:
            assert input_data == b"custom-format-dump"
            return subprocess.CompletedProcess(argv, 0, b"", b"")
        if "psql" in argv:
            query = argv[-1]
            if "'schema_head'" in query:
                inventory = {
                    "schema_head": self.snapshot["schema_head"],
                    "tables": sorted(
                        name for name in self.snapshot if name != "schema_head"
                    ),
                    "sequences": sorted(self.sequence_states),
                }
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(inventory).encode() + b"\n",
                    b"",
                )
            if query.startswith("SELECT json_build_object('last_value'"):
                sequence = query.split('"')[1]
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(self.sequence_states[sequence]).encode() + b"\n",
                    b"",
                )
            if query.startswith("SELECT count(*)::bigint FROM public.\""):
                table = query.split('"')[1]
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    f"{self.snapshot[table]}\n".encode(),
                    b"",
                )
            if "'app_role_exists'" in query:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(
                        {
                            "app_role_exists": True,
                            "app_role_superuser": False,
                            "app_role_bypassrls": False,
                            "app_role_createdb": False,
                            "app_role_createrole": False,
                            "app_role_replication": False,
                            "app_role_memberships": 0,
                            "rls_tables": 14,
                            "unforced_rls_tables": 0,
                        }
                    ).encode()
                    + b"\n",
                    b"",
                )
            if "'visible_fixture_rows'" in query:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    b'\n{"visible_fixture_rows": 1, "cross_tenant_rows": 0}\n',
                    b"",
                )
        return subprocess.CompletedProcess(argv, 0, b"", b"")


def test_local_restore_rehearsal_compares_truth_proves_rls_and_destroys_target(
    monkeypatch: object,
) -> None:
    del monkeypatch
    runner = _SuccessfulRunner()

    report = run_local_restore_drill(Path.cwd(), runner=runner)

    assert report.passed is True
    assert report.backup_bytes == len(b"custom-format-dump")
    assert report.to_dict()["evidence_class"] == "local_recovery_rehearsal"
    assert report.to_dict()["managed_recovery_eligible"] is False
    assert report.to_dict()["release_eligible"] is False
    assert report.restored_database.startswith("ec_restore_drill_")
    assert report.restored_database_destroyed is True
    assert runner.sequence_states["registration_action_audit_audit_id_seq"]["last_value"] == 7
    assert {
        "registration_action_audit",
        "handoff_completion_attempts",
        "notification_ledger",
        "handoff_tasks",
        "lifecycle_watch_projection_outbox",
        "provider_budget_ledger",
        "request_start_outbox",
        "tenant_context_violation_audit",
        "watch_subscriptions",
        "request_outcome_links",
        "account_erasure_requests",
    }.issubset(runner.snapshot)
    assert {check.name for check in report.checks} >= {
        "single_migration_head",
        "complete_source_inventory",
        "source_at_migration_head",
        "backup_artifact",
        "restored_migration_head",
        "durable_aggregate_continuity",
        "restored_rls_posture",
        "restored_app_role_tenant_isolation",
        "isolated_restore_destroyed",
    }
    assert any("dropdb" in command for command, _ in runner.commands)
    assert "example.invalid" not in str(report.to_dict())


def test_restore_failure_still_destroys_the_isolated_database() -> None:
    class FailingRunner(_SuccessfulRunner):
        def __call__(
            self,
            command: Sequence[str],
            input_data: bytes | None,
            timeout: float,
        ) -> subprocess.CompletedProcess[bytes]:
            argv = list(command)
            if "pg_restore" in argv:
                self.commands.append((argv, input_data))
                return subprocess.CompletedProcess(argv, 9, b"", b"secret details omitted")
            return super().__call__(command, input_data, timeout)

    runner = FailingRunner()

    report = run_local_restore_drill(Path.cwd(), runner=runner)

    assert report.passed is False
    assert report.restored_database_destroyed is True
    assert next(check for check in report.checks if check.name == "drill_execution").detail == (
        "restore rehearsal failed closed (RuntimeError)"
    )
    assert "secret details omitted" not in str(report.to_dict())
    assert any("dropdb" in command for command, _ in runner.commands)


def test_source_schema_mismatch_aborts_before_dump_or_restore() -> None:
    runner = _SuccessfulRunner()
    runner.snapshot["schema_head"] = "old-head"

    report = run_local_restore_drill(Path.cwd(), runner=runner)

    assert report.passed is False
    assert any(check.name == "source_at_migration_head" and not check.passed for check in report.checks)
    assert not any("pg_dump" in command for command, _ in runner.commands)
    assert not any("createdb" in command for command, _ in runner.commands)


@pytest.mark.parametrize(
    "writer",
    [
        "api",
        "account-erasure",
        "entity-intelligence",
        "ingestion-cadence",
        "ingestion-commands",
        "workflow-worker",
        "request-starter",
        "notifier",
        "change-delivery",
        "handoff-expiry",
        "lifecycle-invariants",
    ],
)
def test_running_writer_service_aborts_before_reading_or_dumping_the_source(
    writer: str,
) -> None:
    class RunningWriterRunner(_SuccessfulRunner):
        def __call__(
            self,
            command: Sequence[str],
            input_data: bytes | None,
            timeout: float,
        ) -> subprocess.CompletedProcess[bytes]:
            argv = list(command)
            if "ps" in argv and "--services" in argv:
                self.commands.append((argv, input_data))
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    f"postgres\n{writer}\n".encode(),
                    b"",
                )
            return super().__call__(command, input_data, timeout)

    runner = RunningWriterRunner()

    report = run_local_restore_drill(Path.cwd(), runner=runner)

    assert report.passed is False
    assert next(check for check in report.checks if check.name == "quiescent_local_source") == (
        report.checks[0]
    )
    assert report.checks[0].passed is False
    assert not any("pg_dump" in command for command, _ in runner.commands)
