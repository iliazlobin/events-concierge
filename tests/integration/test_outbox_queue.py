"""Postgres-backed queue observability for the ADR-009 outbox worker."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from events_concierge.adapters.postgres.tenant_repos import PostgresOutboxRepository
from events_concierge.infra.db import system_session_scope

pytestmark = pytest.mark.integration


async def test_queue_snapshot_matches_claim_and_active_lease_predicates(db: None) -> None:
    """Ready/leased aggregates mirror ``claim_batch`` without exposing outbox payloads (ADR-009)."""
    repository = PostgresOutboxRepository()
    before = await repository.queue_snapshot()
    tag = uuid4().hex
    topic = f"test.outbox-queue.{tag}"
    tenant_id = uuid4()
    now = datetime.now(UTC)
    oldest_ready_at = datetime(2020, 1, 1, tzinfo=UTC)
    later_ready_at = oldest_ready_at + timedelta(seconds=1)

    rows = (
        # Ready because it is due and has no active lease.
        ("ready", oldest_ready_at, now - timedelta(minutes=1), None, None, None),
        # Ready because an abandoned lease expired.
        (
            "expired-lease",
            later_ready_at,
            now - timedelta(minutes=1),
            now - timedelta(seconds=1),
            None,
            None,
        ),
        # Pending but scheduled for a future retry.
        ("future", now, now + timedelta(hours=1), None, None, None),
        # Pending but unavailable to another worker while its lease is active.
        (
            "active-lease",
            now,
            now - timedelta(minutes=1),
            now + timedelta(hours=1),
            None,
            None,
        ),
        # Terminal rows must not inflate queue pressure.
        ("delivered", now, now - timedelta(minutes=1), None, now, None),
        ("failed", now, now - timedelta(minutes=1), None, None, now),
    )

    try:
        async with system_session_scope() as session:
            for (
                name,
                created_at,
                next_attempt_at,
                lease_expires_at,
                delivered_at,
                failed_at,
            ) in rows:
                await session.execute(
                    text(
                        """INSERT INTO outbox
                               (tenant_id, topic, payload, created_at, next_attempt_at,
                                lease_expires_at, delivered_at, failed_at)
                           VALUES
                               (:tenant_id, :topic, CAST(:payload AS jsonb), :created_at,
                                :next_attempt_at, :lease_expires_at, :delivered_at, :failed_at)"""
                    ),
                    {
                        "tenant_id": tenant_id,
                        "topic": topic,
                        "payload": json.dumps({"fixture": name, "tag": tag}),
                        "created_at": created_at,
                        "next_attempt_at": next_attempt_at,
                        "lease_expires_at": lease_expires_at,
                        "delivered_at": delivered_at,
                        "failed_at": failed_at,
                    },
                )

        after = await repository.queue_snapshot()

        assert after.pending == before.pending + 4
        assert after.ready == before.ready + 2
        assert after.leased == before.leased + 1
        expected_oldest = (
            oldest_ready_at
            if before.oldest_ready_at is None
            else min(before.oldest_ready_at, oldest_ready_at)
        )
        assert after.oldest_ready_at == expected_oldest
    finally:
        async with system_session_scope() as session:
            await session.execute(text("DELETE FROM outbox WHERE topic = :topic"), {"topic": topic})
