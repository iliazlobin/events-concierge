"""The application database role is never created with a repository-known production password."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MIGRATION = Path(__file__).parents[2] / "migrations" / "versions" / "0002_app_role.py"


def _load_migration():
    spec = importlib.util.spec_from_file_location("migration_0002_app_role", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_source_contains_no_development_password_literal() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")

    assert "APP_PASSWORD" not in source
    assert "LOGIN PASSWORD 'ec_app'" not in source
    assert "EC_APP_ROLE_PASSWORD" in source
    assert "statement = bind.execute" not in source


@pytest.mark.parametrize("password", ["", "bad\x00secret", "x" * 1025])
def test_role_bootstrap_rejects_invalid_secret_material(password: str) -> None:
    migration = _load_migration()

    with pytest.raises(ValueError, match="EC_APP_ROLE_PASSWORD"):
        migration._validate_password(password)


def test_role_bootstrap_accepts_bounded_secret_material() -> None:
    migration = _load_migration()

    migration._validate_password("fixture-long-random-application-password")
