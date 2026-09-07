"""Migration contract for bounded Meetup event-detail enrichment."""

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
        / "0137_meetup_city_detail_enrichment.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_expands_only_exact_reviewed_meetup_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0137"
    assert migration.down_revision == "0136"
    assert len(operations.statements) == 1
    statement = operations.statements[0]
    assert "SET page_limit = 41" in statement
    assert "source_revision = source_revision + 1" in statement
    assert "mode = 'meetup_city_jsonld'" in statement
    assert "AND handoff_only" in statement
    assert "AND page_limit = 1" in statement
    assert "approved_origins = ARRAY['https://www.meetup.com']::text[]" in statement
    assert "source_key = 'meetup-sf'" in statement
    assert "https://www.meetup.com/find/us--ca--san-francisco/" in statement
    assert "source_key = 'meetup-nyc'" in statement
    assert "https://www.meetup.com/find/us--ny--new-york/" in statement


def test_downgrade_restores_one_city_request_without_reusing_a_revision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert len(operations.statements) == 1
    statement = operations.statements[0]
    assert "SET page_limit = 1" in statement
    assert "AND handoff_only" in statement
    assert "AND page_limit = 41" in statement
    assert "source_revision = source_revision + 1" in statement
    assert "source_key = 'meetup-sf'" in statement
    assert "source_key = 'meetup-nyc'" in statement
