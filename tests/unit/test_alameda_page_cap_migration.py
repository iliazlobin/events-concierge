"""Exercise the source-cap repair's data guards without a shared Postgres role mutation."""

from __future__ import annotations

import importlib.util
import sqlite3
from contextlib import closing
from pathlib import Path
from types import ModuleType

import pytest

_SOURCE_KEY = "alameda-county-library-all-physical-branches-events"


class _DatabaseOperations:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def execute(self, statement: str) -> None:
        self.connection.execute(statement)


def _load_migration() -> ModuleType:
    path = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0194_alameda_bibliocommons_page_cap_headroom.py"
    )
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("page_limit", [20, 40, 80, 120])
@pytest.mark.parametrize("enabled", [False, True])
def test_cap_repair_preserves_operator_choices_and_other_sources(
    page_limit: int, enabled: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = _load_migration()
    # The migration's portable UPDATE runs against real rows. Postgres registry/trigger coverage
    # remains in test_catalog_sources; this test isolates the upgrade and rollback data guards.
    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(
            """CREATE TABLE catalog_sources (
                source_key TEXT PRIMARY KEY, page_limit INTEGER, enabled INTEGER,
                seed_url TEXT, min_interval_ms INTEGER, reviewed_at TEXT,
                review_expires_at TEXT, refresh_interval_minutes INTEGER,
                collection_horizon_days INTEGER, updated_at TEXT
            )"""
        )
        controls = (
            enabled, "https://gateway.bibliocommons.com/reviewed-operator-filter",
            5_000, "2026-07-17", "2027-01-01", 720, 45, "2026-08-01",
        )
        original = [
            (_SOURCE_KEY, page_limit, *controls),
            ("alameda-county-library-fremont-events", 40, *controls),
            ("another-library-events", 40, *controls),
        ]
        connection.executemany("INSERT INTO catalog_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", original)
        monkeypatch.setattr(migration, "op", _DatabaseOperations(connection))

        migration.upgrade()

        actual = connection.execute("SELECT * FROM catalog_sources ORDER BY source_key").fetchall()
        for before, after in zip(sorted(original), actual, strict=True):
            if before[0] == _SOURCE_KEY and page_limit == 40:
                assert after[1] == 80
                assert after[2:-1] == before[2:-1]
                assert after[-1] != before[-1]
            else:
                assert after == before
        # Reapplying and rolling back cannot reset an operator cap or modify review controls.
        migration.upgrade()
        migration.downgrade()
        assert connection.execute(
            "SELECT * FROM catalog_sources ORDER BY source_key"
        ).fetchall() == actual
