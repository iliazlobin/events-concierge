"""Deployment secret files are bounded, explicit, and never ambiguously overridden."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from events_concierge.config import Settings
from events_concierge.secret_files import read_secret_file, resolve_env_or_file


def test_resolve_env_or_file_accepts_inline_or_one_absolute_file(tmp_path: Path) -> None:
    secret = tmp_path / "database-url"
    secret.write_text("postgresql+psycopg://ec_app:secret@127.0.0.1/events\n", encoding="utf-8")

    assert resolve_env_or_file("EC_DATABASE_URL", environ={"EC_DATABASE_URL": "inline"}) == "inline"
    assert (
        resolve_env_or_file("EC_DATABASE_URL", environ={"EC_DATABASE_URL_FILE": str(secret)})
        == "postgresql+psycopg://ec_app:secret@127.0.0.1/events"
    )


def test_resolve_env_or_file_rejects_ambiguous_sources(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.write_text("mounted", encoding="utf-8")

    with pytest.raises(ValueError, match="exactly one"):
        resolve_env_or_file(
            "EC_TEMPORAL_API_KEY",
            environ={
                "EC_TEMPORAL_API_KEY": "inline",
                "EC_TEMPORAL_API_KEY_FILE": str(secret),
            },
        )


@pytest.mark.parametrize("value", ["relative", "", "missing"])
def test_secret_file_rejects_relative_or_unreadable_paths(value: str, tmp_path: Path) -> None:
    path = value if value == "relative" else str(tmp_path / value)

    with pytest.raises(ValueError, match="EC_REDIS_URL_FILE"):
        read_secret_file(path, setting_name="EC_REDIS_URL")


def test_secret_file_rejects_empty_nul_and_oversized_values(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    for value in ("", "bad\x00value", "x" * 65_537):
        secret.write_text(value, encoding="utf-8")
        with pytest.raises(ValueError, match="EC_OIDC_CLIENT_SECRET_FILE"):
            read_secret_file(str(secret), setting_name="EC_OIDC_CLIENT_SECRET")


def test_settings_resolve_mounted_connection_and_api_secrets(tmp_path: Path) -> None:
    database = tmp_path / "database-url"
    redis = tmp_path / "redis-url"
    temporal = tmp_path / "temporal-api-key"
    oidc = tmp_path / "oidc-client-secret"
    database.write_text("postgresql+psycopg://ec_app:secret@127.0.0.1/events\n")
    redis.write_text("rediss://:secret@redis.example/0\n")
    temporal.write_text("temporal-secret\n")
    oidc.write_text("oidc-secret\n")

    settings = Settings(
        database_url_file=str(database),
        redis_url_file=str(redis),
        temporal_api_key_file=str(temporal),
        oidc_client_secret_file=str(oidc),
    )

    assert settings.database_url.endswith("@127.0.0.1/events")
    assert settings.redis_url == "rediss://:secret@redis.example/0"
    assert settings.temporal_api_key is not None
    assert settings.temporal_api_key.get_secret_value() == "temporal-secret"
    assert settings.oidc_client_secret is not None
    assert settings.oidc_client_secret.get_secret_value() == "oidc-secret"
    assert not any(name.endswith("_file") for name in settings.model_dump())


def test_settings_reject_inline_and_mounted_secret_ambiguity(tmp_path: Path) -> None:
    secret = tmp_path / "database-url"
    secret.write_text("mounted")

    with pytest.raises(ValidationError, match="exactly one"):
        Settings(database_url="inline", database_url_file=str(secret))


def test_settings_load_secret_file_paths_from_prefixed_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = tmp_path / "redis-url"
    secret.write_text("redis://mounted.example/0\n")
    monkeypatch.delenv("EC_REDIS_URL", raising=False)
    monkeypatch.setenv("EC_REDIS_URL_FILE", str(secret))

    settings = Settings(_env_file=None)

    assert settings.redis_url == "redis://mounted.example/0"
