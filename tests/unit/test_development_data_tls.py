"""Render the opt-in store TLS contract; optionally rehearse it with disposable containers.

Run the real images with EC_DATA_TLS_DOCKER=1 and a configured Docker host. No exposed ports,
live credentials, existing volumes or cluster access are used. Helm uses EC_HELM_BINARY or PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy/helm/events-concierge-dev-data"
NAMESPACE = "events-concierge-dev"
STORES = ("application", "temporal", "redis")


@pytest.fixture(scope="module")
def helm_binary():
    binary = os.environ.get("EC_HELM_BINARY") or shutil.which("helm")
    if not binary:
        pytest.skip("Set EC_HELM_BINARY or install Helm to run chart contracts")
    return binary


def tls_values():
    return {
        "postgres": {
            "tls": {
                "enabled": True,
                "applicationSecretName": "application-tls-v1",
                "temporalSecretName": "temporal-tls-v1",
            }
        },
        "redis": {"tls": {"enabled": True, "secretName": "redis-tls-v1"}},
    }


def render(binary, tmp_path, values):
    overrides = tmp_path / "values.json"
    overrides.write_text(json.dumps(values))
    return subprocess.run(
        [
            binary,
            "template",
            "fixture",
            str(CHART),
            "--namespace",
            NAMESPACE,
            "--values",
            str(overrides),
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


@pytest.fixture(scope="module", params=[False, True], ids=["plaintext", "tls"])
def rendered(request, helm_binary, tmp_path_factory):
    result = render(
        helm_binary, tmp_path_factory.mktemp("store-tls"), tls_values() if request.param else {}
    )
    assert result.returncode == 0, result.stderr
    resources = {
        (item["kind"], item["metadata"]["name"]): item
        for item in yaml.safe_load_all(result.stdout)
        if item
    }
    return request.param, resources


def workload(resources, store):
    kind, name = (
        ("Deployment", "ec-dev-redis")
        if store == "redis"
        else ("StatefulSet", f"ec-dev-{store}-postgres")
    )
    return resources[kind, name]["spec"]["template"]["spec"]


@pytest.mark.parametrize("store", STORES)
def test_tls_preserves_storage_image_and_probe_timing(rendered, store):
    enabled, resources = rendered
    pod = workload(resources, store)
    container = pod["containers"][0]
    defaults = yaml.safe_load((CHART / "values.yaml").read_text())
    expected_image = (
        defaults["redis"]["image"] if store == "redis" else defaults["postgres"][f"{store}Image"]
    )
    assert container["image"] == expected_image
    data = next(volume for volume in pod["volumes"] if volume["name"] == "data")
    assert data["persistentVolumeClaim"]["claimName"] == f"ec-dev-{store}"
    assert container["startupProbe"]["periodSeconds"] == 2
    assert container["startupProbe"]["failureThreshold"] == (150 if store == "redis" else 90)
    assert container["livenessProbe"]["periodSeconds"] == 20
    assert all(
        container[key]["timeoutSeconds"] == 5
        for key in ("startupProbe", "readinessProbe", "livenessProbe")
    )
    assert not pod["automountServiceAccountToken"]
    if not enabled:
        assert "initContainers" not in pod
        assert not any(volume["name"].startswith("tls") for volume in pod["volumes"])
        expected = (
            ["redis-cli", "ping"]
            if store == "redis"
            else ["sh", "-c", 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"']
        )
        assert container["readinessProbe"]["exec"]["command"] == expected


@pytest.mark.parametrize("store", STORES)
def test_secret_keys_are_copied_to_private_memory_with_image_native_ownership(rendered, store):
    enabled, resources = rendered
    if not enabled:
        return
    pod = workload(resources, store)
    init = pod["initContainers"][0]
    assert init["image"] == pod["containers"][0]["image"]
    assert init["securityContext"]["capabilities"] == {"drop": ["ALL"], "add": ["CHOWN"]}
    assert init["securityContext"]["readOnlyRootFilesystem"]
    user = "redis" if store == "redis" else "postgres"
    assert f"chown {user}:{user} /tls/*" in init["args"][0]
    assert "chmod 0600 /tls/*" in init["args"][0]
    volumes = {item["name"]: item for item in pod["volumes"]}
    assert volumes["tls"]["emptyDir"] == {"medium": "Memory", "sizeLimit": "1Mi"}
    secret = volumes["tls-source"]["secret"]
    assert secret["secretName"] == f"{store}-tls-v1"
    assert secret["defaultMode"] == 0o400
    assert {item["key"] for item in secret["items"]} == {"ca.crt", "tls.crt", "tls.key"}
    assert next(m for m in pod["containers"][0]["volumeMounts"] if m["name"] == "tls")["readOnly"]
    assert not any(kind == "Secret" for kind, _ in resources)


@pytest.mark.parametrize("store", ["application", "temporal"])
def test_postgres_rejects_plaintext_and_probes_verified_tls_without_service_resolution(
    rendered, store
):
    enabled, resources = rendered
    if not enabled:
        return
    hba = resources["ConfigMap", f"ec-dev-{store}-postgres-tls"]["data"]["pg_hba.conf"]
    lines = [line for line in hba.splitlines() if line and not line.startswith("#")]
    assert lines == [
        "local all all trust",
        "hostnossl all all 0.0.0.0/0 reject",
        "hostnossl all all ::/0 reject",
        "hostssl all all 0.0.0.0/0 scram-sha-256",
        "hostssl all all ::/0 scram-sha-256",
    ]
    container = workload(resources, store)["containers"][0]
    assert "ssl=on" in container["args"]
    assert "hba_file=/tls-config/pg_hba.conf" in container["args"]
    assert "password_encryption=scram-sha-256" in container["args"]
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        command = container[probe]["exec"]["command"][2]
        assert "PGSSLMODE=verify-full" in command and "PGHOSTADDR=127.0.0.1" in command
        assert 'PGHOST="$POSTGRES_TLS_SERVER_NAME"' in command
        assert "PGSSLROOTCERT=/tls/ca.crt" in command and "psql --no-password" in command
    # Do not redirect ordinary Unix-socket administration or image bootstrap to TCP.
    assert not any(item["name"].startswith("PGHOST") for item in container["env"])


def test_redis_disables_plaintext_and_probes_ca_and_password(rendered):
    enabled, resources = rendered
    if not enabled:
        return
    container = workload(resources, "redis")["containers"][0]
    args = container["args"][0]
    assert "--port 0" in args and "--tls-port 6379" in args
    assert '--requirepass "$REDIS_PASSWORD"' in args
    assert "--tls-auth-clients no" in args
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        command = container[probe]["exec"]["command"][2]
        assert "--tls --cacert /tls/ca.crt" in command and "--raw ping" in command
        assert "--insecure" not in command and "= PONG" in command
    assert next(item for item in container["env"] if item["name"] == "REDISCLI_AUTH")["valueFrom"][
        "secretKeyRef"
    ] == {"name": "ec-dev-redis", "key": "password"}


@pytest.mark.parametrize("store", STORES)
def test_enabled_tls_requires_explicit_secret_references(helm_binary, tmp_path, store):
    values = tls_values()
    target = values["redis"]["tls"] if store == "redis" else values["postgres"]["tls"]
    target["secretName" if store == "redis" else f"{store}SecretName"] = ""
    result = render(helm_binary, tmp_path, values)
    assert result.returncode != 0
    assert "is required when TLS is enabled" in result.stderr


def _docker(*args, timeout=90):
    return subprocess.run(
        ["docker", *args], text=True, capture_output=True, check=False, timeout=timeout
    )


def _fixture_certificates(directory, hostname):
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, f"Disposable TLS test CA {uuid.uuid4().hex[:8]}")]
    )
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname.split(".")[0])]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    directory.mkdir()
    (directory / "tls.crt").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (directory / "ca.crt").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    key_file = directory / "tls.key"
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_file.chmod(0o600)


@pytest.mark.skipif(
    os.environ.get("EC_DATA_TLS_DOCKER") != "1", reason="Opt-in disposable Docker TLS rehearsal"
)
@pytest.mark.parametrize("store", STORES)
def test_real_store_accepts_tls_and_rejects_plaintext_and_wrong_ca(helm_binary, tmp_path, store):  # noqa: PLR0915 - keep disposable container cleanup beside its lifecycle
    """Exercise the rendered commands and pinned images, never a running deployment."""
    result = render(helm_binary, tmp_path, tls_values())
    assert result.returncode == 0, result.stderr
    resources = {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in yaml.safe_load_all(result.stdout)
        if doc
    }
    pod = workload(resources, store)
    container, init = pod["containers"][0], pod["initContainers"][0]
    env = {
        item["name"]: item.get("value", "synthetic-tls-probe-password") for item in container["env"]
    }
    hostname = env["REDIS_TLS_SERVER_NAME" if store == "redis" else "POSTGRES_TLS_SERVER_NAME"]
    source = tmp_path / "source"
    wrong_ca = tmp_path / "wrong"
    _fixture_certificates(source, hostname)
    _fixture_certificates(wrong_ca, hostname)
    name = f"ec-tls-test-{uuid.uuid4().hex[:12]}"
    volume, init_name = f"{name}-tls", f"{name}-init"
    try:
        pull = _docker("image", "inspect", container["image"])
        if pull.returncode:
            pull = _docker("pull", container["image"], timeout=300)
            assert pull.returncode == 0, pull.stderr
        created = _docker("volume", "create", volume)
        assert created.returncode == 0, created.stderr
        staged = _docker(
            "create",
            "--name",
            init_name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=volume,source={volume},target=/tls",
            "--entrypoint",
            "sh",
            init["image"],
            "-ec",
            # Model Kubernetes Secret ownership after Docker copies host fixture files.
            "chown -R 0:0 /tls-source\n" + init["args"][0],
        )
        assert staged.returncode == 0, staged.stderr
        copied = _docker("cp", str(source), f"{init_name}:/tls-source")
        assert copied.returncode == 0, copied.stderr
        started = _docker("start", "-a", init_name)
        assert started.returncode == 0, started.stderr
        command = [
            "create",
            "--name",
            name,
            "--network",
            "none",
            "--memory",
            "768m",
            "--cpus",
            "1",
            "--mount",
            f"type=volume,source={volume},target=/tls,readonly",
            "--tmpfs",
            "/data" if store == "redis" else "/var/lib/postgresql/data",
        ]
        for key, value in env.items():
            command.extend(["--env", f"{key}={value}"])
        if store == "redis":
            command.extend(["--entrypoint", "sh", container["image"], "-ec", container["args"][0]])
        else:
            command.extend([container["image"], *container["args"]])
        created = _docker(*command)
        assert created.returncode == 0, created.stderr
        copied = _docker("cp", str(wrong_ca / "ca.crt"), f"{name}:/wrong-ca.crt")
        assert copied.returncode == 0, copied.stderr
        if store != "redis":
            config = tmp_path / "config"
            config.mkdir()
            (config / "pg_hba.conf").write_text(
                resources["ConfigMap", f"ec-dev-{store}-postgres-tls"]["data"]["pg_hba.conf"]
            )
            copied = _docker("cp", str(config), f"{name}:/tls-config")
            assert copied.returncode == 0, copied.stderr
        started = _docker("start", name)
        assert started.returncode == 0, started.stderr
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            probe = _docker("exec", name, *container["readinessProbe"]["exec"]["command"])
            if probe.returncode == 0:
                break
            time.sleep(0.25)
        else:
            pytest.fail(f"TLS probe did not pass: {probe.stderr}\n{_docker('logs', name).stdout}")
        if store == "redis":
            wrong = _docker("exec", name, "redis-cli", "--tls", "--cacert", "/wrong-ca.crt", "ping")
            plaintext = _docker("exec", name, "timeout", "3", "redis-cli", "ping")
            wrong_password = _docker(
                "exec",
                "--env",
                "REDISCLI_AUTH=wrong-fixture-password",
                name,
                *container["readinessProbe"]["exec"]["command"],
            )
            assert wrong_password.returncode != 0
        else:
            client = [
                "env",
                "PGPASSWORD=synthetic-tls-probe-password",
                "PGHOSTADDR=127.0.0.1",
                f"PGHOST={hostname}",
                "PGCONNECT_TIMEOUT=3",
            ]
            query = [
                "psql",
                "--no-password",
                "-U",
                env["POSTGRES_USER"],
                "-d",
                env["POSTGRES_DB"],
                "-tAc",
                "SELECT 1",
            ]
            wrong = _docker(
                "exec",
                name,
                *client,
                "PGSSLMODE=verify-full",
                "PGSSLROOTCERT=/wrong-ca.crt",
                *query,
            )
            plaintext = _docker("exec", name, *client, "PGSSLMODE=disable", *query)
            wrong_host = _docker(
                "exec",
                name,
                *client,
                "PGHOST=wrong.example.invalid",
                "PGSSLMODE=verify-full",
                "PGSSLROOTCERT=/tls/ca.crt",
                *query,
            )
            assert wrong_host.returncode != 0 and "does not match host name" in wrong_host.stderr
            local = _docker("exec", name, *query)
            assert local.returncode == 0 and local.stdout.strip() == "1"
        assert wrong.returncode != 0 and "certificate verify failed" in wrong.stderr
        assert plaintext.returncode != 0
    finally:
        _docker("rm", "-f", "-v", name, init_name)
        _docker("volume", "rm", volume)


def _start_retained_store(name, data_volume, tls_volume, resources, store, env, tmp_path, tls):
    container = workload(resources, store)["containers"][0]
    data_path = "/data" if store == "redis" else "/var/lib/postgresql/data"
    command = [
        "create",
        "--name",
        name,
        "--network",
        "none",
        "--memory",
        "768m",
        "--cpus",
        "1",
        "--mount",
        f"type=volume,source={data_volume},target={data_path}",
    ]
    if tls:
        command.extend(["--mount", f"type=volume,source={tls_volume},target=/tls,readonly"])
    for key, value in env.items():
        command.extend(["--env", f"{key}={value}"])
    if store == "redis":
        command.extend(["--entrypoint", "sh", container["image"], "-ec", container["args"][0]])
    else:
        command.extend([container["image"], *container["args"]])
    result = _docker(*command)
    assert result.returncode == 0, result.stderr
    if tls and store != "redis":
        config = tmp_path / "retained-config"
        config.mkdir()
        (config / "pg_hba.conf").write_text(
            resources["ConfigMap", f"ec-dev-{store}-postgres-tls"]["data"]["pg_hba.conf"]
        )
        result = _docker("cp", str(config), f"{name}:/tls-config")
        assert result.returncode == 0, result.stderr
    result = _docker("start", name)
    assert result.returncode == 0, result.stderr
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        # TCP also waits for the final PostgreSQL process; initdb's temporary server is socket-only.
        probe = _retained_query(name, store, env, tls, "PING" if store == "redis" else "SELECT 1")
        if probe.returncode == 0:
            return
        time.sleep(0.25)
    pytest.fail(f"Retained-data store startup failed: {probe.stderr}")


def _retained_query(name, store, env, tls, query, *, app_role=False):
    if store == "redis":
        command = ["redis-cli", "--raw", "-h", "127.0.0.1"]
        if tls:
            command.extend(
                ["--tls", "--cacert", "/tls/ca.crt", "--sni", env["REDIS_TLS_SERVER_NAME"]]
            )
        command.extend(query.split())
    else:
        command = [
            "env",
            "PGPASSWORD=synthetic-tls-probe-password",
            "PGHOSTADDR=127.0.0.1",
            f"PGHOST={env['POSTGRES_TLS_SERVER_NAME']}",
            "PGCONNECT_TIMEOUT=3",
            f"PGSSLMODE={'verify-full' if tls else 'disable'}",
        ]
        if tls:
            command.append("PGSSLROOTCERT=/tls/ca.crt")
        command.extend(
            [
                "psql",
                "--no-password",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                "tls_probe" if app_role else env["POSTGRES_USER"],
                "-d",
                env["POSTGRES_DB"],
                "-tAc",
                query,
            ]
        )
    return _docker("exec", name, *command)


@pytest.mark.skipif(
    os.environ.get("EC_DATA_TLS_DOCKER") != "1", reason="Opt-in disposable Docker TLS rehearsal"
)
@pytest.mark.parametrize("store", STORES)
def test_retained_data_survives_plaintext_to_tls_and_rollback(helm_binary, tmp_path, store):  # noqa: PLR0915 - keep the three phases and cleanup visible
    """Keep one named data volume while replacing the plaintext, TLS and rollback containers."""
    rendered_modes = {}
    for mode, values in ((False, {}), (True, tls_values())):
        result = render(helm_binary, tmp_path, values)
        assert result.returncode == 0, result.stderr
        rendered_modes[mode] = {
            (doc["kind"], doc["metadata"]["name"]): doc
            for doc in yaml.safe_load_all(result.stdout)
            if doc
        }
    pod = workload(rendered_modes[True], store)
    container, init = pod["containers"][0], pod["initContainers"][0]
    env = {
        item["name"]: item.get("value", "synthetic-tls-probe-password") for item in container["env"]
    }
    hostname = env["REDIS_TLS_SERVER_NAME" if store == "redis" else "POSTGRES_TLS_SERVER_NAME"]
    source = tmp_path / "retained-source"
    _fixture_certificates(source, hostname)
    prefix = f"ec-tls-retained-{uuid.uuid4().hex[:12]}"
    data_volume, tls_volume, init_name = f"{prefix}-data", f"{prefix}-tls", f"{prefix}-init"
    names = [f"{prefix}-{phase}" for phase in ("plain", "secure", "rollback")]
    sentinel = uuid.uuid4().hex
    try:
        inspected = _docker("image", "inspect", container["image"])
        if inspected.returncode:
            pulled = _docker("pull", container["image"], timeout=300)
            assert pulled.returncode == 0, pulled.stderr
        for volume in (data_volume, tls_volume):
            result = _docker("volume", "create", volume)
            assert result.returncode == 0, result.stderr
        staged = _docker(
            "create",
            "--name",
            init_name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "CHOWN",
            "--security-opt",
            "no-new-privileges",
            "--mount",
            f"type=volume,source={tls_volume},target=/tls",
            "--entrypoint",
            "sh",
            init["image"],
            "-ec",
            "chown -R 0:0 /tls-source\n" + init["args"][0],
        )
        assert staged.returncode == 0, staged.stderr
        copied = _docker("cp", str(source), f"{init_name}:/tls-source")
        assert copied.returncode == 0, copied.stderr
        staged = _docker("start", "-a", init_name)
        assert staged.returncode == 0, staged.stderr

        for phase, (name, tls) in enumerate(zip(names, (False, True, False), strict=True)):
            _start_retained_store(
                name, data_volume, tls_volume, rendered_modes[tls], store, env, tmp_path, tls
            )
            if phase == 0:
                query = (
                    f"SET tls-retained-sentinel {sentinel}"
                    if store == "redis"
                    else "CREATE ROLE tls_probe LOGIN PASSWORD 'synthetic-tls-probe-password'; "
                    "CREATE TABLE tls_retained_sentinel (value text PRIMARY KEY); "
                    f"INSERT INTO tls_retained_sentinel VALUES ('{sentinel}'); "
                    "GRANT SELECT ON tls_retained_sentinel TO tls_probe"
                )
                result = _retained_query(name, store, env, tls, query)
                assert result.returncode == 0, result.stderr
            if store != "redis":
                transport = _retained_query(
                    name,
                    store,
                    env,
                    tls,
                    "SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()",
                    app_role=True,
                )
                assert transport.returncode == 0, transport.stderr
                assert transport.stdout.strip() == ("t" if tls else "f")
            read = (
                "GET tls-retained-sentinel"
                if store == "redis"
                else "SELECT value FROM tls_retained_sentinel"
            )
            result = _retained_query(name, store, env, tls, read, app_role=True)
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip() == sentinel
            if phase == 1:
                # The rollback must preserve writes committed while TLS was active, too.
                updated = f"{sentinel}-tls"
                query = (
                    f"SET tls-retained-sentinel {updated}"
                    if store == "redis"
                    else f"UPDATE tls_retained_sentinel SET value='{updated}'"
                )
                result = _retained_query(name, store, env, tls, query)
                assert result.returncode == 0, result.stderr
                sentinel = updated
            stopped = _docker("stop", "--time", "15", name, timeout=25)
            assert stopped.returncode == 0, stopped.stderr
            removed = _docker("rm", "-v", name)
            assert removed.returncode == 0, removed.stderr
    finally:
        _docker("rm", "-f", "-v", *names, init_name)
        removed = _docker("volume", "rm", data_volume, tls_volume)
        assert removed.returncode == 0, removed.stderr
