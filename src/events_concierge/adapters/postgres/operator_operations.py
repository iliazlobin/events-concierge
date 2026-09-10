"""Read restricted operator projections without exposing tenant payloads or raw failures."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...infra.db import system_session_scope

SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]
_ERROR_MAX_OFFSET = 10000
_ERROR_MAX_LIMIT = 50
_MIN_BIGINT_ID = -9223372036854775808
_MAX_BIGINT_ID = 9223372036854775807


@dataclass(frozen=True)
class BackendQueueSnapshot:
    queue: str
    pending: int
    ready: int
    leased: int
    failed: int
    oldest_pending_at: datetime | None
    last_progress_at: datetime | None


@dataclass(frozen=True)
class BackendOperationsSnapshot:
    generated_at: datetime
    schema_revisions: tuple[str, ...]
    queues: tuple[BackendQueueSnapshot, ...]


OperationErrorQueue = Literal["request_start", "entity_refresh"]
OperationRecordQueue = Literal["request_start", "notifications"]
OperationRecordScope = Literal["pending", "errors", "failed"]


def validate_record_query(
    queue: str, scope: str, offset: int, limit: int, record_id: str | None
) -> None:
    valid_scope = {"request_start": {"pending", "errors"}, "notifications": {"pending", "failed"}}
    invalid = (
        scope not in valid_scope.get(queue, set())
        or not 0 <= offset <= _ERROR_MAX_OFFSET
        or not 1 <= limit <= _ERROR_MAX_LIMIT
    )
    if record_id is not None:
        if queue == "request_start":
            invalid |= (
                re.fullmatch(
                    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}",
                    record_id,
                )
                is None
            )
        else:
            invalid |= not (
                re.fullmatch(r"(?:0|-?[1-9][0-9]{0,18})", record_id)
                and _MIN_BIGINT_ID <= int(record_id) <= _MAX_BIGINT_ID
            )
    if invalid:
        raise ValueError("invalid operator record query")


@dataclass(frozen=True)
class BackendOperationErrorSource:
    source_id: UUID
    provider_key: str
    status: str
    error_code: str | None
    error_summary: str
    observed_at: datetime | None
    next_refresh_at: datetime | None


@dataclass(frozen=True)
class BackendOperationError:
    record_id: UUID
    label: str
    state: str
    error_code: str
    error_summary: str
    attempt_count: int | None
    created_at: datetime | None
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    last_observed_at: datetime | None
    sources: tuple[BackendOperationErrorSource, ...] = ()


@dataclass(frozen=True)
class BackendOperationErrorsSnapshot:
    generated_at: datetime
    queue: OperationErrorQueue
    total: int
    offset: int
    limit: int
    items: tuple[BackendOperationError, ...]


@dataclass(frozen=True)
class BackendOperationRecord:
    record_id: str
    label: str
    state: str
    error_code: str | None
    error_summary: str | None
    attempt_count: int
    attempt_kind: str
    created_at: datetime
    next_attempt_at: datetime | None
    lease_expires_at: datetime | None
    last_observed_at: datetime | None
    failed_at: datetime | None
    sources: tuple[BackendOperationErrorSource, ...] = ()


@dataclass(frozen=True)
class BackendOperationRecordsSnapshot:
    generated_at: datetime
    queue: OperationRecordQueue
    scope: OperationRecordScope
    total: int
    offset: int
    limit: int
    items: tuple[BackendOperationRecord, ...]


class PostgresOperatorOperationsRepository:
    """Only fixed owner-defined projections are callable through this adapter."""

    def __init__(self, session_scope: SessionScope = system_session_scope) -> None:
        self._session_scope = session_scope

    async def records(
        self,
        *,
        queue: OperationRecordQueue,
        scope: OperationRecordScope = "pending",
        offset: int = 0,
        limit: int = 10,
        record_id: str | None = None,
    ) -> BackendOperationRecordsSnapshot:
        validate_record_query(queue, scope, offset, limit, record_id)
        async with self._session_scope() as session:
            result = (
                await session.execute(
                    text("""SELECT public.fn_get_operator_records_v1(
                        :queue, :scope, :offset, :limit, :record_id
                    )"""),
                    {
                        "queue": queue,
                        "scope": scope,
                        "offset": offset,
                        "limit": limit,
                        "record_id": record_id,
                    },
                )
            ).scalar_one()
        return BackendOperationRecordsSnapshot(
            generated_at=datetime.fromisoformat(result["generated_at"]),
            queue=queue,
            scope=scope,
            total=int(result["total"]),
            offset=int(result["offset"]),
            limit=int(result["limit"]),
            items=tuple(
                BackendOperationRecord(
                    record_id=row["record_id"],
                    label=row["label"],
                    state=row["state"],
                    error_code=row["error_code"],
                    error_summary=row["error_summary"],
                    attempt_count=int(row["attempt_count"]),
                    attempt_kind=row["attempt_kind"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                    next_attempt_at=_timestamp(row["next_attempt_at"]),
                    lease_expires_at=_timestamp(row["lease_expires_at"]),
                    last_observed_at=_timestamp(row["last_observed_at"]),
                    failed_at=_timestamp(row["failed_at"]),
                )
                for row in result["items"]
            ),
        )

    async def errors(
        self,
        *,
        queue: OperationErrorQueue,
        offset: int = 0,
        limit: int = 10,
        record_id: UUID | None = None,
    ) -> BackendOperationErrorsSnapshot:
        if (
            queue not in {"request_start", "entity_refresh"}
            or not 0 <= offset <= _ERROR_MAX_OFFSET
            or not 1 <= limit <= _ERROR_MAX_LIMIT
        ):
            raise ValueError("invalid operator error query")
        async with self._session_scope() as session:
            result = (
                await session.execute(
                    text("""
                SELECT public.fn_get_operator_errors_v1(
                    :queue, :offset, :limit, CAST(:record_id AS uuid)
                )
            """),
                    {"queue": queue, "offset": offset, "limit": limit, "record_id": record_id},
                )
            ).scalar_one()
        return BackendOperationErrorsSnapshot(
            generated_at=datetime.fromisoformat(result["generated_at"]),
            queue=queue,
            total=int(result["total"]),
            offset=int(result["offset"]),
            limit=int(result["limit"]),
            items=tuple(
                BackendOperationError(
                    record_id=UUID(row["record_id"]),
                    label=row["label"],
                    state=row["state"],
                    error_code=row["error_code"],
                    error_summary=row["error_summary"],
                    attempt_count=row["attempt_count"],
                    created_at=_timestamp(row["created_at"]),
                    next_attempt_at=_timestamp(row["next_attempt_at"]),
                    lease_expires_at=_timestamp(row["lease_expires_at"]),
                    last_observed_at=_timestamp(row["last_observed_at"]),
                    sources=tuple(
                        BackendOperationErrorSource(
                            source_id=UUID(source["source_id"]),
                            provider_key=source["provider_key"],
                            status=source["status"],
                            error_code=source["error_code"],
                            error_summary=source["error_summary"],
                            observed_at=_timestamp(source["observed_at"]),
                            next_refresh_at=_timestamp(source["next_refresh_at"]),
                        )
                        for source in row["sources"]
                    ),
                )
                for row in result["items"]
            ),
        )

    async def overview(self) -> BackendOperationsSnapshot:
        async with self._session_scope() as session:
            # A single statement observes one MVCC snapshot, including every queue and schema.
            result = (
                (
                    await session.execute(
                        text("""
                    SELECT statement_timestamp() AS generated_at,
                           ARRAY(SELECT version_num
                                 FROM public.fn_get_operator_schema_head_v1()
                                 ORDER BY version_num) AS schema_revisions,
                           COALESCE((SELECT jsonb_agg(to_jsonb(q) ORDER BY q.queue)
                                     FROM public.fn_get_operator_backend_overview_v1() q),
                                    '[]'::jsonb) AS queues
                """)
                    )
                )
                .mappings()
                .one()
            )
        return BackendOperationsSnapshot(
            generated_at=result["generated_at"],
            schema_revisions=tuple(str(value) for value in result["schema_revisions"]),
            queues=tuple(
                BackendQueueSnapshot(
                    queue=str(row["queue"]),
                    pending=int(row["pending"]),
                    ready=int(row["ready"]),
                    leased=int(row["leased"]),
                    failed=int(row["failed"]),
                    oldest_pending_at=_timestamp(row["oldest_pending_at"]),
                    last_progress_at=_timestamp(row["last_progress_at"]),
                )
                for row in result["queues"]
            ),
        )


def _timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None
