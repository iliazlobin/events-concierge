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
    SeenRegistration,
    SignupBatch,
    SignupEvent,
    SignupItem,
    SignupOutcome,
    SignupRegistration,
    SignupRegistrations,
)
from ...infra.db import tenant_session_scope
from ...ports.auth import AuthenticationFailedError

_MAX_PAGE_SIZE = 100
_REGISTRATION_FIELDS = """
    i.event, i.status, i.attempt_id, i.outcome, i.updated_at, i.batch_id,
    b.created_at, i.version, i.version > i.seen_version AS unread
"""


async def _request_batch_id(
    session: AsyncSession, tenant_id: UUID, request_id: UUID
) -> UUID | None:
    batch_id = (
        await session.execute(
            text("""
                SELECT batch_id FROM public.muse_signup_batches
                WHERE tenant_id = :tenant_id AND request_id = :request_id
            """),
            {"tenant_id": tenant_id, "request_id": request_id},
        )
    ).scalar_one_or_none()
    if batch_id is None:
        registration_request = (
            await session.execute(
                text("""
                    SELECT EXISTS (SELECT 1 FROM public.muse_signup_requests
                    WHERE tenant_id = :tenant_id AND request_id = :request_id)
                """),
                {"tenant_id": tenant_id, "request_id": request_id},
            )
        ).scalar_one()
        if registration_request:
            raise MuseConflictError("this request belongs to an individual registration")
    return batch_id


async def _registration(
    session: AsyncSession, tenant_id: UUID, event_id: UUID
) -> SignupRegistration | None:
    row = (
        (
            await session.execute(
                text(f"""
                SELECT {_REGISTRATION_FIELDS} FROM public.muse_signup_items i
                JOIN public.muse_signup_batches b USING (tenant_id, batch_id)
                WHERE i.tenant_id = :tenant_id AND i.canonical_event_id = :event_id
            """),
                {"tenant_id": tenant_id, "event_id": event_id},
            )
        )
        .mappings()
        .one_or_none()
    )
    return SignupRegistration.model_validate(dict(row)) if row is not None else None


async def _check_queue_request(
    session: AsyncSession, tenant_id: UUID, request_id: UUID, event_id: UUID
) -> None:
    original = (
        (
            await session.execute(
                text("""
                SELECT i.canonical_event_id FROM public.muse_signup_batches b
                JOIN public.muse_signup_items i USING (tenant_id, batch_id)
                WHERE b.tenant_id = :tenant_id AND b.request_id = :request_id
                UNION
                SELECT canonical_event_id FROM public.muse_signup_requests
                WHERE tenant_id = :tenant_id AND request_id = :request_id
            """),
                {"tenant_id": tenant_id, "request_id": request_id},
            )
        )
        .scalars()
        .all()
    )
    if original and set(original) != {event_id}:
        raise MuseConflictError("this request already has a different selection")


