"""Bounded, authenticated work diagnostics without consumer records or raw failures."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from events_concierge.adapters.postgres.operator_operations import (
    BackendOperationError,
    BackendOperationErrorSource,
    BackendOperationErrorsSnapshot,
    OperationErrorQueue,
)
from events_concierge.api.admin import _local_ingestion_admin
from events_concierge.api.operator_operations import install_operator_operations_routes

_ID = UUID("019a7137-8b68-7bf4-b75c-000100000001")
_STAMP = datetime(2026, 9, 8, tzinfo=UTC)


class _Errors:
    def __init__(self) -> None:
        self.calls: list[tuple[OperationErrorQueue, int, int, UUID | None]] = []

    async def errors(
        self,
        *,
        queue: OperationErrorQueue,
        offset: int = 0,
        limit: int = 10,
        record_id: UUID | None = None,
    ) -> BackendOperationErrorsSnapshot:
        self.calls.append((queue, offset, limit, record_id))
        sources = (
            ()
            if queue == "request_start"
            else (
                BackendOperationErrorSource(
                    source_id=_ID,
                    provider_key="official_website",
                    status="blocked",
                    error_code="network_policy",
                    error_summary="Network policy blocked this source.",
                    observed_at=_STAMP,
                    next_refresh_at=datetime(2026, 9, 9, tzinfo=UTC),
                ),
            )
        )
        item = BackendOperationError(
            record_id=_ID,
            label="Request 019a7137" if not sources else "Public organizer",
            state="scheduled",
            error_code="test_retry_fixture" if not sources else "source_errors",
            error_summary="Recorded test retry after reclaim."
            if not sources
            else "Source errors recorded.",
            attempt_count=1 if not sources else None,
            created_at=_STAMP,
            next_attempt_at=datetime(2099, 1, 2, tzinfo=UTC),
            lease_expires_at=None,
            last_observed_at=_STAMP if sources else None,
            sources=sources,
        )
        return BackendOperationErrorsSnapshot(_STAMP, queue, 1, offset, limit, (item,))


def _app(repository: object | None = None) -> FastAPI:
    app = FastAPI()
    app.state.settings = SimpleNamespace(env="local", release_revision="test", image_digest=None)
    if repository is not None:
        app.state.operator_operations = repository
    install_operator_operations_routes(app)
    app.dependency_overrides[_local_ingestion_admin] = object
    return app


@pytest.mark.parametrize("queue", ["request_start", "entity_refresh"])
def test_errors_return_bounded_evidence_and_no_store(queue: str) -> None:
    repository = _Errors()
    with TestClient(_app(repository)) as client:
        response = client.get(
            "/admin/v1/operations/errors",
            params={
                "queue": queue,
                "offset": 10,
                "limit": 5,
                "record_id": str(_ID),
            },
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    body = response.json()
    assert body["queue"] == queue
    assert repository.calls == [(queue, 10, 5, _ID)]
    assert (body["offset"], body["limit"], body["total"]) == (10, 5, 1)
    row = body["items"][0]
    assert row["record_id"] == str(_ID)
    assert row["next_attempt_at"].startswith("2099-01-02")
    assert set(row) == {
        "record_id",
        "label",
        "state",
        "error_code",
        "error_summary",
        "attempt_count",
        "created_at",
        "next_attempt_at",
        "lease_expires_at",
        "last_observed_at",
        "sources",
    }
    if queue == "request_start":
        assert row["last_observed_at"] is None  # There is no recorded failure timestamp.
        assert row["sources"] == []
    else:
        assert row["sources"][0]["error_code"] == "network_policy"


@pytest.mark.parametrize(
    "query",
    [
        "",
        "queue=notifications",
        "queue=request_start&limit=0",
        "queue=request_start&limit=51",
        "queue=request_start&offset=-1",
        "queue=request_start&offset=10001",
        "queue=request_start&record_id=private-record",
        "queue=request_start&limit=not-a-number",
    ],
)
def test_invalid_diagnostic_queries_never_reach_repository(query: str) -> None:
    repository = _Errors()
    with TestClient(_app(repository)) as client:
        assert client.get(f"/admin/v1/operations/errors?{query}").status_code == 422
    assert repository.calls == []


def test_error_read_requires_operator_authority() -> None:
    repository = _Errors()
    app = _app(repository)

    def denied() -> object:
        raise HTTPException(403, "operator access required")

    app.dependency_overrides[_local_ingestion_admin] = denied
    with TestClient(app) as client:
        assert client.get("/admin/v1/operations/errors?queue=request_start").status_code == 403
    assert repository.calls == []


def test_missing_and_failed_projection_are_unavailable_without_database_details() -> None:
    class BrokenErrors:
        async def errors(self, **_: object) -> BackendOperationErrorsSnapshot:
            raise RuntimeError("postgresql://secret:password@db raw tenant request text")

    for repository in (None, BrokenErrors()):
        with TestClient(_app(repository)) as client:
            response = client.get("/admin/v1/operations/errors?queue=request_start")
        assert response.status_code == 503
        assert "no-store" in response.headers["cache-control"]
        assert "secret" not in response.text
        assert "tenant" not in response.text
        assert "unavailable" in response.text


def test_diagnostics_expose_no_retry_or_delete_route() -> None:
    repository = _Errors()
    with TestClient(_app(repository)) as client:
        for method in ("POST", "PATCH", "DELETE"):
            assert (
                client.request(
                    method, "/admin/v1/operations/errors?queue=request_start"
                ).status_code
                == 405
            )
    assert repository.calls == []
