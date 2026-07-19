"""Fail-closed rollback coverage for catalog-source disable migrations (FR-10.3/NFR-8)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


class _RecordingOperations:
    """Capture Alembic operations without binding an engine or migration environment."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: str) -> None:
        """Record a migration SQL statement."""
        self.statements.append(statement)


def _load_migration(filename: str) -> ModuleType:
    """Load one version script in isolation so the Alembic global state is untouched."""
    path = Path(__file__).parents[2] / "migrations" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "filename",
    (
        "0071_disable_legacy_smcl_millbrae_source.py",
        "0073_disable_legacy_sccld_sources.py",
        "0075_disable_legacy_alameda_fremont_source.py",
        "0077_disable_berkeley_livewhale_source.py",
        "0078_disable_scu_smccd_livewhale_sources.py",
        "0080_disable_localist_sources.py",
        "0081_disable_tribe_sources.py",
        "0082_disable_los_altos_civic_engage_source.py",
        "0083_disable_usfca_calperformances_sources.py",
        "0084_disable_midpen_berkeley_public_library_sources.py",
        "0085_disable_berkeley_rep_ybca_sources.py",
        "0086_disable_sf_civic_sources.py",
    ),
)
def test_source_disable_downgrades_do_not_reenable_sources(
    filename: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Downgrades must not overwrite an unrecorded operator safety disable (FR-10.3/NFR-8)."""
    migration = _load_migration(filename)
    operations = _RecordingOperations()
    monkeypatch.setattr(migration, "op", operations)

    migration.downgrade()

    assert operations.statements == []
