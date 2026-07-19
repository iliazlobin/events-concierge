"""PostgreSQL control-plane adapter for orphaned handoff-TTL recovery (FR-6.6, ADR-007)."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import text

from ...infra.db import system_session_scope
from ...ports.handoff_expiry import HandoffExpiryRecord, HandoffExpiryRepairOutcome


class _HandoffExpiryRow(Protocol):
    """Named fields returned by the queue's explicit global claim projection."""

    task_id: str
    tenant_id: UUID
    workflow_id: str
    canonical_event_id: UUID
    expiry_transition_id: str
    ttl_expires_at: datetime
    attempt_count: int
    lease_token: str


class PostgresHandoffExpiryRepository:
    """Lease only opaque, post-grace TTL repair records across tenants (FR-6.6, ADR-007)."""

    async def claim_batch(self, limit: int, lease_seconds: int) -> list[HandoffExpiryRecord]:
        """Claim records after their five-minute grace, recovering safely after worker crashes."""
        if limit < 1:
            raise ValueError("handoff expiry claim limit must be positive")
        if lease_seconds < 1:
            raise ValueError("handoff expiry lease_seconds must be positive")
        lease_token = uuid4().hex
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        WITH candidates AS (
                            SELECT task_id
                            FROM handoff_expiry_queue
                            WHERE resolved_at IS NULL
                              AND eligible_at <= clock_timestamp()
                              AND next_attempt_at <= clock_timestamp()
                              AND (lease_expires_at IS NULL
                                   OR lease_expires_at <= clock_timestamp())
                            ORDER BY next_attempt_at, task_id
                            FOR UPDATE SKIP LOCKED
                            LIMIT :limit
                        )
                        UPDATE handoff_expiry_queue AS expiry
                        SET lease_token = :lease_token,
                            lease_expires_at = clock_timestamp()
                                + (:lease_seconds * INTERVAL '1 second'),
                            attempt_count = expiry.attempt_count + 1
                        FROM candidates
                        WHERE expiry.task_id = candidates.task_id
                        RETURNING expiry.task_id, expiry.tenant_id, expiry.workflow_id,
                                  expiry.canonical_event_id, expiry.expiry_transition_id,
                                  expiry.ttl_expires_at, expiry.attempt_count, expiry.lease_token
                        """
                    ),
                    {
                        "limit": limit,
                        "lease_token": lease_token,
                        "lease_seconds": lease_seconds,
                    },
                )
            ).all()
        return [self._record(cast(_HandoffExpiryRow, row)) for row in rows]

    async def acknowledge(self, record: HandoffExpiryRecord) -> bool:
        """Resolve only an exact, still-live post-grace repair lease (FR-6.6, ADR-007)."""
        async with system_session_scope() as session:
            acknowledged = (
                await session.execute(
                    text(
                        """
                        UPDATE handoff_expiry_queue
                        SET resolved_at = clock_timestamp(),
                            lease_token = NULL,
                            lease_expires_at = NULL,
                            last_error = NULL
                        WHERE task_id = :task_id
                          AND lease_token = :lease_token
                          AND resolved_at IS NULL
                          AND lease_expires_at > pg_catalog.clock_timestamp()
                        RETURNING task_id
                        """
                    ),
                    {"task_id": record.task_id, "lease_token": record.lease_token},
                )
            ).first()
        return acknowledged is not None

    async def expire_orphan(self, record: HandoffExpiryRecord) -> HandoffExpiryRepairOutcome:
        """Atomically fence an orphan-only expiry effect at its exact live queue lease (ADR-007)."""
        async with system_session_scope() as session:
            raw_outcome = str(
                (
                    await session.execute(
                        text(
                            """
                            SELECT public.fn_expire_handoff_from_queue(
                                :task_id,
                                :lease_token
                            ) AS outcome
                            """
                        ),
                        {"task_id": record.task_id, "lease_token": record.lease_token},
                    )
                ).scalar_one()
            )
        try:
            return HandoffExpiryRepairOutcome(raw_outcome)
        except ValueError as error:
            raise RuntimeError("handoff expiry capability returned an invalid outcome") from error

    async def reschedule(
        self, record: HandoffExpiryRecord, *, retry_at: datetime, error: str
    ) -> bool:
        """Release only a live failed repair without losing the TTL obligation (ADR-007)."""
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            raise ValueError("handoff expiry retry_at must be timezone-aware")
        async with system_session_scope() as session:
            released = (
                await session.execute(
                    text(
                        """
                        UPDATE handoff_expiry_queue
                        SET next_attempt_at = :retry_at,
                            lease_token = NULL,
                            lease_expires_at = NULL,
                            last_error = :error
                        WHERE task_id = :task_id
                          AND lease_token = :lease_token
                          AND resolved_at IS NULL
                          AND lease_expires_at > pg_catalog.clock_timestamp()
                        RETURNING task_id
                        """
                    ),
                    {
                        "task_id": record.task_id,
                        "lease_token": record.lease_token,
                        "retry_at": retry_at,
                        "error": error[:1000],
                    },
                )
            ).first()
        return released is not None

    @staticmethod
    def _record(row: _HandoffExpiryRow) -> HandoffExpiryRecord:
        """Decode the intentionally opaque global queue projection."""
        return HandoffExpiryRecord(
            task_id=row.task_id,
            tenant_id=row.tenant_id,
            workflow_id=row.workflow_id,
            canonical_event_id=row.canonical_event_id,
            expiry_transition_id=row.expiry_transition_id,
            ttl_expires_at=row.ttl_expires_at,
            attempt_count=int(row.attempt_count),
            lease_token=row.lease_token,
        )
