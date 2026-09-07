"""Migration contract for reviewed anonymous Meetup city sources."""

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
        Path(__file__).parents[2] / "migrations" / "versions" / "0126_meetup_city_jsonld_sources.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_closed_mode_and_two_enabled_reviewed_city_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    assert migration.revision == "0126"
    assert migration.down_revision == "0125"
    assert len(operations.statements) == 3
    drop_constraint, add_constraint, seed_sources = operations.statements
    assert drop_constraint == (
        "ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode"
    )
    assert "'luma_discover_json', 'meetup_city_jsonld'" in add_constraint
    assert "'meetup-sf', 'Meetup San Francisco', 'Meetup City'" in seed_sources
    assert "'https://www.meetup.com/find/us--ca--san-francisco/'" in seed_sources
    assert "'meetup-nyc', 'Meetup New York', 'Meetup City'" in seed_sources
    assert "'https://www.meetup.com/find/us--ny--new-york/'" in seed_sources
    assert "ARRAY['https://www.meetup.com']" in seed_sources
    assert "'meetup_city_jsonld', true, true, now(), NULL, 120, 1500, 1, 1" in seed_sources
    assert "ON CONFLICT (source_key) DO UPDATE" in seed_sources


def test_downgrade_disables_retained_rows_before_removing_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert len(operations.statements) == 3
    retire_sources, drop_constraint, add_constraint = operations.statements
    assert "SET mode = 'public_jsonld'" in retire_sources
    assert "enabled = false" in retire_sources
    assert "WHERE source_key IN ('meetup-sf', 'meetup-nyc')" in retire_sources
    assert drop_constraint == (
        "ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode"
    )
    assert "meetup_city_jsonld" not in add_constraint
    assert "'luma_discover_json'" in add_constraint
