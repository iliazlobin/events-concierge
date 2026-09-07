"""Migration contract for topic persistence, facets, and narrow price fallback."""

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
    path = Path(__file__).parents[2] / "migrations" / "versions" / "0136_catalog_event_topics.py"
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_adds_bounded_topics_evidence_and_paging_safe_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.upgrade()

    sql = "\n".join(operations.statements)
    assert migration.revision == "0136"
    assert migration.down_revision == "0135"
    assert "ADD COLUMN topics text[]" in sql
    assert "extraction_evidence jsonb" in sql
    assert "'gaming'" in sql
    assert "fn_extract_catalog_event_semantics_v1" in sql
    assert "fn_browse_filtered_current_catalog_events_v7" in sql
    assert "fn_list_catalog_topic_facets_v1" in sql
    assert "event.topics @> coalesce(p_topics" in sql
    assert "super smash bros" in sql
    assert "cost\\s*:\\s*free" in sql
    assert "link.price_status = 'unknown'" in sql
    assert "SELECT count(*)" in sql and "sibling.canonical_event_id" in sql
    assert "VOLATILE" in sql
    assert "GRANT EXECUTE" in sql


def test_downgrade_removes_only_topic_capabilities_and_columns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    sql = "\n".join(operations.statements)
    assert "DROP FUNCTION IF EXISTS public.fn_list_catalog_topic_facets_v1" in sql
    assert "DROP FUNCTION IF EXISTS public.fn_browse_filtered_current_catalog_events_v7" in sql
    assert "DROP FUNCTION IF EXISTS public.fn_extract_catalog_event_semantics_v1" in sql
    assert "DROP COLUMN IF EXISTS extraction_evidence" in sql
    assert "DROP COLUMN IF EXISTS topics" in sql
