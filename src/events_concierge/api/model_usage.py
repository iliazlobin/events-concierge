"""Model history and budget controls inside the existing operator boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal, cast
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from ..adapters.agent_runtime.openrouter.key_usage import OpenRouterKeyUsage
from ..ports.model_usage import ModelUsageStore
from .admin import (
    LocalIngestionAdmin,
    SameOriginAdminCommand,
    _no_store,
    _request_operator_actor,
)

_HEADERS = {"Cache-Control": "no-store, max-age=0"}


class UsageTotals(BaseModel):
    calls: int
    failed: int
    cost_usd: Decimal
    unknown_cost_calls: int
    input_tokens: int
    output_tokens: int
    latency_ms: float | None


class UsageSummary(UsageTotals):
    pending: int
    unknown_token_calls: int
    cached_tokens: int
    reasoning_tokens: int


class UsageBucket(UsageTotals):
    at: AwareDatetime
    until: AwareDatetime


class UsageByModel(UsageTotals):
    model: str
    cached_tokens: int
    reasoning_tokens: int


class UsageCall(BaseModel):
    call_id: UUID
    started_at: AwareDatetime
    completed_at: AwareDatetime | None
    requested_model: str
    actual_model: str | None
    status: str
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: Decimal | None
    latency_ms: float | None
    error_code: str | None


class UsageReport(BaseModel):
    generated_at: AwareDatetime
    start_at: AwareDatetime
    end_at: AwareDatetime
    bucket_hours: int
    model: str | None
    tracked_since: AwareDatetime | None
    model_options: list[str]
    totals: UsageSummary
    series: list[UsageBucket]
    models: list[UsageByModel]
    recent: list[UsageCall]


class ModelBudget(BaseModel):
    revision: int
    mode: Literal["warn", "enforce"]
    daily_limit_usd: Decimal | None
    monthly_limit_usd: Decimal | None
    daily_used_usd: Decimal
    monthly_used_usd: Decimal
    alert_percent: int
    updated_at: AwareDatetime
    generated_at: AwareDatetime
    day_start: AwareDatetime
    month_start: AwareDatetime
    unknown_calls: int
    pending_calls: int


BudgetAmount = Annotated[Decimal, Field(ge=0, le=100_000, max_digits=14, decimal_places=8)]


class BudgetUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    mode: Literal["warn", "enforce"]
    daily_limit_usd: BudgetAmount | None
    monthly_limit_usd: BudgetAmount | None
    alert_percent: int = Field(ge=1, le=100)


class KeyUsageOut(BaseModel):
    status: Literal["ok", "unavailable", "not_configured"]
    checked_at: AwareDatetime | None
    limit_state: Literal["configured", "unlimited", "unknown"] = "unknown"
    usage: Decimal | None = None
    usage_daily: Decimal | None = None
    usage_weekly: Decimal | None = None
    usage_monthly: Decimal | None = None
    limit: Decimal | None = None
    limit_remaining: Decimal | None = None
    limit_reset: Literal["daily", "weekly", "monthly"] | None = None


def _store(request: Request) -> ModelUsageStore:
    store = getattr(request.app.state, "model_usage", None)
    if store is None:
        raise _unavailable()
    return cast("ModelUsageStore", store)


def _unavailable() -> HTTPException:
    return HTTPException(503, "model usage is unavailable", headers=_HEADERS)


def install_model_usage_routes(app: FastAPI) -> None:
    settings = app.state.settings
    if not getattr(settings, "admin_ingestion_enabled", False) and not getattr(
        app.state, "operator_boundary", False
    ):
        return

    @app.get("/admin/v1/models/usage", response_model=UsageReport, include_in_schema=False)
    async def usage_report(
        request: Request,
        response: Response,
        admin: LocalIngestionAdmin,
        start_at: AwareDatetime,
        end_at: AwareDatetime,
        bucket_hours: int = 24,
        model: Annotated[str | None, Query(max_length=200, pattern=r"^[!-~]{1,200}$")] = None,
    ) -> dict[str, Any]:
        del admin
        _no_store(response)
        span = end_at - start_at
        if (
            bucket_hours not in (1, 24)
            or span <= timedelta(0)
            or span > timedelta(days=90)
            or end_at > datetime.now(UTC) + timedelta(days=1)
            or span.total_seconds() > bucket_hours * 3600 * 120
        ):
            raise HTTPException(
                422, "choose up to 90 days and at most 120 intervals", headers=_HEADERS
            )
        try:
            return await _store(request).report(start_at, end_at, bucket_hours, model)
        except Exception as error:
            raise _unavailable() from error

    @app.get("/admin/v1/models/budget", response_model=ModelBudget, include_in_schema=False)
    async def model_budget(
        request: Request, response: Response, admin: LocalIngestionAdmin
    ) -> dict[str, Any]:
        del admin
        _no_store(response)
        try:
            return await _store(request).budget()
        except Exception as error:
            raise _unavailable() from error

    @app.patch("/admin/v1/models/budget", response_model=ModelBudget, include_in_schema=False)
    async def update_budget(
        request: Request,
        response: Response,
        body: BudgetUpdate,
        admin: LocalIngestionAdmin,
        same_origin: SameOriginAdminCommand,
    ) -> dict[str, Any]:
        del admin, same_origin
        _no_store(response)
        try:
            result = await _store(request).update_budget(
                body.model_dump(mode="json"), _request_operator_actor(request)
            )
        except Exception as error:
            raise _unavailable() from error
        if result is None:
            raise HTTPException(409, "budget changed; refresh before editing", headers=_HEADERS)
        return result

    @app.get("/admin/v1/models/key", response_model=KeyUsageOut, include_in_schema=False)
    async def key_usage(
        request: Request, response: Response, admin: LocalIngestionAdmin
    ) -> dict[str, Any]:
        del admin
        _no_store(response)
        provider = cast(
            "OpenRouterKeyUsage | None", getattr(request.app.state, "openrouter_key_usage", None)
        )
        if provider is None:
            return {"status": "not_configured", "checked_at": None}
        return await provider.get()
