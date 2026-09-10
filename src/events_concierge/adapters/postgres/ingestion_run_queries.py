"""Operator-only global run queries; return the existing normalized rich run contract."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.ingestion_admin import IngestionRunPage, IngestionRunStatus
from ...ports.ingestion_admin import IngestionExecutionDescriptorRegistry

SessionScope = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def decode_run_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Decode only known top-level PostgreSQL JSON timestamps and opaque identities."""
    for key, value in row.items():
        if key.endswith("_at") and isinstance(value, str):
            row[key] = datetime.fromisoformat(value)
    if row.get("command_id") is not None and not isinstance(row["command_id"], UUID):
        row["command_id"] = UUID(row["command_id"])
    return row


class OperatorRunQueries:
    def __init__(
        self,
        scope: SessionScope,
        descriptors: IngestionExecutionDescriptorRegistry | None,
        mapper: Callable[
            [Mapping[str, object], IngestionExecutionDescriptorRegistry | None], IngestionRunStatus
        ],
    ) -> None:
        self._mapper = mapper
        self._scope = scope
        self._descriptors = descriptors

    async def query(self, parameters: dict[str, object]) -> IngestionRunPage:
        payload = {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in parameters.items()
        }
        async with self._scope() as session:
            result = (
                await session.execute(
                    text("SELECT public.fn_query_ingestion_admin_runs_v1(CAST(:p AS jsonb))"),
                    {"p": json.dumps(payload)},
                )
            ).scalar_one()
        return IngestionRunPage(
            tuple(self._run(row) for row in result["items"]),
            result["total"],
            result["limit"],
            result["offset"],
        )

    async def lookup(
        self, source_key: str, run_key: str, include_fixtures: bool
    ) -> IngestionRunStatus | None:
        async with self._scope() as session:
            row = (
                await session.execute(
                    text("SELECT public.fn_lookup_ingestion_admin_run_v1(:source,:run,:fixtures)"),
                    {"source": source_key, "run": run_key, "fixtures": include_fixtures},
                )
            ).scalar_one()
        return self._run(row) if row is not None else None

    def _run(self, row: dict[str, Any]) -> IngestionRunStatus:
        # Shared mapper preserves normalized error codes, timing scopes and missing provenance.
        decode_run_fields(row)
        result = self._mapper(row, self._descriptors)
        if result.command is not None and self._descriptors is not None:
            # Command-owned direct tasks execute in the command worker; true trigger is retained.
            result = replace(
                result,
                execution=self._descriptors.describe(
                    source_key=result.source_key,
                    mode=row.get("mode"),
                    page_limit=row.get("page_limit"),
                    trigger="admin_source",
                ),
            )
        return result
