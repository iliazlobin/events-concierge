"""Restart-safety coverage for the request-outcome online index migration."""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping
from pathlib import Path
from types import ModuleType
from typing import Any, Self

import pytest


class _MappingResult:
    """Expose the small SQLAlchemy result surface used by the migration helper."""

    def __init__(self, row: Mapping[str, object] | None) -> None:
        self._row = row

    def mappings(self) -> Self:
        """Return the mapping result adapter."""
        return self

    def one_or_none(self) -> Mapping[str, object] | None:
        """Return the configured catalog row, if one exists."""
        return self._row


class _RecordingBind:
    """Return one deterministic PostgreSQL catalog state and record its lookup."""

    def __init__(self, row: Mapping[str, object] | None) -> None:
        self._row = row
        self.calls: list[tuple[object, Mapping[str, object]]] = []

    def execute(
        self,
        statement: object,
        parameters: Mapping[str, object],
    ) -> _MappingResult:
        """Capture the catalog lookup and return its configured result."""
        self.calls.append((statement, parameters))
        return _MappingResult(self._row)


class _RecordingOperations:
    """Capture Alembic operations without requiring an active migration context."""

    def __init__(self, row: Mapping[str, object] | None) -> None:
        self.bind = _RecordingBind(row)
        self.statements: list[str] = []

    def get_bind(self) -> _RecordingBind:
        """Return the fake migration connection."""
        return self.bind

    def execute(self, statement: str) -> None:
        """Record DDL emitted by the helper."""
        self.statements.append(statement)


def _load_migration() -> ModuleType:
    """Load revision 0107 without mutating Alembic's process-global operation proxy."""
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0107_request_outcome_links.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _index_inputs(migration: ModuleType) -> tuple[str, str, str]:
    return migration._ONLINE_UNIQUE_INDEXES[0]  # type: ignore[attr-defined, no-any-return]


def _catalog_row(expected_definition: str, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "is_valid": True,
        "is_ready": True,
        "is_unique": True,
        "definition": expected_definition,
        "is_constraint_owned": False,
    }
    row.update(overrides)
    return row


def _run_helper(
    monkeypatch: pytest.MonkeyPatch,
    row: Mapping[str, object] | None,
) -> tuple[ModuleType, _RecordingOperations, str, str, str]:
    migration = _load_migration()
    index_name, expected_definition, create_sql = _index_inputs(migration)
    operations = _RecordingOperations(row)
    monkeypatch.setattr(migration, "op", operations)

    migration._ensure_online_unique_index(  # type: ignore[attr-defined]
        index_name,
        expected_definition,
        create_sql,
    )
    return migration, operations, index_name, expected_definition, create_sql


def test_online_index_is_created_when_catalog_entry_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clean upgrade emits exactly the concurrent index creation statement."""
    migration, operations, index_name, _, create_sql = _run_helper(monkeypatch, None)

    assert operations.bind.calls == [
        (migration._INDEX_STATE, {"index_name": index_name})  # type: ignore[attr-defined]
    ]
    assert operations.statements == [create_sql]


def test_exact_healthy_unattached_index_is_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry leaves a completed, migration-owned concurrent index untouched."""
    migration = _load_migration()
    _, expected_definition, _ = _index_inputs(migration)
    row = _catalog_row(expected_definition)

    _, operations, _, _, _ = _run_helper(monkeypatch, row)

    assert operations.statements == []


@pytest.mark.parametrize(
    "overrides",
    (
        {"is_valid": False},
        {"is_ready": False},
        {"is_unique": False},
        {
            "definition": "CREATE UNIQUE INDEX stale_index ON public.event_requests "
            "USING btree (request_id, tenant_id)"
        },
    ),
    ids=("invalid", "not-ready", "not-unique", "stale-definition"),
)
def test_unhealthy_unattached_index_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    overrides: Mapping[str, Any],
) -> None:
    """Invalid, incomplete, non-unique, and stale named indexes are rebuilt on retry."""
    migration = _load_migration()
    _, expected_definition, _ = _index_inputs(migration)
    row = _catalog_row(expected_definition, **overrides)

    _, operations, index_name, _, create_sql = _run_helper(monkeypatch, row)

    assert operations.statements == [
        f"DROP INDEX CONCURRENTLY IF EXISTS public.{index_name}",
        create_sql,
    ]


def test_constraint_owned_index_is_rejected_as_revision_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constraint-owned name below 0107 fails closed instead of dropping live schema."""
    migration = _load_migration()
    index_name, expected_definition, create_sql = _index_inputs(migration)
    operations = _RecordingOperations(
        _catalog_row(expected_definition, is_constraint_owned=True)
    )
    monkeypatch.setattr(migration, "op", operations)

    with pytest.raises(
        RuntimeError,
        match=rf"public\.{index_name} is already constraint-owned.*below revision 0107",
    ):
        migration._ensure_online_unique_index(  # type: ignore[attr-defined]
            index_name,
            expected_definition,
            create_sql,
        )

    assert operations.statements == []
