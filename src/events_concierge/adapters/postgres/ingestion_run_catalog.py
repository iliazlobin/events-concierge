"""Bounded operator reads of current event attribution for an exact source/run."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.events import EventEntityProfile
from ...domain.ingestion_admin import (
    IngestionCatalogEvent,
    IngestionCatalogEventPage,
    IngestionCatalogQualityIssue,
)


def _event(row: dict[str, Any]) -> IngestionCatalogEvent:
    item = dict(row)
    item["canonical_event_id"] = UUID(item["canonical_event_id"])
    for key in ("start_at", "end_at", "last_seen_at"):
        item[key] = datetime.fromisoformat(item[key]) if item[key] is not None else None
    item["quality_issues"] = tuple(
        IngestionCatalogQualityIssue(value) for value in item["quality_issues"]
    )
    for key in ("host_names", "speaker_names", "partner_names"):
        item[key] = tuple(item[key])
    item["entity_profiles"] = tuple(
        EventEntityProfile(**value) for value in item["entity_profiles"]
    )
    return IngestionCatalogEvent(**item)


async def browse_run_catalog(
    session_scope: Callable[[], AbstractAsyncContextManager[AsyncSession]],
    source_key: str,
    run_key: str,
    *,
    query: str | None,
    after_start_at: datetime | None,
    after_canonical_event_id: UUID | None,
    limit: int,
) -> IngestionCatalogEventPage:
    async with session_scope() as session:
        data = (
            await session.execute(
                text(
                    "SELECT public.fn_get_operator_run_catalog_records_v1(:source,:run,:query,:after,:after_id,:limit)"
                ),
                {
                    "source": source_key,
                    "run": run_key,
                    "query": query,
                    "after": after_start_at,
                    "after_id": after_canonical_event_id,
                    "limit": limit,
                },
            )
        ).scalar_one()
    return IngestionCatalogEventPage(
        items=tuple(_event(row) for row in data["items"]),
        source_total=int(data["source_total"]),
        limit=int(data["limit"]),
        has_more=bool(data["has_more"]),
        next_start_at=datetime.fromisoformat(data["next_start_at"])
        if data["next_start_at"]
        else None,
        next_canonical_event_id=UUID(data["next_canonical_event_id"])
        if data["next_canonical_event_id"]
        else None,
        query=data["query"],
        run_key=data["run_key"],
    )
