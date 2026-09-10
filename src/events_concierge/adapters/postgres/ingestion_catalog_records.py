"""Bounded operator inventory with exact filtered totals and public provenance."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import fields
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.ingestion_admin import (
    IngestionCatalogRecord,
    IngestionCatalogRecordPage,
    IngestionCatalogSourceCount,
)
from .ingestion_run_catalog import _event


def _record(row: dict[str, Any]) -> IngestionCatalogRecord:
    values = dict(row)
    source_key = values.pop("source_key")
    source_display_name = values.pop("source_display_name")
    event = _event(values)
    return IngestionCatalogRecord(
        **{field.name: getattr(event, field.name) for field in fields(event)},
        source_key=source_key,
        source_display_name=source_display_name,
    )


async def browse_catalog_records(
    session_scope: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    *,
    source_key: str | None,
    run_key: str | None,
    query: str | None,
    date_scope: str,
    price_status: str,
    after_start_at: datetime | None,
    after_canonical_event_id: UUID | None,
    limit: int,
) -> IngestionCatalogRecordPage:
    async with session_scope() as session:
        data = (
            await session.execute(
                text(
                    "SELECT public.fn_get_operator_catalog_records_v1(:source,:run,:query,:date_scope,:price_status,:after,:after_id,:limit)"
                ),
                {
                    "source": source_key,
                    "run": run_key,
                    "query": query,
                    "date_scope": date_scope,
                    "price_status": price_status,
                    "after": after_start_at,
                    "after_id": after_canonical_event_id,
                    "limit": limit,
                },
            )
        ).scalar_one()
    return IngestionCatalogRecordPage(
        generated_at=datetime.fromisoformat(data["generated_at"]),
        items=tuple(_record(row) for row in data["items"]),
        total=int(data["total"]),
        limit=int(data["limit"]),
        has_more=bool(data["has_more"]),
        next_start_at=datetime.fromisoformat(data["next_start_at"])
        if data["next_start_at"]
        else None,
        next_canonical_event_id=UUID(data["next_canonical_event_id"])
        if data["next_canonical_event_id"]
        else None,
        query=data["query"],
        source_key=data["source_key"],
        run_key=data["run_key"],
        date_scope=data["date_scope"],
        price_status=data["price_status"],
        upcoming_total=int(data["upcoming_total"]),
        source_counts=tuple(IngestionCatalogSourceCount(**row) for row in data["source_counts"]),
        source_count=int(data["source_count"]),
        source_counts_truncated=bool(data["source_counts_truncated"]),
    )
