"""Focused guard coverage for the disposable integration-test entry point."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url
from tests.support import run_isolated_integration as runner
from tests.support.integration_database import (
    isolated_database_name,
    maintenance_url,
    replace_database,
    validate_isolated_test_environment,
)

_RUN_ID = "0123456789abcdef0123456789abcdef"
_DATABASE = f"ec_test_{_RUN_ID}"
_BASE_ENVIRONMENT = {
    "EC_DATABASE_URL": "postgresql+psycopg://ec_app:ec_app@localhost:5433/ec",
    "EC_MIGRATION_URL": "postgresql+psycopg://ec:ec@localhost:5433/ec",
}


def test_make_slice_uses_the_disposable_database_runner() -> None:
    """The documented smoke must never invoke its mutating module against runtime ``ec``."""
    makefile = (Path(__file__).resolve().parents[2] / "Makefile").read_text(encoding="utf-8")
    target = makefile.split("\nslice:", maxsplit=1)[1].split("\n\n", maxsplit=1)[0]

    assert target.startswith(" up ## Run the end-to-end vertical slice in a disposable database")
    assert "EC_MIGRATION_URL=postgresql+psycopg://ec:ec@localhost:5433/ec" in target
    assert "python -m tests.support.run_isolated_integration --" in target
    assert "test_documented_slice_demo_replays_against_a_persistent_catalog" in target
    assert "python -m events_concierge.slice_demo" not in target


@pytest.mark.parametrize(
    "run_id",
    ["", "abc", "A" * 32, "0" * 31, "0" * 33, "0" * 31 + "-"],
)
def test_isolated_database_name_rejects_every_non_uuid_hex_identifier(run_id: str) -> None:
    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        isolated_database_name(run_id)


def test_isolated_database_name_accepts_only_the_exact_generated_shape() -> None:
    assert isolated_database_name(_RUN_ID) == _DATABASE


def test_database_url_helpers_preserve_roles_and_hide_no_runtime_state() -> None:
    app = replace_database(
        "postgresql+psycopg://ec_app:secret@localhost:5433/ec?sslmode=disable", _DATABASE
    )
    parsed = make_url(app)
    assert parsed.username == "ec_app"
    assert parsed.password == "secret"
    assert parsed.database == _DATABASE
    assert parsed.query["sslmode"] == "disable"

    owner = maintenance_url("postgresql+psycopg://ec:owner@localhost:5433/ec")
    assert owner.username == "ec"
    assert owner.database == "postgres"


@pytest.mark.parametrize(
    "query_key",
    [
        "database",
        "dbname",
        "host",
        "hostaddr",
        "passfile",
        "password",
        "port",
        "service",
        "servicefile",
        "user",
    ],
)
def test_database_url_helpers_reject_query_options_that_can_override_identity(
    query_key: str,
) -> None:
    unsafe = f"postgresql+psycopg://ec:ec@localhost:5433/ec?{query_key}=runtime-override"

    with pytest.raises(ValueError, match="cannot override connection identity"):
        replace_database(unsafe, _DATABASE)
    with pytest.raises(ValueError, match="cannot override connection identity"):
        maintenance_url(unsafe)


def test_isolation_guard_rejects_query_database_override_even_when_path_is_safe() -> None:
    unsafe = {
        "EC_INTEGRATION_TEST_RUN_ID": _RUN_ID,
        "EC_DATABASE_URL": (
            f"postgresql+psycopg://ec_app:ec_app@localhost:5433/{_DATABASE}?dbname=ec"
        ),
        "EC_MIGRATION_URL": (f"postgresql+psycopg://ec:ec@localhost:5433/{_DATABASE}?dbname=ec"),
    }

    with pytest.raises(ValueError, match="cannot override connection identity"):
        validate_isolated_test_environment(unsafe)


def test_isolation_guard_requires_both_urls_to_match_the_exact_run_id() -> None:
    valid = {
        "EC_INTEGRATION_TEST_RUN_ID": _RUN_ID,
        "EC_DATABASE_URL": f"postgresql+psycopg://ec_app:ec_app@localhost:5433/{_DATABASE}",
        "EC_MIGRATION_URL": f"postgresql+psycopg://ec:ec@localhost:5433/{_DATABASE}",
    }
    assert validate_isolated_test_environment(valid) == _DATABASE

    for variable in ("EC_DATABASE_URL", "EC_MIGRATION_URL"):
        unsafe = valid | {variable: "postgresql+psycopg://ec:ec@localhost:5433/ec"}
        with pytest.raises(ValueError, match="must point at the isolated database"):
            validate_isolated_test_environment(unsafe)


def test_isolation_guard_rejects_direct_service_backed_pytest_invocations() -> None:
    with pytest.raises(ValueError, match="use `make test-integration`"):
        validate_isolated_test_environment(
            {
                "EC_DATABASE_URL": "postgresql+psycopg://ec_app:ec_app@localhost:5433/ec",
                "EC_MIGRATION_URL": "postgresql+psycopg://ec:ec@localhost:5433/ec",
            }
        )


def test_runner_migrates_and_tests_one_fresh_database_then_drops_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[str] = []
    dropped: list[str] = []
    child_calls: list[tuple[list[str], dict[str, str]]] = []

    monkeypatch.setattr(runner, "_create_database", lambda _url, name: created.append(name))
    monkeypatch.setattr(runner, "_drop_database", lambda _url, name: dropped.append(name))

    def invoke(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        child_calls.append((command, env))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", invoke)

    assert runner.run(["-m", "integration"], dict(_BASE_ENVIRONMENT)) == 0
    assert len(created) == 1
    assert dropped == created
    assert created[0].startswith("ec_test_")
    assert len(child_calls) == 2
    assert child_calls[0][0][1:] == ["-m", "alembic", "upgrade", "head"]
    assert child_calls[1][0][1:] == ["-m", "pytest", "-m", "integration"]
    child_environment = child_calls[1][1]
    assert validate_isolated_test_environment(child_environment) == created[0]
    assert child_environment["EC_TEMPORAL_TASK_QUEUE"] == (
        f"events-concierge-test-{child_environment['EC_INTEGRATION_TEST_RUN_ID']}"
    )


def test_runner_drops_database_and_skips_pytest_when_migration_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[str] = []
    dropped: list[str] = []
    commands: list[list[str]] = []
    monkeypatch.setattr(runner, "_create_database", lambda _url, name: created.append(name))
    monkeypatch.setattr(runner, "_drop_database", lambda _url, name: dropped.append(name))

    def fail_migration(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        del env
        assert check is False
        commands.append(command)
        return subprocess.CompletedProcess(command, 17)

    monkeypatch.setattr(runner.subprocess, "run", fail_migration)

    assert runner.run(["-m", "integration"], dict(_BASE_ENVIRONMENT)) == 17
    assert len(commands) == 1
    assert dropped == created


def test_runner_drops_database_when_pytest_is_interrupted(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[str] = []
    dropped: list[str] = []
    calls = 0
    monkeypatch.setattr(runner, "_create_database", lambda _url, name: created.append(name))
    monkeypatch.setattr(runner, "_drop_database", lambda _url, name: dropped.append(name))

    def interrupt_pytest(
        command: list[str], *, env: dict[str, str], check: bool
    ) -> subprocess.CompletedProcess[str]:
        nonlocal calls
        del env
        assert check is False
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(runner.subprocess, "run", interrupt_pytest)

    with pytest.raises(KeyboardInterrupt):
        runner.run(["-m", "integration"], dict(_BASE_ENVIRONMENT))
    assert dropped == created
