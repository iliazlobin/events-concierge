"""Restricted SQL functions; the app cannot read history or change budget policy."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain.model_usage import ModelCallUsage
from ...infra.db import system_session_scope


class PostgresModelUsageStore:
    def __init__(
        self,
        session_scope: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ] = system_session_scope,
    ) -> None:
        self._scope = session_scope

    async def _call(self, sql: str, params: dict[str, Any]) -> Any:
        async with self._scope() as session:
            return (await session.execute(text(sql), params)).scalar_one()

    async def begin_call(self, call_id: UUID, requested_model: str) -> str:
        return str(
            await self._call(
                "SELECT public.fn_begin_model_call_v1(:id, :model)",
                {"id": call_id, "model": requested_model},
            )
        )

    async def finish_call(self, call_id: UUID, usage: ModelCallUsage) -> None:
        await self._call(
            "SELECT public.fn_finish_model_call_v1(:id, CAST(:usage AS jsonb))",
            {"id": call_id, "usage": json.dumps(usage.as_json())},
        )

    async def report(
        self, start_at: datetime, end_at: datetime, bucket_hours: int, model: str | None
    ) -> dict[str, Any]:
        return cast(
            "dict[str, Any]",
            await self._call(
                "SELECT public.fn_model_usage_report_v1(:start, :end, :hours, :model)",
                {"start": start_at, "end": end_at, "hours": bucket_hours, "model": model},
            ),
        )

    async def budget(self) -> dict[str, Any]:
        return cast("dict[str, Any]", await self._call("SELECT public.fn_model_budget_v1()", {}))

    async def update_budget(self, settings: dict[str, Any], actor: str) -> dict[str, Any] | None:
        return cast(
            "dict[str, Any] | None",
            await self._call(
                "SELECT public.fn_update_model_budget_v1(CAST(:settings AS jsonb), :actor)",
                {"settings": json.dumps(settings), "actor": actor},
            ),
        )
