"""Read-only, validated request-start and notification record API contracts."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from tests.unit.test_operator_operation_errors import _app

from events_concierge.adapters.postgres.operator_operations import (
    BackendOperationRecord,
    BackendOperationRecordsSnapshot,
    OperationRecordQueue,
    OperationRecordScope,
    validate_record_query,
)
from events_concierge.api.admin import _local_ingestion_admin

_UUID = "019a7137-8b68-7bf4-b75c-000100000001"
_BIGINT = "9007199254740993"
_STAMP = datetime(2026, 9, 8, tzinfo=UTC)


def test_alembic_console_script_loads_revisions_without_repository_pythonpath() -> None:
    root = Path(__file__).resolve().parents[2]
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    result = subprocess.run(
        [str(root / ".venv" / "bin" / "alembic"), "heads"],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "(head)" in result.stdout


class _Records:
    def __init__(self) -> None:
        self.calls: list[
            tuple[OperationRecordQueue, OperationRecordScope, int, int, str | None]
        ] = []

    async def records(
        self,
        *,
        queue: OperationRecordQueue,
        scope: OperationRecordScope = "pending",
        offset: int = 0,
        limit: int = 10,
        record_id: str | None = None,
    ) -> BackendOperationRecordsSnapshot:
        self.calls.append((queue, scope, offset, limit, record_id))
        notification = queue == "notifications"
        failed = scope == "failed"
        item = BackendOperationRecord(
            record_id=record_id or (_BIGINT if notification else _UUID),
            label="Notification work" if notification else "Request start",
            state="failed" if failed else "scheduled",
            error_code="unsafe_projection" if failed else None,
            error_summary="Notification work was quarantined." if failed else None,
            attempt_count=0,
            attempt_kind="failed_delivery_attempts" if notification else "failed_start_attempts",
            created_at=_STAMP,
            next_attempt_at=None if failed else datetime(2099, 1, 2, tzinfo=UTC),
            lease_expires_at=None,
            last_observed_at=_STAMP if failed else None,
            failed_at=_STAMP if failed else None,
        )
        return BackendOperationRecordsSnapshot(_STAMP, queue, scope, 1, offset, limit, (item,))


@pytest.mark.parametrize(
    "queue,scope,reference",
    [
        ("request_start", "pending", _UUID),
        ("request_start", "errors", _UUID),
        ("notifications", "pending", _BIGINT),
        ("notifications", "failed", _BIGINT),
        ("notifications", "pending", "-9222999999738606380"),
        ("notifications", "pending", "-9223372036854775808"),
        ("notifications", "failed", "0"),
    ],
)
def test_record_scopes_preserve_ids_attempt_meanings_and_nullable_errors(
    queue: str,
    scope: str,
    reference: str,
) -> None:
    repository = _Records()
    with TestClient(_app(repository)) as client:
        response = client.get(
            "/admin/v1/operations/records",
            params={
                "queue": queue,
                "scope": scope,
                "record_id": reference,
                "offset": 10,
                "limit": 5,
            },
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store, max-age=0"
    body = response.json()
    assert (body["queue"], body["scope"], body["total"], body["offset"], body["limit"]) == (
        queue,
        scope,
        1,
        10,
        5,
    )
    assert repository.calls == [(queue, scope, 10, 5, reference)]
    row = body["items"][0]
    assert row["record_id"] == reference  # bigint identifiers must never round through a JS number.
    assert row["attempt_kind"] == (
        "failed_delivery_attempts" if queue == "notifications" else "failed_start_attempts"
    )
    assert row["attempt_count"] == 0 and row["sources"] == []
    assert set(row) == {
        "record_id",
        "label",
        "state",
        "error_code",
        "error_summary",
        "attempt_count",
        "attempt_kind",
        "created_at",
        "next_attempt_at",
        "lease_expires_at",
        "last_observed_at",
        "failed_at",
        "sources",
    }
    if scope == "failed":
        assert row["state"] == "failed" and row["failed_at"]
        assert row["next_attempt_at"] is None and row["lease_expires_at"] is None
    else:
        assert row["error_code"] is None and row["error_summary"] is None
        assert row["next_attempt_at"].startswith("2099-01-02")


def test_pending_is_the_default_scope() -> None:
    repository = _Records()
    with TestClient(_app(repository)) as client:
        assert client.get("/admin/v1/operations/records?queue=request_start").status_code == 200
    assert repository.calls == [("request_start", "pending", 0, 10, None)]


@pytest.mark.parametrize(
    "query",
    [
        "",
        "queue=entity_refresh",
        "queue=notifications&scope=errors",
        "queue=request_start&scope=failed",
        "queue=request_start&offset=-1",
        "queue=notifications&offset=10001",
        "queue=request_start&limit=0",
        "queue=notifications&limit=51",
        "queue=request_start&record_id=42",
        f"queue=notifications&record_id={_UUID}",
        "queue=notifications&record_id=01",
        "queue=notifications&record_id=-0",
        "queue=notifications&record_id=-01",
        "queue=notifications&record_id=%2B1",
        "queue=notifications&record_id=-9223372036854775809",
        "queue=notifications&record_id=9223372036854775808",
        "queue=request_start&record_id=private-raw-input",
    ],
)
def test_invalid_pairs_bounds_and_reference_types_never_reach_repository(query: str) -> None:
    repository = _Records()
    with TestClient(_app(repository)) as client:
        assert client.get(f"/admin/v1/operations/records?{query}").status_code == 422
    assert repository.calls == []


@pytest.mark.parametrize(
    "reference",
    [
        "-9223372036854775808",
        "-9222999999738606380",
        "-1",
        "0",
        "1",
        "9223372036854775807",
    ],
)
def test_database_bigint_boundary_is_validated_without_float_conversion(reference: str) -> None:
    validate_record_query("notifications", "pending", 10000, 50, reference)


@pytest.mark.parametrize(
    "reference", ["9223372036854775808", "-9223372036854775809", "-0", "-01", "+1", "1.0"]
)
def test_database_bigint_overflow_and_noncanonical_references_are_rejected(reference: str) -> None:
    with pytest.raises(ValueError, match="invalid operator record query"):
        validate_record_query("notifications", "pending", 0, 10, reference)


def test_record_reads_require_operator_authority_and_offer_no_mutations() -> None:
    repository = _Records()
    app = _app(repository)

    def denied() -> object:
        raise HTTPException(403, "operator access required")

    app.dependency_overrides[_local_ingestion_admin] = denied
    with TestClient(app) as client:
        assert client.get("/admin/v1/operations/records?queue=request_start").status_code == 403
        for method in ("POST", "PATCH", "DELETE"):
            assert (
                client.request(
                    method, "/admin/v1/operations/records?queue=notifications"
                ).status_code
                == 405
            )
    assert repository.calls == []


def test_unavailable_record_reader_never_exposes_database_or_tenant_details() -> None:
    class Broken:
        async def records(self, **_: object) -> BackendOperationRecordsSnapshot:
            raise RuntimeError("postgresql://private:password@db tenant payload recipient")

    for repository in (None, Broken()):
        with TestClient(_app(repository)) as client:
            response = client.get("/admin/v1/operations/records?queue=notifications")
        assert response.status_code == 503
        assert "no-store" in response.headers["cache-control"]
        assert response.json()["detail"] == "operation records are unavailable"
