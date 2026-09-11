"""Composition selects explicit durable hosted media without constructing credentials early."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from events_concierge.adapters.gcs.media_store import GcsMediaStore
from events_concierge.adapters.local_media import LocalFilesystemMediaStore
from events_concierge.adapters.postgres.profile_media import PostgresProfileMediaMutationGuard
from events_concierge.composition import _build_media_store
from events_concierge.config import Settings
from events_concierge.deployment.gcp_runtime import build_gcs_media_store


def test_default_local_media_remains_filesystem(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, media_local_root=str(tmp_path))
    assert isinstance(_build_media_store(settings), LocalFilesystemMediaStore)


def test_gcs_media_is_lazy_until_use_and_does_not_read_or_write_local_files(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def no_credentials(**kwargs: object) -> object:
        raise AssertionError("composition must not load credentials or perform provider I/O")

    monkeypatch.setattr(
        "events_concierge.deployment.gcp_runtime.import_module",
        lambda _: SimpleNamespace(Client=no_credentials),
    )
    root = tmp_path / "must-not-exist"
    settings = Settings(
        _env_file=None,
        media_backend="gcs",
        gcs_media_bucket="private-media",
        gcs_claim_check_bucket="private-claims",
        media_local_root=str(root),
    )
    assert isinstance(_build_media_store(settings), GcsMediaStore)
    assert not root.exists()


@pytest.mark.parametrize(
    "overrides",
    [
        {"gcs_media_bucket": None},
        {"gcs_media_bucket": " "},
        {"gcs_media_bucket": "private-claims"},
        {"gcs_media_prefix": "events-concierge/claim-check/v1"},
    ],
)
def test_media_configuration_requires_explicit_separate_lifetime(overrides: dict) -> None:
    values = {
        "_env_file": None,
        "media_backend": "gcs",
        "gcs_media_bucket": "private-media",
        "gcs_claim_check_bucket": "private-claims",
    }
    values.update(overrides)
    with pytest.raises(ValueError, match="GCS media"):
        Settings(**values)


@pytest.mark.parametrize(
    "prefix",
    [
        "events-concierge/media/../claim-check",
        "events-concierge/media/",
        "events-concierge/media//v1",
        "events-concierge/media/v1?public=true",
    ],
)
def test_malformed_media_prefix_fails_before_client_initialization(prefix: str) -> None:
    settings = Settings(
        _env_file=None,
        media_backend="gcs",
        gcs_media_bucket="private-media",
        gcs_media_prefix=prefix,
    )
    with pytest.raises(ValueError, match="safe slash-delimited"):
        build_gcs_media_store(settings)


@pytest.mark.parametrize(
    "values",
    [
        {"pool_capacity": 1},
        {"lock_timeout_seconds": 0.00001},
        {"lock_timeout_seconds": float("inf")},
        {"mutation_timeout_seconds": float("nan")},
    ],
)
def test_media_guard_rejects_unbounded_or_starving_configuration(values: dict) -> None:
    settings = {"pool_capacity": 2, "lock_timeout_seconds": 0.1, "mutation_timeout_seconds": 1.0}
    settings.update(values)
    with pytest.raises(ValueError):
        PostgresProfileMediaMutationGuard(**settings)
