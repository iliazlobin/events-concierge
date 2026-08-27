"""Migration contract for the failure-aware cadence due-source projection."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


class _RecordingOperations:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0155_cadence_failure_aware_due_sources.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _upgrade_sql(monkeypatch: pytest.MonkeyPatch) -> tuple[ModuleType, str]:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)
    migration.upgrade()
    return migration, "\n".join(operations.statements)


def test_upgrade_chains_onto_the_catalog_multi_source_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, _ = _upgrade_sql(monkeypatch)

    assert migration.revision == "0155"
    assert migration.down_revision == "0154"


def test_upgrade_creates_the_gated_projection_with_the_owner_defined_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    assert "CREATE FUNCTION public.fn_list_ingestion_admin_due_sources_v3" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = pg_catalog, public" in sql
    assert "ingestion admin due-source query is invalid" in sql
    assert "p_limit < 1 OR p_limit > 500" in sql


def test_upgrade_delegates_eligibility_and_slot_identity_to_the_existing_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    # Retirement, review and due_at stay owned by _v2 so one slot keeps one durable run key.
    assert "public.fn_list_ingestion_admin_due_sources_v2(p_now, 500)" in sql
    assert "candidate.due_at" in sql
    # The gate must not recompute a slot timestamp of its own.
    assert "refresh_interval_minutes * INTERVAL" not in sql


def test_upgrade_withdraws_configuration_defects_from_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, sql = _upgrade_sql(monkeypatch)

    assert migration._TERMINAL_ERROR_CODES == (
        "page_cap_exceeded",
        "source_access_denied",
    )
    # A fail-closed policy store and the kill switch normalize to `policy_blocked` and clear on
    # their own; parking the roster on one of those would turn an outage into manual recovery.
    assert "policy_blocked" not in sql
    for code in migration._TERMINAL_ERROR_CODES:
        assert f"latest.attempt_error_code IS DISTINCT FROM '{code}'" in sql
    assert "public.fn_normalize_catalog_refresh_error" in sql


def test_upgrade_breaks_the_circuit_on_a_runaway_attempt_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, sql = _upgrade_sql(monkeypatch)

    assert migration._ATTEMPT_BREAK == 20
    assert f"latest.attempt_count < {migration._ATTEMPT_BREAK}" in sql


def test_upgrade_backs_off_exponentially_from_the_last_completed_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, sql = _upgrade_sql(monkeypatch)

    assert migration._BACKOFF_CEILING_HOURS == 24
    # The shift must overrun the ceiling, otherwise LEAST() never clamps and the documented
    # once-a-day floor is not the behavior.
    assert 2**migration._BACKOFF_MAX_SHIFT * migration._BACKOFF_BASE_MINUTES > (
        migration._BACKOFF_CEILING_HOURS * 60
    )
    assert f"INTERVAL '{migration._BACKOFF_CEILING_HOURS} hours'" in sql
    assert "power(" in sql
    assert "latest.attempt_completed_at" in sql


def test_upgrade_gates_on_cadence_history_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration, sql = _upgrade_sql(monkeypatch)

    # A manual `admin:` retry must not park a slot, nor reset an accumulated cadence backoff.
    assert migration._CADENCE_RUN_KEY_PREFIX == "cadence:"
    assert "refresh.run_key LIKE 'cadence:%'" in sql


def test_upgrade_orders_healthy_slots_by_their_stable_due_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    # The scheduler probes with limit=1, so ordering decides the whole fleet slot.
    assert "ORDER BY candidate.due_at, candidate.source_key" in sql
    assert "LIMIT p_limit" in sql


def test_upgrade_reopens_the_gate_after_an_operator_configuration_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    assert "registry.updated_at > COALESCE(" in sql
    # completed_at is NULL for the whole time a slot is executing; started_at never is.
    assert "latest.attempt_started_at" in sql


def test_upgrade_leaves_healthy_and_in_flight_slots_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    assert "latest.attempt_source_key IS NULL" in sql
    assert "latest.attempt_status <> 'failed'" in sql


def test_upgrade_excludes_fixture_runs_from_the_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    assert "NOT public.fn_ingestion_admin_run_is_fixture" in sql


def test_upgrade_grants_execute_only_to_the_runtime_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, sql = _upgrade_sql(monkeypatch)

    qualified = (
        "public.fn_list_ingestion_admin_due_sources_v3(timestamp with time zone,integer)"
    )
    assert f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC" in sql
    assert f"GRANT EXECUTE ON FUNCTION {qualified} TO ec_app" in sql


def test_downgrade_drops_only_the_new_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "DROP FUNCTION IF EXISTS public.fn_list_ingestion_admin_due_sources_v3" in sql
    assert "fn_list_ingestion_admin_due_sources_v2" not in sql
