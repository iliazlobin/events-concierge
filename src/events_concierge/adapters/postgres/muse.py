"""Durable Muse batches, hashed credentials and fenced attempt ownership."""

import hmac
import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.muse import (
    MuseConflictError,
    MuseConnection,
    MuseNotFoundError,
    SignupBatch,
    SignupEvent,
    SignupItem,
    SignupOutcome,
)
from ...infra.db import tenant_session_scope
from ...ports.auth import AuthenticationFailedError


async def _account_lock(session: AsyncSession, tenant_id: UUID) -> None:
    # Same lock as erasure: account deletion cannot race new connector state.
    await session.execute(
        text("""
        SELECT pg_advisory_xact_lock(hashtextextended('account-erasure:' || :tenant, 0))
    """),
        {"tenant": str(tenant_id)},
    )
    active = (
        await session.execute(
            text("""
        SELECT EXISTS (SELECT 1 FROM public.tenants WHERE tenant_id = :tenant_id)
        AND NOT EXISTS (SELECT 1 FROM public.account_erasure_requests WHERE tenant_id = :tenant_id)
    """),
            {"tenant_id": tenant_id},
        )
    ).scalar_one()
    if not active:
        raise AuthenticationFailedError("account unavailable")


async def _batch(session: AsyncSession, tenant_id: UUID, batch_id: UUID) -> SignupBatch | None:
    row = (
        (
            await session.execute(
                text("""
        SELECT batch_id, request_id, created_at FROM public.muse_signup_batches
        WHERE tenant_id = :tenant_id AND batch_id = :batch_id
    """),
                {"tenant_id": tenant_id, "batch_id": batch_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    items = (
        (
            await session.execute(
                text("""
        SELECT event, status, attempt_id, outcome, updated_at FROM public.muse_signup_items
        WHERE tenant_id = :tenant_id AND batch_id = :batch_id
        ORDER BY event->>'start_at', canonical_event_id
    """),
                {"tenant_id": tenant_id, "batch_id": batch_id},
            )
        )
        .mappings()
        .all()
    )
    return SignupBatch(**dict(row), items=[SignupItem.model_validate(dict(item)) for item in items])


async def _item(
    session: AsyncSession,
    tenant_id: UUID,
    batch_id: UUID,
    event_id: UUID,
) -> SignupItem:
    row = (
        (
            await session.execute(
                text("""
        SELECT event, status, attempt_id, outcome, updated_at FROM public.muse_signup_items
        WHERE tenant_id = :tenant_id AND batch_id = :batch_id AND canonical_event_id = :event_id
        FOR UPDATE
    """),
                {"tenant_id": tenant_id, "batch_id": batch_id, "event_id": event_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise MuseNotFoundError("signup item not found")
    return SignupItem.model_validate(dict(row))


class PostgresMuseRepository:
    async def connection(self, tenant_id: UUID) -> MuseConnection:
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            row = (
                (
                    await session.execute(
                        text("""
                SELECT expires_at FROM public.muse_connections
                WHERE tenant_id = :tenant_id AND revoked_at IS NULL AND expires_at > clock_timestamp()
            """),
                        {"tenant_id": tenant_id},
                    )
                )
                .mappings()
                .one_or_none()
            )
            return MuseConnection(
                connected=row is not None, expires_at=row["expires_at"] if row is not None else None
            )

    async def connect(self, tenant_id: UUID, digest: str, expires_at: datetime) -> MuseConnection:
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            await session.execute(
                text("""
                INSERT INTO public.muse_connections (tenant_id, token_hash, expires_at)
                VALUES (:tenant_id, :digest, :expires_at)
                ON CONFLICT (tenant_id) DO UPDATE SET token_hash = EXCLUDED.token_hash,
                    expires_at = EXCLUDED.expires_at, revoked_at = NULL
            """),
                {"tenant_id": tenant_id, "digest": digest, "expires_at": expires_at},
            )
        return MuseConnection(connected=True, expires_at=expires_at)

    async def revoke(self, tenant_id: UUID) -> None:
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            await session.execute(
                text("""
                UPDATE public.muse_connections SET revoked_at = COALESCE(revoked_at, clock_timestamp())
                WHERE tenant_id = :tenant_id
            """),
                {"tenant_id": tenant_id},
            )

    async def authenticate(self, tenant_id: UUID, digest: str) -> bool:
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            expected = (
                await session.execute(
                    text("""
                SELECT token_hash FROM public.muse_connections WHERE tenant_id = :tenant_id
                AND revoked_at IS NULL AND expires_at > clock_timestamp()
            """),
                    {"tenant_id": tenant_id},
                )
            ).scalar_one_or_none()
        return expected is not None and hmac.compare_digest(expected, digest)

    async def by_request(self, tenant_id: UUID, request_id: UUID) -> SignupBatch | None:
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            batch_id = (
                await session.execute(
                    text("""
                SELECT batch_id FROM public.muse_signup_batches
                WHERE tenant_id = :tenant_id AND request_id = :request_id
            """),
                    {"tenant_id": tenant_id, "request_id": request_id},
                )
            ).scalar_one_or_none()
            return await _batch(session, tenant_id, batch_id) if batch_id is not None else None

    async def create(
        self,
        tenant_id: UUID,
        request_id: UUID,
        events: list[SignupEvent],
    ) -> SignupBatch:
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            existing_id = (
                await session.execute(
                    text("""
                SELECT batch_id FROM public.muse_signup_batches
                WHERE tenant_id = :tenant_id AND request_id = :request_id
            """),
                    {"tenant_id": tenant_id, "request_id": request_id},
                )
            ).scalar_one_or_none()
            if existing_id is not None:
                existing = await _batch(session, tenant_id, existing_id)
                assert existing is not None
                if {i.event.canonical_event_id for i in existing.items} != {
                    e.canonical_event_id for e in events
                }:
                    raise MuseConflictError("this request already has a different selection")
                return existing
            duplicate = (
                await session.execute(
                    text("""
                SELECT batch_id FROM public.muse_signup_items WHERE tenant_id = :tenant_id
                AND canonical_event_id = ANY(CAST(:event_ids AS uuid[])) LIMIT 1
            """),
                    {
                        "tenant_id": tenant_id,
                        "event_ids": [event.canonical_event_id for event in events],
                    },
                )
            ).scalar_one_or_none()
            if duplicate is not None:
                raise MuseConflictError(
                    "an event is already in a signup batch; open its existing batch"
                )
            batch_id = uuid4()
            await session.execute(
                text("""
                INSERT INTO public.muse_signup_batches (tenant_id, batch_id, request_id)
                VALUES (:tenant_id, :batch_id, :request_id)
            """),
                {"tenant_id": tenant_id, "batch_id": batch_id, "request_id": request_id},
            )
            for event in events:
                await session.execute(
                    text("""
                    INSERT INTO public.muse_signup_items (tenant_id, batch_id, canonical_event_id, event)
                    VALUES (:tenant_id, :batch_id, :event_id, CAST(:event AS jsonb))
                """),
                    {
                        "tenant_id": tenant_id,
                        "batch_id": batch_id,
                        "event_id": event.canonical_event_id,
                        "event": event.model_dump_json(),
                    },
                )
            created = await _batch(session, tenant_id, batch_id)
            assert created is not None
            return created

    async def batches(self, tenant_id: UUID) -> list[SignupBatch]:
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            ids = (
                (
                    await session.execute(
                        text("""
                SELECT batch_id FROM public.muse_signup_batches WHERE tenant_id = :tenant_id
                ORDER BY created_at DESC LIMIT 50
            """),
                        {"tenant_id": tenant_id},
                    )
                )
                .scalars()
                .all()
            )
            result = []
            for batch_id in ids:
                batch = await _batch(session, tenant_id, batch_id)
                if batch is not None:
                    result.append(batch)
            return result

    async def batch(self, tenant_id: UUID, batch_id: UUID) -> SignupBatch | None:
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            return await _batch(session, tenant_id, batch_id)

    async def claim(
        self,
        tenant_id: UUID,
        batch_id: UUID,
        event_id: UUID,
        attempt_id: UUID,
    ) -> SignupItem:
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            item = await _item(session, tenant_id, batch_id, event_id)
            if item.attempt_id == attempt_id:
                return item
            if item.status != "queued":
                raise MuseConflictError(
                    "another attempt owns this signup; check its provider status"
                )
            await session.execute(
                text("""
                UPDATE public.muse_signup_items SET status = 'in_progress',
                    attempt_id = :attempt_id, updated_at = clock_timestamp()
                WHERE tenant_id = :tenant_id AND batch_id = :batch_id AND canonical_event_id = :event_id
            """),
                {
                    "tenant_id": tenant_id,
                    "batch_id": batch_id,
                    "event_id": event_id,
                    "attempt_id": attempt_id,
                },
            )
            return await _item(session, tenant_id, batch_id, event_id)

    async def report(
        self,
        tenant_id: UUID,
        batch_id: UUID,
        event_id: UUID,
        attempt_id: UUID,
        outcome: SignupOutcome,
    ) -> SignupItem:
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            item = await _item(session, tenant_id, batch_id, event_id)
            if item.attempt_id != attempt_id:
                raise MuseConflictError("result does not belong to the owning signup attempt")
            if item.outcome == outcome:
                return item
            if item.status in {"registered", "failed"}:
                raise MuseConflictError("a completed signup result cannot be replaced")
            values: dict[str, Any] = {
                "tenant_id": tenant_id,
                "batch_id": batch_id,
                "event_id": event_id,
                "status": outcome.status,
                "outcome": json.dumps(outcome.model_dump(mode="json")),
            }
            await session.execute(
                text("""
                UPDATE public.muse_signup_items SET status = :status,
                    outcome = CAST(:outcome AS jsonb), updated_at = clock_timestamp()
                WHERE tenant_id = :tenant_id AND batch_id = :batch_id AND canonical_event_id = :event_id
            """),
                values,
            )
            return await _item(session, tenant_id, batch_id, event_id)
