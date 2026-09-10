from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from events_concierge.adapters.postgres.operator_operations import (
    BackendOperationsSnapshot,
    BackendQueueSnapshot,
)
from events_concierge.api.admin import _local_ingestion_admin
from events_concierge.api.operator_operations import install_operator_operations_routes


class _Operations:
    async def overview(self) -> BackendOperationsSnapshot:
        return BackendOperationsSnapshot(
            generated_at=datetime(2026, 9, 7, tzinfo=UTC),
            schema_revisions=("0182",),
            queues=(BackendQueueSnapshot("request_start", 3, 1, 1, 1, None, None),),
        )


def _app(*, operator: bool = False) -> FastAPI:
    app = FastAPI()
    app.state.operator_boundary = operator
    app.state.settings = SimpleNamespace(
        env="staging",
        release_revision="test-release",
        image_digest=None,
    )
    app.state.operator_operations = _Operations()
    install_operator_operations_routes(app)
    app.dependency_overrides[_local_ingestion_admin] = object
    return app


def test_backend_snapshot_states_measurement_limits_and_safe_shape() -> None:
    with TestClient(_app()) as client:
        response = client.get("/admin/v1/operations/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["measurement_scope"] == "durable_queue_state"
    assert body["worker_liveness"] == "not_measured"
    assert body["schema_revisions"] == ["0182"]
    assert body["environment"] == "staging"
    assert set(body["queues"][0]) == {
        "queue",
        "pending",
        "ready",
        "leased",
        "failed",
        "oldest_pending_at",
        "last_progress_at",
    }
    assert "no-store" in response.headers["cache-control"]


def test_private_scrape_exports_counts_without_asserting_liveness() -> None:
    with TestClient(_app(operator=True)) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert 'ec_backend_queue_pending{queue="request_start"} 3' in response.text
    assert 'ec_backend_queue_leased{queue="request_start"} 1' in response.text
    assert "liveness" not in response.text
    assert "tenant" not in response.text
    assert "last_progress_at_timestamp_seconds{" not in response.text
    with TestClient(_app()) as client:
        assert client.get("/metrics").status_code == 404


def test_backend_database_failure_is_unavailable_without_sensitive_details() -> None:
    class BrokenOperations:
        async def overview(self) -> BackendOperationsSnapshot:
            raise RuntimeError("postgresql://secret:user@db/tenant, raw payload and SQL")

    app = _app()
    app.state.operator_operations = BrokenOperations()
    with TestClient(app) as client:
        response = client.get("/admin/v1/operations/overview")
    assert response.status_code == 503
    assert response.json() == {"detail": "backend operations projection is unavailable"}
    assert "no-store" in response.headers["cache-control"]
