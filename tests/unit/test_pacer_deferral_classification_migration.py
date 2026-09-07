"""Migration contract for no-egress catalog Pacer deferral classification."""

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
        / "0129_reclassify_catalog_pacer_deferrals.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_reclassifies_only_closed_pacer_forms_and_restores_pause_invariants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0129"
    assert migration.down_revision == "0128"
    assert len(operations.statements) == 1
    statement = operations.statements[0]
    assert "SET status = 'paused'" in statement
    assert "refresh.status IN ('failed', 'paused')" in statement
    assert "Pacer (wait|degrade|saturated)" in statement
    assert "char_length(refresh.error) BETWEEN 13 AND 2000" in statement
    assert "[^[:cntrl:]]+" in statement
    assert "lease_token = NULL" in statement
    assert "lease_expires_at = NULL" in statement
    assert "completed_at = NULL" in statement
    assert "fn_normalize_catalog_refresh_error" not in statement
    assert "DELETE" not in statement


def test_downgrade_does_not_corrupt_existing_paged_pause_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert operations.statements == []
