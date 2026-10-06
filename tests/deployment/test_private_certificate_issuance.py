"""Local issuance keeps signing keys separate and produces usable peer identities."""

from __future__ import annotations

import importlib.util
import stat
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "private_tls", ROOT / "scripts/development/private_tls.py"
)
assert spec and spec.loader
issuance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(issuance)


def test_issued_bundle_has_separate_authorities_and_verified_leaf_usages(tmp_path):
    output = tmp_path / "generation"
    inventory = issuance.generate(output)
    assert len(inventory) == 8
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert len(list((output / "authorities").rglob("ca.key"))) == 3
    for name, record in inventory.items():
        directory = output / "leaves" / name
        assert {path.name for path in directory.iterdir()} == {"ca.crt", "tls.crt", "tls.key"}
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in directory.iterdir())
        cert = x509.load_pem_x509_certificate((directory / "tls.crt").read_bytes())
        ca = x509.load_pem_x509_certificate((directory / "ca.crt").read_bytes())
        ca.public_key().verify(
            cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm)
        )
        assert not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
        usage = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        if name in issuance.CLIENTS:
            assert list(usage) == [ExtendedKeyUsageOID.CLIENT_AUTH]
        else:
            assert ExtendedKeyUsageOID.SERVER_AUTH in usage
            hosts = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            assert hosts.get_values_for_type(x509.DNSName) == record["dns_names"]
        if name == "ec-dev-temporal-tls-v1":
            assert ExtendedKeyUsageOID.CLIENT_AUTH in usage
            assert "ec-dev-temporal-internode" in record["dns_names"]


def test_issuance_never_replaces_an_existing_generation(tmp_path):
    output = tmp_path / "generation"
    issuance.generate(output)
    saved = (output / "authorities" / "temporal" / "ca.key").read_bytes()
    with pytest.raises(FileExistsError):
        issuance.generate(output)
    assert (output / "authorities" / "temporal" / "ca.key").read_bytes() == saved


def test_next_generation_has_new_secret_names_and_preserves_client_identity(tmp_path):
    output = tmp_path / "generation-2"
    inventory = issuance.generate(output, generation=2)
    assert all(name.endswith("-v2") for name in inventory)
    cert = x509.load_pem_x509_certificate(
        (output / "leaves/ec-dev-temporal-api-tls-v2/tls.crt").read_bytes()
    )
    assert cert.subject.rfc4514_string() == "CN=api"


def test_invalid_generation_leaves_no_output(tmp_path):
    output = tmp_path / "invalid"
    with pytest.raises(ValueError):
        issuance.generate(output, generation=0)
    assert not output.exists()


@pytest.mark.parametrize("days", [0, 91])
def test_unsupported_lifetimes_leave_no_output(tmp_path, days):
    output = tmp_path / "generation"
    with pytest.raises(ValueError):
        issuance.generate(output, leaf_days=days)
    assert not output.exists()
