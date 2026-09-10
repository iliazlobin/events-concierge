"""Operator-only backend queue visibility, independent of provider availability."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Protocol
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from ..adapters.postgres.operator_operations import (
    BackendOperationErrorsSnapshot,
    BackendOperationRecordsSnapshot,
    BackendOperationsSnapshot,
    OperationErrorQueue,
    OperationRecordQueue,
    OperationRecordScope,
    validate_record_query,
)
from .admin import _local_ingestion_admin


class OperationsReader(Protocol):
    async def overview(self) -> BackendOperationsSnapshot: ...

    async def errors(
        self,
        *,
        queue: OperationErrorQueue,
        offset: int = 0,
        limit: int = 10,
        record_id: UUID | None = None,
    ) -> BackendOperationErrorsSnapshot: ...

    async def records(
        self,
        *,
        queue: OperationRecordQueue,
        scope: OperationRecordScope = "pending",
        offset: int = 0,
        limit: int = 10,
        record_id: str | None = None,
    ) -> BackendOperationRecordsSnapshot: ...


class OperationErrorSourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    source_id: UUID
    provider_key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    status: Literal["failed", "blocked"]
    error_code: (
        Literal[
            "unavailable",
            "rate_limited",
            "invalid_response",
            "unsupported_profile",
            "network_policy",
            "unclassified",
        ]
        | None
    )
    error_summary: str = Field(max_length=200)
    observed_at: datetime | None
    next_refresh_at: datetime | None


class OperationErrorOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    record_id: UUID
    label: str = Field(min_length=1, max_length=160)
    state: Literal["ready", "scheduled", "leased", "due"]
    error_code: Literal["test_retry_fixture", "unclassified", "source_errors"]
    error_summary: str = Field(max_length=200)
    attempt_count: int | None = Field(ge=0)
    created_at: datetime | None
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    last_observed_at: datetime | None
    sources: list[OperationErrorSourceOut]


class OperationErrorsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    generated_at: datetime
    queue: OperationErrorQueue
    total: int = Field(ge=0)
    offset: int = Field(ge=0, le=10000)
    limit: int = Field(ge=1, le=50)
    items: list[OperationErrorOut]


class OperationRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    record_id: str = Field(min_length=1, max_length=36)
    label: Literal["Request start", "Notification work"]
    state: Literal["ready", "scheduled", "leased", "failed"]
    error_code: (
        Literal[
            "test_retry_fixture",
            "unclassified",
            "delivery_failed",
            "unsafe_projection",
            "unsupported_topic",
            "missing_protected_capability",
            "lease_lost",
            "delivery_busy",
        ]
        | None
    )
    error_summary: str | None = Field(max_length=200)
    attempt_count: int = Field(ge=0)
    attempt_kind: Literal["failed_start_attempts", "failed_delivery_attempts"]
    created_at: datetime
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    last_observed_at: datetime | None
    failed_at: datetime | None
    sources: list[OperationErrorSourceOut] = Field(max_length=0)


class OperationRecordsOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    generated_at: datetime
    queue: OperationRecordQueue
    scope: OperationRecordScope
    total: int = Field(ge=0)
    offset: int = Field(ge=0, le=10000)
    limit: int = Field(ge=1, le=50)
    items: list[OperationRecordOut]


class BackendQueueOut(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    queue: str = Field(pattern=r"^[a-z_]{1,64}$")
    pending: int = Field(ge=0)
    ready: int = Field(ge=0)
    leased: int = Field(ge=0)
    failed: int = Field(ge=0)
    oldest_pending_at: datetime | None
    last_progress_at: datetime | None


class BackendOverviewOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: datetime
    environment: str
    release_revision: str
    image_digest: str | None
    schema_revisions: tuple[str, ...]
    measurement_scope: Literal["durable_queue_state"] = "durable_queue_state"
    worker_liveness: Literal["not_measured"] = "not_measured"
    queues: list[BackendQueueOut]


async def _snapshot(request: Request) -> BackendOperationsSnapshot:
    repository: OperationsReader | None = getattr(request.app.state, "operator_operations", None)
    if repository is None:
        raise HTTPException(
            503,
            "backend operations projection is unavailable",
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    try:
        return await repository.overview()
    except Exception as error:
        raise HTTPException(
            503,
            "backend operations projection is unavailable",
            headers={"Cache-Control": "no-store, max-age=0"},
        ) from error


def install_operator_operations_routes(app: FastAPI) -> None:
    """Use the same verified operator authority as catalog GET endpoints."""

    @app.get("/admin/v1/operations/records", response_model=OperationRecordsOut)
    async def operation_records(
        request: Request,
        response: Response,
        authority: Annotated[object, Depends(_local_ingestion_admin)],
        queue: OperationRecordQueue,
        scope: OperationRecordScope = "pending",
        offset: int = Query(default=0, ge=0, le=10000),
        limit: int = Query(default=10, ge=1, le=50),
        record_id: str | None = Query(default=None, min_length=1, max_length=36),
    ) -> OperationRecordsOut:
        del authority
        response.headers["Cache-Control"] = "no-store, max-age=0"
        try:
            validate_record_query(queue, scope, offset, limit, record_id)
        except ValueError as error:
            raise HTTPException(422, "invalid operator record query") from error
        repository: OperationsReader | None = getattr(
            request.app.state, "operator_operations", None
        )
        try:
            if repository is None:
                raise RuntimeError("operator records unavailable")
            return OperationRecordsOut.model_validate(
                await repository.records(
                    queue=queue,
                    scope=scope,
                    offset=offset,
                    limit=limit,
                    record_id=record_id,
                )
            )
        except Exception as error:
            raise HTTPException(
                503,
                "operation records are unavailable",
                headers={"Cache-Control": "no-store, max-age=0"},
            ) from error

    @app.get("/admin/v1/operations/errors", response_model=OperationErrorsOut)
    async def operation_errors(
        request: Request,
        response: Response,
        authority: Annotated[object, Depends(_local_ingestion_admin)],
        queue: OperationErrorQueue,
        offset: int = Query(default=0, ge=0, le=10000),
        limit: int = Query(default=10, ge=1, le=50),
        record_id: UUID | None = None,
    ) -> OperationErrorsOut:
        del authority
        response.headers["Cache-Control"] = "no-store, max-age=0"
        repository: OperationsReader | None = getattr(
            request.app.state, "operator_operations", None
        )
        try:
            if repository is None:
                raise RuntimeError("operator diagnostics unavailable")
            snapshot = await repository.errors(
                queue=queue, offset=offset, limit=limit, record_id=record_id
            )
            return OperationErrorsOut.model_validate(snapshot)
        except Exception as error:
            raise HTTPException(
                503,
                "operation error diagnostics are unavailable",
                headers={"Cache-Control": "no-store, max-age=0"},
            ) from error

    @app.get("/admin/v1/operations/overview", response_model=BackendOverviewOut)
    async def backend_overview(
        request: Request,
        response: Response,
        authority: Annotated[object, Depends(_local_ingestion_admin)],
    ) -> BackendOverviewOut:
        del authority
        response.headers["Cache-Control"] = "no-store, max-age=0"
        snapshot = await _snapshot(request)
        settings = request.app.state.settings
        return BackendOverviewOut(
            generated_at=snapshot.generated_at,
            environment=str(settings.env),
            release_revision=settings.release_revision,
            image_digest=settings.image_digest,
            schema_revisions=snapshot.schema_revisions,
            queues=[BackendQueueOut.model_validate(queue) for queue in snapshot.queues],
        )

    if getattr(app.state, "operator_boundary", False):
        # Private scrape surface. The operator web proxy does not forward /metrics; Helm permits
        # the GMP scraper. No tenant or command labels, and scrape failure never becomes zeros.
        @app.get("/metrics", include_in_schema=False)
        async def backend_metrics(request: Request) -> Response:
            snapshot = await _snapshot(request)
            lines = [
                "# TYPE ec_backend_snapshot_timestamp_seconds gauge",
                f"ec_backend_snapshot_timestamp_seconds {snapshot.generated_at.timestamp()}",
            ]
            queues = [BackendQueueOut.model_validate(queue) for queue in snapshot.queues]
            for field in ("pending", "ready", "leased", "failed"):
                lines.append(f"# TYPE ec_backend_queue_{field} gauge")
                for queue in queues:
                    lines.append(
                        f'ec_backend_queue_{field}{{queue="{queue.queue}"}} {getattr(queue, field)}'
                    )
            for field in ("oldest_pending_at", "last_progress_at"):
                metric = f"ec_backend_queue_{field}_timestamp_seconds"
                lines.append(f"# TYPE {metric} gauge")
                for queue in queues:
                    timestamp: datetime | None = getattr(queue, field)
                    if timestamp is not None:
                        lines.append(f'{metric}{{queue="{queue.queue}"}} {timestamp.timestamp()}')
            return Response(
                "\n".join(lines) + "\n",
                media_type="text/plain; version=0.0.4",
                headers={"Cache-Control": "no-store, max-age=0"},
            )