async def _insert_batch(
    session: AsyncSession, tenant_id: UUID, request_id: UUID, events: list[SignupEvent]
) -> UUID:
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
    return batch_id


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
            batch_id = await _request_batch_id(session, tenant_id, request_id)
            return await _batch(session, tenant_id, batch_id) if batch_id is not None else None

    async def create(
        self,
        tenant_id: UUID,
        request_id: UUID,
        events: list[SignupEvent],
    ) -> SignupBatch:
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            existing_id = await _request_batch_id(session, tenant_id, request_id)
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
            batch_id = await _insert_batch(session, tenant_id, request_id, events)
            created = await _batch(session, tenant_id, batch_id)
            assert created is not None
            return created

    async def batches(
        self, tenant_id: UUID, limit: int = 50, cursor: UUID | None = None
    ) -> list[SignupBatch]:
        if not 1 <= limit <= _MAX_PAGE_SIZE:
            raise ValueError("page size must be between 1 and 100")
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            cursor_created = None
            if cursor is not None:
                cursor_created = (
                    await session.execute(
                        text("""
                            SELECT created_at FROM public.muse_signup_batches
                            WHERE tenant_id = :tenant_id AND batch_id = :cursor
                        """),
                        {"tenant_id": tenant_id, "cursor": cursor},
                    )
                ).scalar_one_or_none()
                if cursor_created is None:
                    raise MuseNotFoundError("signup batch cursor not found")
            ids = (
                (
                    await session.execute(
                        text("""
                SELECT batch_id FROM public.muse_signup_batches WHERE tenant_id = :tenant_id
                AND (CAST(:cursor AS uuid) IS NULL OR
                    (created_at, batch_id) < (CAST(:cursor_created AS timestamptz), :cursor))
                ORDER BY created_at DESC, batch_id DESC LIMIT :limit
            """),
                        {
                            "tenant_id": tenant_id,
                            "limit": limit,
                            "cursor": cursor,
                            "cursor_created": cursor_created,
                        },
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

    async def queue_registration(
        self,
        tenant_id: UUID,
        request_id: UUID,
        event_id: UUID,
        event: SignupEvent | None = None,
    ) -> SignupRegistration:
        if event is not None and event.canonical_event_id != event_id:
            raise ValueError("the selected event does not match the request")
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            await _check_queue_request(session, tenant_id, request_id, event_id)
            registration = await _registration(session, tenant_id, event_id)
            if registration is None:
                if event is None:
                    raise MuseNotFoundError("signup registration not found")
                await _insert_batch(session, tenant_id, request_id, [event])
                registration = await _registration(session, tenant_id, event_id)
                assert registration is not None
            await session.execute(
                text("""
                    INSERT INTO public.muse_signup_requests (tenant_id, request_id, canonical_event_id)
                    VALUES (:tenant_id, :request_id, :event_id)
                    ON CONFLICT (tenant_id, request_id) DO NOTHING
                """),
                {"tenant_id": tenant_id, "request_id": request_id, "event_id": event_id},
            )
            return registration

    async def registrations(
        self, tenant_id: UUID, limit: int = 50, cursor: UUID | None = None
    ) -> SignupRegistrations:
        if not 1 <= limit <= _MAX_PAGE_SIZE:
            raise ValueError("page size must be between 1 and 100")
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            cursor_item = await _registration(session, tenant_id, cursor) if cursor else None
            if cursor is not None and cursor_item is None:
                raise MuseNotFoundError("signup registration cursor not found")
            totals = (
                (
                    await session.execute(
                        text("""
                        SELECT count(*) AS total,
                            count(*) FILTER (WHERE version > seen_version) AS unread_count
                        FROM public.muse_signup_items WHERE tenant_id = :tenant_id
                    """),
                        {"tenant_id": tenant_id},
                    )
                )
                .mappings()
                .one()
            )
            rows = (
                (
                    await session.execute(
                        text(f"""
                        SELECT {_REGISTRATION_FIELDS} FROM public.muse_signup_items i
                        JOIN public.muse_signup_batches b USING (tenant_id, batch_id)
                        WHERE i.tenant_id = :tenant_id
                        AND (CAST(:cursor AS uuid) IS NULL OR
                            (b.created_at, i.canonical_event_id) <
                            (CAST(:cursor_created AS timestamptz), :cursor))
                        ORDER BY b.created_at DESC, i.canonical_event_id DESC LIMIT :limit
                    """),
                        {
                            "tenant_id": tenant_id,
                            "cursor": cursor,
                            "limit": limit + 1,
                            "cursor_created": cursor_item.created_at if cursor_item else None,
                        },
                    )
                )
                .mappings()
                .all()
            )
            items = [SignupRegistration.model_validate(dict(row)) for row in rows[:limit]]
            return SignupRegistrations(
                items=items,
                total=totals["total"],
                unread_count=totals["unread_count"],
                next_cursor=items[-1].event.canonical_event_id if len(rows) > limit else None,
            )

    async def see_registrations(self, tenant_id: UUID, items: list[SeenRegistration]) -> None:
        if len(items) > _MAX_PAGE_SIZE or len({item.event_id for item in items}) != len(items):
            raise ValueError("acknowledge at most 100 distinct registrations")
        async with tenant_session_scope(tenant_id) as session:
            await _account_lock(session, tenant_id)
            rows = (
                (
                    await session.execute(
                        text("""
                        SELECT canonical_event_id, version FROM public.muse_signup_items
                        WHERE tenant_id = :tenant_id AND
                            canonical_event_id = ANY(CAST(:event_ids AS uuid[]))
                        FOR UPDATE
                    """),
                        {"tenant_id": tenant_id, "event_ids": [item.event_id for item in items]},
                    )
                )
                .mappings()
                .all()
            )
            versions: dict[UUID, int] = {row["canonical_event_id"]: row["version"] for row in rows}
            if len(versions) != len(items):
                raise MuseNotFoundError("signup registration not found")
            if any(item.version > versions[item.event_id] for item in items):
                raise ValueError("cannot acknowledge a registration version that has not occurred")
            for item in items:
                await session.execute(
                    text("""
                        UPDATE public.muse_signup_items SET seen_version = GREATEST(seen_version, :version)
                        WHERE tenant_id = :tenant_id AND canonical_event_id = :event_id
                    """),
                    {"tenant_id": tenant_id, "event_id": item.event_id, "version": item.version},
                )

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
                    attempt_id = :attempt_id, updated_at = clock_timestamp(), version = version + 1
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
                    outcome = CAST(:outcome AS jsonb), updated_at = clock_timestamp(),
                    version = version + 1
                WHERE tenant_id = :tenant_id AND batch_id = :batch_id AND canonical_event_id = :event_id
            """),
                values,
            )
            return await _item(session, tenant_id, batch_id, event_id)
