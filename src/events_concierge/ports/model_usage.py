"""Durable, application-wide model accounting and budget admission."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from ..domain.model_usage import ModelCallUsage


class ModelUsageLedger(Protocol):
    async def begin_call(self, call_id: UUID, requested_model: str) -> str: ...

    async def finish_call(self, call_id: UUID, usage: ModelCallUsage) -> None: ...


class ModelUsageStore(ModelUsageLedger, Protocol):
    async def report(
        self, start_at: datetime, end_at: datetime, bucket_hours: int, model: str | None
    ) -> dict[str, Any]: ...

    async def budget(self) -> dict[str, Any]: ...

    async def update_budget(
        self, settings: dict[str, Any], actor: str
    ) -> dict[str, Any] | None: ...
