"""Secure shared Temporal client configuration tests."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import ValidationError
from temporalio.client import TLSConfig

from events_concierge.adapters.mock.object_store import MockFilesystemObjectStore
from events_concierge.config import Settings
from events_concierge.workflows.temporal_client import (
    connect_temporal,
    validate_temporal_settings,
)


class _FixtureClient:
    pass


def mtls_settings(tmp_path: Path, *, invalid: str | None = None) -> dict[str, Any]:
    """Issue temporary test-only certificates; no committed private keys or external CA needed."""
    now = datetime.now(UTC)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Temporal test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=invalid != "not_ca", path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    client_key = ec.generate_private_key(ec.SECP256R1())
    client = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Temporal test client")]))
        .issuer_name(name)
        .public_key(client_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(
            now + timedelta(hours=1) if invalid == "not_yet_valid" else now - timedelta(days=2)
        )
        .not_valid_after(
            now - timedelta(days=1) if invalid == "expired" else now + timedelta(days=1)
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .add_extension(
            x509.ExtendedKeyUsage(
                [
                    ExtendedKeyUsageOID.SERVER_AUTH
                    if invalid == "server_only"
                    else ExtendedKeyUsageOID.CLIENT_AUTH
                ]
            ),
            False,
        )
        .sign(key, hashes.SHA256())
    )
    if invalid == "mismatched_key":
        client_key = ec.generate_private_key(ec.SECP256R1())
    material = {
        "server_ca": ca.public_bytes(serialization.Encoding.PEM),
        "client_cert": client.public_bytes(serialization.Encoding.PEM),
        "client_key": client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(b"test-only")
            if invalid == "encrypted_key"
            else serialization.NoEncryption(),
        ),
    }
    values: dict[str, Any] = {
        "temporal_target": "temporal.internal.example:7233",
        "temporal_tls_enabled": True,
        "temporal_tls_domain": "temporal.internal.example",
        "temporal_api_key": None,
    }
    for suffix, pem in material.items():
        path = tmp_path / f"{suffix}.pem"
        path.write_bytes(pem)
        path.chmod(0o600)
        values[f"temporal_tls_{suffix}_file"] = str(path)
    return values


async def test_self_hosted_temporal_passes_verified_mtls_bytes_to_sdk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    async def connect(target: str, **kwargs: Any) -> _FixtureClient:
        captured["target"] = target
        captured.update(kwargs)
        return _FixtureClient()

    monkeypatch.setattr("events_concierge.workflows.temporal_client.Client.connect", connect)
    values = mtls_settings(tmp_path)
    settings = Settings(**values)
    await connect_temporal(settings, MockFilesystemObjectStore(tmp_path), lazy=True)

    assert captured["api_key"] is None
    tls = captured["tls"]
    assert isinstance(tls, TLSConfig)
    assert tls.domain == "temporal.internal.example"
    for suffix, actual in (
        ("server_ca", tls.server_root_ca_cert),
        ("client_cert", tls.client_cert),
        ("client_key", tls.client_private_key),
    ):
        expected = await asyncio.to_thread(Path(values[f"temporal_tls_{suffix}_file"]).read_bytes)
        assert actual == expected.rstrip(b"\n")
    assert "client_key.pem" not in repr(settings)
    assert "temporal_tls_client_key_file" not in settings.model_dump()


@pytest.mark.parametrize(
    "change",
    [
        {"temporal_tls_enabled": False},
        {"temporal_tls_domain": None},
        {"temporal_tls_domain": "https://temporal.internal.example"},
        {"temporal_tls_domain": "*.internal.example"},
        {"temporal_tls_server_ca_file": None},
        {"temporal_tls_client_cert_file": None},
        {"temporal_tls_client_key_file": None},
        {"temporal_tls_client_key_file": ""},
        {"temporal_api_key": "fixture-key"},
        {"temporal_api_key": " "},
    ],
)
async def test_mtls_rejects_incomplete_ambiguous_or_insecure_configuration_before_connect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, change: dict[str, Any]
) -> None:
    def unexpected_connect(*args: Any, **kwargs: Any) -> None:
        pytest.fail("invalid mTLS must fail before contacting Temporal")

    monkeypatch.setattr(
        "events_concierge.workflows.temporal_client.Client.connect", unexpected_connect
    )
    values = mtls_settings(tmp_path)
    values.update(change)
    with pytest.raises(ValueError):
        await connect_temporal(Settings(**values), MockFilesystemObjectStore(tmp_path))


@pytest.mark.parametrize(
    "invalid",
    ["not_ca", "not_yet_valid", "expired", "server_only", "mismatched_key", "encrypted_key"],
)
def test_mtls_rejects_invalid_certificate_material(tmp_path: Path, invalid: str) -> None:
    with pytest.raises(ValueError, match="valid CA and matching client credentials") as failure:
        validate_temporal_settings(Settings(**mtls_settings(tmp_path, invalid=invalid)))
    assert str(tmp_path) not in str(failure.value)
    assert failure.value.__suppress_context__ is True


@pytest.mark.parametrize("invalid", ["missing", "directory", "relative", "oversized", "malformed"])
def test_mtls_file_validation_is_bounded_and_sanitized(tmp_path: Path, invalid: str) -> None:
    values = mtls_settings(tmp_path)
    path = Path(values["temporal_tls_client_key_file"])
    if invalid == "missing":
        path.unlink()
    elif invalid == "directory":
        values["temporal_tls_client_key_file"] = str(tmp_path)
    elif invalid == "relative":
        values["temporal_tls_client_key_file"] = "secret-private-key.pem"
    else:
        path.write_text("sensitive-test-marker" * (4000 if invalid == "oversized" else 1))
    with pytest.raises(ValueError, match="valid CA and matching client credentials") as failure:
        validate_temporal_settings(Settings(**values))
    assert str(tmp_path) not in str(failure.value)
    assert "sensitive-test-marker" not in str(failure.value)


async def test_local_temporal_connection_preserves_plaintext_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    async def connect(target: str, **kwargs: Any) -> _FixtureClient:
        captured["target"] = target
        captured.update(kwargs)
        return _FixtureClient()

    monkeypatch.setattr("events_concierge.workflows.temporal_client.Client.connect", connect)
    settings = Settings(
        temporal_target="localhost:7234",
        temporal_namespace="default",
        temporal_tls_enabled=False,
        temporal_api_key=None,
    )

    client = await connect_temporal(settings, MockFilesystemObjectStore(tmp_path))

    assert isinstance(client, _FixtureClient)
    assert captured["target"] == "localhost:7234"
    assert captured["namespace"] == "default"
    assert captured["api_key"] is None
    assert captured["tls"] is None
    assert captured["lazy"] is False
    assert captured["data_converter"] is not None


async def test_temporal_cloud_connection_uses_tls_domain_and_trimmed_api_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured: dict[str, Any] = {}

    async def connect(target: str, **kwargs: Any) -> _FixtureClient:
        captured["target"] = target
        captured.update(kwargs)
        return _FixtureClient()

    monkeypatch.setattr("events_concierge.workflows.temporal_client.Client.connect", connect)
    settings = Settings(
        temporal_target="example.tmprl.cloud:7233",
        temporal_namespace="production.example",
        temporal_tls_enabled=True,
        temporal_tls_domain="example.tmprl.cloud",
        temporal_api_key="  fixture-api-key  ",
    )

    await connect_temporal(settings, MockFilesystemObjectStore(tmp_path), lazy=True)

    assert captured["api_key"] == "fixture-api-key"
    assert captured["lazy"] is True
    tls = captured["tls"]
    assert isinstance(tls, TLSConfig)
    assert tls.domain == "example.tmprl.cloud"


async def test_eager_temporal_connection_has_the_configured_outer_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cancelled = False

    async def blocked_connect(*args: object, **kwargs: object) -> _FixtureClient:
        del args, kwargs
        nonlocal cancelled
        try:
            await asyncio.Event().wait()
        finally:
            cancelled = True
        return _FixtureClient()

    monkeypatch.setattr(
        "events_concierge.workflows.temporal_client.Client.connect",
        blocked_connect,
    )

    with pytest.raises(TimeoutError):
        await connect_temporal(
            Settings(temporal_rpc_timeout_seconds=0.1),
            MockFilesystemObjectStore(tmp_path),
        )

    assert cancelled is True


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (
            Settings(temporal_api_key="fixture", temporal_tls_enabled=False),
            "requires TLS",
        ),
        (
            Settings(temporal_api_key=" \t", temporal_tls_enabled=True),
            "must not be empty",
        ),
        (
            Settings(
                temporal_tls_domain="example.tmprl.cloud",
                temporal_tls_enabled=False,
            ),
            "domain requires TLS",
        ),
        (
            Settings(temporal_tls_domain=" \t", temporal_tls_enabled=True),
            "domain must not be empty",
        ),
    ],
)
async def test_temporal_connection_rejects_insecure_or_blank_credentials(
    tmp_path: Path,
    settings: Settings,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        await connect_temporal(settings, MockFilesystemObjectStore(tmp_path))


def test_temporal_validation_does_not_require_engine_reachability() -> None:
    settings = Settings(
        temporal_target="unreachable.example.test:7233",
        temporal_namespace="production.example",
        temporal_tls_enabled=True,
        temporal_tls_domain="unreachable.example.test",
        temporal_api_key="fixture-api-key",
    )

    validate_temporal_settings(settings)


def test_temporal_rpc_timeout_has_a_short_validated_bound() -> None:
    """A deployment cannot disable the outer Temporal call deadline with zero or an extreme value."""
    assert Settings().temporal_rpc_timeout_seconds == 5.0
    assert Settings(temporal_rpc_timeout_seconds=0.1).temporal_rpc_timeout_seconds == 0.1
    assert Settings(temporal_rpc_timeout_seconds=60.0).temporal_rpc_timeout_seconds == 60.0

    for invalid in (0.0, 0.099, -1.0, 1e-9, 60.01, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            Settings(temporal_rpc_timeout_seconds=invalid)
