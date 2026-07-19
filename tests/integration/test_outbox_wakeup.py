"""PostgreSQL commit-boundary coverage for the ADR-009 outbox wake-up trigger."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from sqlalchemy import text

from events_concierge.adapters.postgres.outbox_wakeup import PostgresOutboxWakeup
from events_concierge.config import get_settings
from events_concierge.infra.db import get_engine, system_session_scope
from events_concierge.ports.outbox import OutboxWakeupStatus

pytestmark = pytest.mark.integration


async def test_outbox_listener_receives_committed_insert_but_not_rolled_back_insert(
    db: None,
) -> None:
    """A statement trigger publishes only rows that commit, leaving rollback recovery to polling (ADR-009)."""
    listener = PostgresOutboxWakeup(get_settings().database_url)
    topic = f"test.outbox-wakeup.{uuid4().hex}"
    try:
        assert (await listener.start()).status is OutboxWakeupStatus.LISTENING

        async with system_session_scope() as session:
            await session.execute(
                text(
                    """INSERT INTO outbox (tenant_id, topic, payload)
                       VALUES (:tenant_id, :topic, CAST(:payload AS jsonb))"""
                ),
                {
                    "tenant_id": uuid4(),
                    "topic": topic,
                    "payload": json.dumps({"fixture": "committed"}),
                },
            )
        assert (await listener.wait(1.0)).status is OutboxWakeupStatus.NOTIFIED

        async with get_engine().connect() as connection:
            await connection.execute(
                text(
                    """INSERT INTO outbox (tenant_id, topic, payload)
                       VALUES (:tenant_id, :topic, CAST(:payload AS jsonb))"""
                ),
                {
                    "tenant_id": uuid4(),
                    "topic": topic,
                    "payload": json.dumps({"fixture": "rolled-back"}),
                },
            )
            await connection.rollback()
        assert (await listener.wait(0.1)).status is OutboxWakeupStatus.TIMED_OUT
    finally:
        await listener.aclose()
        async with system_session_scope() as session:
            await session.execute(text("DELETE FROM outbox WHERE topic = :topic"), {"topic": topic})
