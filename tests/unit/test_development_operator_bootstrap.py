from pathlib import Path
from unittest.mock import Mock

import pytest

from events_concierge.deployment import development_operator_bootstrap as dev

VALID = {
    "environment": "development",
    "mock_cloud": "true",
    "connection_mode": "development_plaintext",
    "migration_url": "postgresql+psycopg://ec_owner:fixture@ec-dev-application-postgres:5432/events",
}


def test_development_target_is_explicit() -> None:
    dev.validate_development_bootstrap(**VALID)


@pytest.mark.parametrize(
    "key,value",
    [
        ("environment", "local"),
        ("environment", "production"),
        ("mock_cloud", "false"),
        ("connection_mode", "direct_tls"),
        (
            "migration_url",
            "postgresql+psycopg://ec_app:fixture@ec-dev-application-postgres:5432/events",
        ),
        ("migration_url", "postgresql+psycopg://ec_owner:fixture@localhost:5432/events"),
        ("migration_url", VALID["migration_url"] + "?host=other"),
        ("migration_url", VALID["migration_url"] + "#other"),
        ("migration_url", VALID["migration_url"].replace(":5432", ":bad")),
        ("migration_url", VALID["migration_url"].replace("/events", "/production")),
    ],
)
def test_development_target_rejects_ambiguous_settings(key: str, value: str) -> None:
    with pytest.raises(ValueError, match="fixed private development"):
        dev.validate_development_bootstrap(**(VALID | {key: value}))


@pytest.fixture
def bootstrap_mocks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Mock:
    for key, value in {
        "EC_ENV": VALID["environment"],
        "EC_MOCK_CLOUD": VALID["mock_cloud"],
        "EC_DATABASE_CONNECTION_MODE": VALID["connection_mode"],
        "EC_MIGRATION_URL": VALID["migration_url"],
        "EC_DEV_OPERATOR_PASSWORD": "operator-fixture",
        "EC_DEV_INGESTION_PASSWORD": "executor-fixture",
    }.items():
        monkeypatch.delenv(key, raising=False)
        secret = tmp_path / key
        secret.write_text(value + "\n")
        monkeypatch.setenv(key + "_FILE", str(secret))
    # Non-secret settings are environment values rather than mounted secrets.
    for key, value in {
        "EC_ENV": "development",
        "EC_MOCK_CLOUD": "true",
        "EC_DATABASE_CONNECTION_MODE": "development_plaintext",
    }.items():
        monkeypatch.setenv(key, value)
        monkeypatch.delenv(key + "_FILE")
    calls = Mock()
    calls.preflight.return_value = ("ec_dev_operator",)
    monkeypatch.setattr(dev, "create_engine", calls.engine)
    monkeypatch.setattr(dev, "preflight_operator_logins", calls.preflight)
    monkeypatch.setattr(dev, "verify_login_passwords", calls.verify)
    monkeypatch.setattr(dev, "bootstrap_operator_logins", calls.provision)
    monkeypatch.setattr(dev.subprocess, "run", calls.migrate)
    return calls


def test_mounted_credentials_are_checked_before_migration_and_after_provision(
    bootstrap_mocks: Mock,
) -> None:
    dev.bootstrap(migrate=True)
    names = [call[0] for call in bootstrap_mocks.mock_calls]
    assert names == [
        "engine",
        "preflight",
        "verify",
        "migrate",
        "provision",
        "verify",
        "engine().dispose",
    ]
    assert bootstrap_mocks.verify.call_args_list[0].args[1] == (
        ("ec_dev_operator", "ec_operator_controller", "operator-fixture"),
    )
    assert len(bootstrap_mocks.verify.call_args_list[1].args[1]) == 2
    bootstrap_mocks.migrate.assert_called_once_with(["alembic", "upgrade", "head"], check=True)


@pytest.mark.parametrize("failure", ["preflight", "verify"])
def test_preflight_failure_never_migrates(bootstrap_mocks: Mock, failure: str) -> None:
    getattr(bootstrap_mocks, failure).side_effect = RuntimeError("preflight failed")
    with pytest.raises(RuntimeError, match="preflight failed"):
        dev.bootstrap(migrate=True)
    bootstrap_mocks.migrate.assert_not_called()
    bootstrap_mocks.provision.assert_not_called()
    bootstrap_mocks.engine.return_value.dispose.assert_called_once()


def test_missing_credentials_never_connect_or_migrate(
    bootstrap_mocks: Mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("EC_DEV_INGESTION_PASSWORD_FILE")
    with pytest.raises(ValueError, match="invalid operator bootstrap password"):
        dev.bootstrap(migrate=True)
    bootstrap_mocks.engine.assert_not_called()
    bootstrap_mocks.migrate.assert_not_called()


def test_post_migration_failure_is_not_automatically_rolled_back(bootstrap_mocks: Mock) -> None:
    bootstrap_mocks.provision.side_effect = RuntimeError("grant failed")
    with pytest.raises(RuntimeError, match="grant failed"):
        dev.bootstrap(migrate=True)
    bootstrap_mocks.migrate.assert_called_once_with(["alembic", "upgrade", "head"], check=True)
    bootstrap_mocks.engine.return_value.dispose.assert_called_once()
