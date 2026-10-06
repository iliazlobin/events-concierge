"""Real Temporal SDK RPCs over gRPC mTLS; no Temporal persistence or deployed acceptance."""

from __future__ import annotations

import importlib.util
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import grpc
import pytest
from temporalio.api.workflowservice.v1 import GetSystemInfoRequest, GetSystemInfoResponse

from events_concierge.config import Settings
from events_concierge.workflows.temporal_client import connect_temporal

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "mtls_issuance", ROOT / "scripts/development/private_tls.py"
)
assert spec and spec.loader
issuance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(issuance)
HOST = "ec-dev-temporal-frontend.events-concierge-dev.svc.cluster.local"


@pytest.fixture(scope="module")
def mtls_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("sdk-mtls")
    trusted, untrusted = root / "trusted", root / "untrusted"
    issuance.generate(trusted)
    issuance.generate(untrusted)
    server_material = trusted / "leaves" / "ec-dev-temporal-tls-v1"
    identities = []

    def system_info(request, context):
        identities.append(context.auth_context().get("x509_common_name"))
        return GetSystemInfoResponse(server_version="disposable-transport-fixture")

    handler = grpc.method_handlers_generic_handler(
        "temporal.api.workflowservice.v1.WorkflowService",
        {
            "GetSystemInfo": grpc.unary_unary_rpc_method_handler(
                system_info,
                request_deserializer=GetSystemInfoRequest.FromString,
                response_serializer=GetSystemInfoResponse.SerializeToString,
            )
        },
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        server = grpc.server(pool)
        server.add_generic_rpc_handlers((handler,))
        credentials = grpc.ssl_server_credentials(
            (
                (
                    (server_material / "tls.key").read_bytes(),
                    (server_material / "tls.crt").read_bytes(),
                ),
            ),
            root_certificates=(server_material / "ca.crt").read_bytes(),
            require_client_auth=True,
        )
        port = server.add_secure_port("127.0.0.1:0", credentials)
        assert port
        server.start()
        try:
            yield trusted, untrusted, port, identities
        finally:
            server.stop(0).wait(timeout=5)


def settings(trusted: Path, port: int, **overrides) -> Settings:
    client = trusted / "leaves" / "ec-dev-temporal-api-tls-v1"
    config = {
        "env": "staging",
        "mock_cloud": False,
        "temporal_target": f"127.0.0.1:{port}",
        "temporal_namespace": "events-development",
        "temporal_tls_enabled": True,
        "temporal_tls_domain": HOST,
        "temporal_tls_server_ca_file": str(client / "ca.crt"),
        "temporal_tls_client_cert_file": str(client / "tls.crt"),
        "temporal_tls_client_key_file": str(client / "tls.key"),
        "temporal_rpc_timeout_seconds": 3,
        "temporal_task_queue": "catalog",
        "temporal_catalog_task_queue": "catalog",
        "temporal_transactional_task_queue": "transactional",
        "temporal_worker_role": "catalog",
        "temporal_worker_versioning_enabled": True,
        "release_revision": "1" * 40,
    }
    config.update(overrides)
    return Settings(_env_file=None, **config)


async def test_sdk_authenticates_client_and_verifies_server_before_rpc(mtls_server):
    trusted, _, port, identities = mtls_server
    client = await connect_temporal(settings(trusted, port), Mock())
    response = await client.workflow_service.get_system_info(
        GetSystemInfoRequest(), timeout=timedelta(seconds=3)
    )
    assert response.server_version == "disposable-transport-fixture"
    assert identities and identities[-1] == [b"api"]


@pytest.mark.parametrize(
    "problem", ["wrong_ca", "wrong_hostname", "untrusted_client", "missing_client"]
)
async def test_failed_peer_verification_never_reaches_an_application_rpc(mtls_server, problem):
    trusted, untrusted, port, identities = mtls_server
    bad_client = untrusted / "leaves" / "ec-dev-temporal-api-tls-v1"
    overrides = {}
    if problem == "wrong_ca":
        overrides["temporal_tls_server_ca_file"] = str(bad_client / "ca.crt")
    elif problem == "wrong_hostname":
        overrides["temporal_tls_domain"] = "unexpected.example.com"
    elif problem == "untrusted_client":
        overrides.update(
            temporal_tls_client_cert_file=str(bad_client / "tls.crt"),
            temporal_tls_client_key_file=str(bad_client / "tls.key"),
        )
    else:
        overrides.update(temporal_tls_client_cert_file=None, temporal_tls_client_key_file=None)
    received = len(identities)
    with pytest.raises((ValueError, RuntimeError, TimeoutError)):
        await connect_temporal(settings(trusted, port, **overrides), Mock())
    assert len(identities) == received
