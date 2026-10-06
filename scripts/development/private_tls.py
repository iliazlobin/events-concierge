"""Issue local private-store certificates; no cloud access or secret publication."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NAMESPACE = "events-concierge-dev"
MAX_LEAF_DAYS = 90
SERVICES = {
    "ec-dev-application-postgres-tls-v1": ("postgres", ["ec-dev-application-postgres"], False),
    "ec-dev-temporal-postgres-tls-v1": ("postgres", ["ec-dev-temporal-postgres"], False),
    "ec-dev-redis-tls-v1": ("redis", ["ec-dev-redis"], False),
    "ec-dev-temporal-tls-v1": ("temporal", ["ec-dev-temporal-frontend"], True),
}
CLIENTS = {
    "ec-dev-temporal-api-tls-v1": "api",
    "ec-dev-temporal-erasure-tls-v1": "account-erasure",
    "ec-dev-temporal-executor-tls-v1": "ingestion-executor",
    "ec-dev-temporal-catalog-tls-v1": "temporal-catalog",
}


def _write(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        path.chmod(0o600)
        stream.write(data)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _key_bytes(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


def generate(
    output: Path, *, leaf_days: int = MAX_LEAF_DAYS, generation: int = 1
) -> dict[str, object]:
    """Create a new 0700 directory; never overwrite an existing certificate generation."""
    if not 1 <= leaf_days <= MAX_LEAF_DAYS:
        raise ValueError("leaf lifetime must be from 1 through 90 days")
    if generation < 1:
        raise ValueError("generation must be a positive integer")
    output.mkdir(mode=0o700)
    now = datetime.now(UTC)
    authorities = {}
    ca_directory = output / "authorities"
    ca_directory.mkdir(mode=0o700)
    for kind in ("postgres", "redis", "temporal"):
        key = ec.generate_private_key(ec.SECP256R1())
        name = _name(f"ec-private-{kind}-ca")
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=True,
                    crl_sign=True,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
            .sign(key, hashes.SHA256())
        )
        directory = ca_directory / kind
        directory.mkdir(mode=0o700)
        _write(directory / "ca.crt", cert.public_bytes(serialization.Encoding.PEM))
        _write(directory / "ca.key", _key_bytes(key))
        authorities[kind] = (cert, key)
    leaves = output / "leaves"
    leaves.mkdir(mode=0o700)
    inventory: dict[str, object] = {}
    profiles = {**SERVICES, **{name: ("temporal", [], True) for name in CLIENTS}}
    for profile, (kind, services, client_usage) in profiles.items():
        secret = profile.removesuffix("v1") + f"v{generation}"
        ca, ca_key = authorities[kind]
        key = ec.generate_private_key(ec.SECP256R1())
        builder = (
            x509.CertificateBuilder()
            .subject_name(_name(CLIENTS.get(profile, secret)))
            .issuer_name(ca.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=leaf_days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True,
                    content_commitment=False,
                    key_encipherment=False,
                    data_encipherment=False,
                    key_agreement=False,
                    key_cert_sign=False,
                    crl_sign=False,
                    encipher_only=False,
                    decipher_only=False,
                ),
                critical=True,
            )
        )
        usages = [ExtendedKeyUsageOID.SERVER_AUTH] if services else []
        if client_usage:
            usages.append(ExtendedKeyUsageOID.CLIENT_AUTH)
        builder = builder.add_extension(x509.ExtendedKeyUsage(usages), critical=True)
        hosts = [f"{service}.{NAMESPACE}.svc.cluster.local" for service in services]
        if profile == "ec-dev-temporal-tls-v1":
            hosts.append("ec-dev-temporal-internode")
        if hosts:
            builder = builder.add_extension(
                x509.SubjectAlternativeName([x509.DNSName(host) for host in hosts]), critical=False
            )
        cert = builder.sign(ca_key, hashes.SHA256())
        directory = leaves / secret
        directory.mkdir(mode=0o700)
        _write(directory / "ca.crt", ca.public_bytes(serialization.Encoding.PEM))
        _write(directory / "tls.crt", cert.public_bytes(serialization.Encoding.PEM))
        _write(directory / "tls.key", _key_bytes(key))
        inventory[secret] = {
            "issuer": kind,
            "dns_names": hosts,
            "expires_at": cert.not_valid_after_utc.isoformat(),
            "sha256": cert.fingerprint(hashes.SHA256()).hex(),
        }
    _write(output / "inventory.json", (json.dumps(inventory, indent=2) + "\n").encode())
    return inventory


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New protected local directory")
    parser.add_argument("--generation", type=int, default=1, help="Reviewed positive Secret suffix")
    args = parser.parse_args()
    try:
        inventory = generate(args.output, generation=args.generation)
    except (OSError, ValueError):
        parser.exit(1, "Certificate issuance failed; inspect the protected output directory.\n")
    print(
        json.dumps(
            {
                "issued": len(inventory),
                "namespace": NAMESPACE,
                "generation": args.generation,
                "published": False,
                "requires_expiry_and_rotation_review": True,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
