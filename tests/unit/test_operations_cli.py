"""Operations CLI preserves exit semantics and never overwrites release evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from events_concierge.operations import __main__ as operations_cli
from events_concierge.operations.app_role import AppRoleRotationReport
from events_concierge.operations.canary import CanaryOptions, CanaryReport


def test_structural_example_is_cli_valid_and_writes_sanitized_evidence(
    tmp_path: Path,
    capsys: object,
) -> None:
    del capsys
    output = tmp_path / "config.json"

    status = operations_cli.main(
        [
            "validate-config",
            "--env-file",
            "deployment/production.env.example",
            "--structural-only",
            "--output",
            str(output),
        ]
    )

    payload = json.loads(output.read_text())
    assert status == 0
    assert payload["status"] == "passed"
    assert "secret-manager-reference" not in output.read_text()


def test_cli_refuses_to_replace_existing_evidence(tmp_path: Path) -> None:
    output = tmp_path / "existing.json"
    output.write_text("preserve-me")

    status = operations_cli.main(
        [
            "validate-config",
            "--env-file",
            "deployment/production.env.example",
            "--structural-only",
            "--output",
            str(output),
        ]
    )

    assert status == 2
    assert output.read_text() == "preserve-me"


def test_invalid_config_returns_a_gate_failure_not_an_execution_error(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    del monkeypatch
    config = tmp_path / "unsafe.env"
    config.write_text("EC_ENV=local\nEC_MOCK_CLOUD=true\n")

    status = operations_cli.main(
        ["validate-config", "--env-file", str(config), "--structural-only"]
    )

    assert status == 1


def test_structural_file_rejects_a_migration_owner_credential(tmp_path: Path) -> None:
    source = Path("deployment/production.env.example").read_text()
    config = tmp_path / "unsafe-owner.env"
    config.write_text(source + "\nEC_MIGRATION_URL=postgresql://owner:secret@db.example/events\n")

    status = operations_cli.main(
        ["validate-config", "--env-file", str(config), "--structural-only"]
    )

    assert status == 1


def test_structural_file_rejects_an_exported_migration_owner_credential(
    tmp_path: Path,
) -> None:
    source = Path("deployment/production.env.example").read_text()
    config = tmp_path / "unsafe-exported-owner.env"
    config.write_text(
        source + "\nexport EC_MIGRATION_URL=postgresql://owner:secret@db.example/events\n"
    )

    status = operations_cli.main(
        ["validate-config", "--env-file", str(config), "--structural-only"]
    )

    assert status == 1


def test_structural_file_rejects_a_mounted_migration_owner_credential(
    tmp_path: Path,
) -> None:
    source = Path("deployment/production.env.example").read_text()
    config = tmp_path / "unsafe-owner-file.env"
    config.write_text(source + "\nEC_MIGRATION_URL_FILE=/var/run/secrets/migration-url\n")

    status = operations_cli.main(
        ["validate-config", "--env-file", str(config), "--structural-only"]
    )

    assert status == 1


def test_role_rotation_cli_reads_mounted_secrets_and_writes_only_sanitized_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migration_url = tmp_path / "migration-url"
    password = tmp_path / "app-role-password"
    migration_url.write_text("postgresql+psycopg://owner:fixture@database/events\n")
    password.write_text("fixture-new-app-password\n")
    monkeypatch.delenv("EC_MIGRATION_URL", raising=False)
    monkeypatch.delenv("EC_APP_ROLE_PASSWORD", raising=False)
    monkeypatch.setenv("EC_MIGRATION_URL_FILE", str(migration_url))
    monkeypatch.setenv("EC_APP_ROLE_PASSWORD_FILE", str(password))
    received: list[tuple[str, str]] = []

    def rotate(owner_url: str, new_password: str) -> AppRoleRotationReport:
        received.append((owner_url, new_password))
        return AppRoleRotationReport(
            schema_version=1,
            kind="events-concierge-app-role-password-rotation",
            role="ec_app",
                status="passed",
                role_can_login=True,
                elevated_attributes_absent=True,
                role_memberships_absent=True,
            )

    monkeypatch.setattr(operations_cli, "rotate_app_role_password", rotate)

    status = operations_cli.main(["rotate-app-role-password"])
    rendered = capsys.readouterr().out

    assert status == 0
    assert received == [
        (
            "postgresql+psycopg://owner:fixture@database/events",
            "fixture-new-app-password",
        )
    ]
    assert "fixture-new-app-password" not in rendered
    assert "owner:fixture" not in rendered
    assert json.loads(rendered)["status"] == "passed"


@pytest.mark.parametrize("profile", ["production", "private_google_pilot"])
def test_canary_cli_selects_and_records_profile_explicitly(
    profile: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    origin = (
        "https://localhost:14443"
        if profile == "private_google_pilot"
        else "https://staging.concierge.example"
    )
    observed: list[CanaryOptions] = []

    def canary(options: CanaryOptions) -> CanaryReport:
        observed.append(options)
        return CanaryReport(
            generated_at="2026-09-16T00:00:00+00:00",
            origin=options.base_url,
            expected_release_revision=options.expected_release_revision,
            expected_image_digest=options.expected_image_digest,
            checks=(),
            profile=options.profile,
        )

    monkeypatch.setattr(operations_cli, "run_canary", canary)
    monkeypatch.setenv("EC_CANARY_SESSION_COOKIE", "session=private-fixture")
    monkeypatch.setenv("EC_CANARY_CSRF_TOKEN", "private-csrf")
    arguments = ["canary", "--base-url", origin]
    if profile != "production":
        arguments += ["--profile", profile]
    assert operations_cli.main(arguments) == 0
    assert len(observed) == 1 and observed[0].profile == profile
    assert observed[0].session_cookie == "session=private-fixture"
    assert observed[0].csrf_token == "private-csrf"
    assert not observed[0].allow_http and not observed[0].allow_local_mode
    assert observed[0].require_temporal
    rendered = capsys.readouterr().out
    assert json.loads(rendered)["profile"] == profile
    assert "private-fixture" not in rendered and "private-csrf" not in rendered


@pytest.mark.parametrize("relaxation", ["--allow-http", "--allow-local-mode", "--allow-temporal-degraded"])
def test_private_google_canary_cli_rejects_relaxation_flags(
    relaxation: str, capsys: pytest.CaptureFixture[str]
) -> None:
    status = operations_cli.main(
        [
            "canary",
            "--base-url",
            "https://localhost:14443",
            "--profile",
            "private_google_pilot",
            relaxation,
        ]
    )
    assert status == 2
    assert "ValueError" in capsys.readouterr().err


def test_canary_cli_rejects_unknown_profile() -> None:
    with pytest.raises(SystemExit) as error:
        operations_cli.main(["canary", "--profile", "unsafe"])
    assert error.value.code == 2
