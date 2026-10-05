"""Paid request admission and provider-reported billing, without provider traffic."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any
from uuid import UUID

import httpx
import pytest

from events_concierge.adapters.agent_runtime.openrouter import runtime as runtime_module
from events_concierge.adapters.agent_runtime.openrouter.key_usage import OpenRouterKeyUsage
from events_concierge.adapters.agent_runtime.openrouter.runtime import OpenRouterAgentRuntime
from events_concierge.agent._devkit import repl
from events_concierge.domain.model_usage import (
    ModelCallUsage,
    ModelUsageError,
    reported_money,
    response_usage,
)
from events_concierge.ports.agent import ToolResult, ToolSpec, TurnEnd, TurnError


class Ledger:
    def __init__(self, admission: str = "allowed", *, unavailable: bool = False) -> None:
        self.admission = admission
        self.unavailable = unavailable
        self.started: list[UUID] = []
        self.finished: list[tuple[UUID, ModelCallUsage]] = []

    async def begin_call(self, call_id: UUID, requested_model: str) -> str:
        if self.unavailable:
            raise RuntimeError("database private details")
        self.started.append(call_id)
        return self.admission

    async def finish_call(self, call_id: UUID, usage: ModelCallUsage) -> None:
        self.finished.append((call_id, usage))


class Tools:
    def describe(self) -> list[ToolSpec]:
        return []

    async def invoke(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        return ToolResult(status="empty")


class WaitingLedger(Ledger):
    def __init__(self) -> None:
        super().__init__()
        self.writing = asyncio.Event()
        self.release = asyncio.Event()
        self.write_cancelled = False

    async def finish_call(self, call_id: UUID, usage: ModelCallUsage) -> None:
        self.writing.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.write_cancelled = True
            raise
        await super().finish_call(call_id, usage)


def completion(**overrides: Any) -> dict[str, Any]:
    return {
        "id": "gen-example",
        "model": "fallback/model",
        "choices": [{"message": {"content": "Answer"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": 30,
            "completion_tokens": 10,
            "cost": 0.0025,
            "prompt_tokens_details": {"cached_tokens": 20},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
        **overrides,
    }


@pytest.mark.parametrize("value", [None, True, -1, "NaN", "Infinity", {}, "bad", 1_000_001])
def test_invalid_cost_is_unknown(value: object) -> None:
    assert reported_money(value) is None


def test_explicit_zero_and_actual_fallback_are_preserved_without_text() -> None:
    payload = completion(usage={"cost": 0, "prompt_tokens": 0, "completion_tokens": 0})
    measured = response_usage(payload)
    assert measured.cost_usd == Decimal(0)
    assert measured.actual_model == "fallback/model"
    assert measured.input_tokens == 0
    assert "Answer" not in json.dumps(measured.as_json())
    assert response_usage(completion(usage={})).cost_usd is None
    assert response_usage(completion(model="invalid\nmodel")).actual_model is None


@pytest.mark.parametrize(
    "choice", [{"error": {"code": 500}}, {"message": {}, "finish_reason": "error"}, {}]
)
def test_200_errors_keep_any_billed_usage(choice: dict[str, Any]) -> None:
    measured = response_usage(completion(choices=[choice]))
    assert measured.status == "failed"
    assert measured.cost_usd == Decimal(".0025")


@pytest.mark.parametrize("admission", ["budget_exhausted", "cost_unknown"])
async def test_budget_denial_happens_before_provider_egress(admission: str) -> None:
    ledger = Ledger(admission)
    requests: list[httpx.Request] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(requests.append)) as client:
        runtime = OpenRouterAgentRuntime(
            api_key="fixture-key", model="primary/model", client=client, usage_ledger=ledger
        )
        events = [
            event
            async for event in runtime.run_turn(
                system_prompt="", history=[], message="hello", toolset=Tools()
            )
        ]
    assert isinstance(events[-1], TurnError) and events[-1].code == admission
    assert not requests and not ledger.finished


async def test_accounting_failure_denies_egress_and_hides_internal_details() -> None:
    ledger = Ledger(unavailable=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("paid egress"))
    ) as client:
        runtime = OpenRouterAgentRuntime(
            api_key="fixture-key", model="primary/model", client=client, usage_ledger=ledger
        )
        events = [
            event
            async for event in runtime.run_turn(
                system_prompt="", history=[], message="hello", toolset=Tools()
            )
        ]
    assert isinstance(events[-1], TurnError) and events[-1].code == "model_accounting_unavailable"
    assert "private" not in events[-1].message


async def test_development_cli_uses_the_application_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = Ledger("budget_exhausted")
    monkeypatch.setenv("EC_OPENROUTER_API_KEY", "fixture-key")
    monkeypatch.setattr(repl, "PostgresModelUsageStore", lambda: ledger)
    runtime = repl._build_runtime()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: pytest.fail("paid egress"))
    ) as client:
        with pytest.raises(ModelUsageError, match="budget_exhausted"):
            await runtime._complete(client, [], [])
    assert len(ledger.started) == 1 and not ledger.finished


async def test_each_physical_call_in_a_tool_turn_is_recorded_and_requests_live_usage() -> None:
    ledger = Ledger()
    requests: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            return httpx.Response(
                200,
                json=completion(
                    id="gen-tool",
                    choices=[
                        {
                            "message": {
                                "tool_calls": [
                                    {
                                        "id": "tool-1",
                                        "function": {"name": "catalog", "arguments": "{}"},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                ),
            )
        return httpx.Response(200, json=completion(id="gen-final"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        runtime = OpenRouterAgentRuntime(
            api_key="fixture-key",
            model="primary/model",
            fallback_model="fallback/model",
            client=client,
            usage_ledger=ledger,
        )
        events = [
            event
            async for event in runtime.run_turn(
                system_prompt="", history=[], message="hello", toolset=Tools()
            )
        ]
    assert len(ledger.started) == len(ledger.finished) == 2
    assert len(set(ledger.started)) == 2
    assert all(request["usage"] == {"include": True} for request in requests)
    assert requests[0]["models"] == ["primary/model", "fallback/model"]
    assert all(
        usage.actual_model == "fallback/model"
        and usage.cached_tokens == 20
        and usage.reasoning_tokens == 2
        for _, usage in ledger.finished
    )
    assert isinstance(events[-1], TurnEnd) and events[-1].input_tokens == 60


async def test_http_error_preserves_reported_charge_in_failed_request() -> None:
    ledger = Ledger()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(503, json=completion(error={"code": 503}))
        )
    ) as client:
        runtime = OpenRouterAgentRuntime(
            api_key="fixture-key", model="primary/model", usage_ledger=ledger
        )
        with pytest.raises(httpx.HTTPStatusError):
            await runtime._complete(client, [], [])
    assert ledger.finished[0][1].cost_usd == Decimal(".0025")
    assert ledger.finished[0][1].status == "failed"


async def test_cancellation_records_interrupted_unknown_cost() -> None:
    ledger = Ledger()

    async def cancel(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError

    async with httpx.AsyncClient(transport=httpx.MockTransport(cancel)) as client:
        runtime = OpenRouterAgentRuntime(
            api_key="fixture-key", model="primary/model", usage_ledger=ledger
        )
        with pytest.raises(asyncio.CancelledError):
            await runtime._complete(client, [], [])
    assert ledger.finished[0][1].status == "interrupted"
    assert ledger.finished[0][1].cost_usd is None


@pytest.mark.parametrize("status", [200, 503])
async def test_disconnect_during_receipt_write_preserves_known_charge(status: int) -> None:
    ledger = WaitingLedger()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(status, json=completion()))
    ) as client:
        runtime = OpenRouterAgentRuntime(
            api_key="fixture-key", model="primary/model", usage_ledger=ledger
        )
        task = asyncio.create_task(runtime._complete(client, [], []))
        await asyncio.wait_for(ledger.writing.wait(), timeout=1)
        task.cancel("client disconnected")
        await asyncio.sleep(0)
        task.cancel("disconnect repeated")
        await asyncio.sleep(0)
        assert not task.done() and not ledger.write_cancelled
        ledger.release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
    assert len(ledger.finished) == 1
    measured = ledger.finished[0][1]
    assert measured.cost_usd == Decimal(".0025")
    assert measured.actual_model == "fallback/model"
    assert measured.status == ("ok" if status == 200 else "failed")
    assert measured.error_code == (None if status == 200 else "http_503")


@pytest.mark.parametrize("disconnect", [False, True])
async def test_stalled_receipt_write_is_bounded(
    monkeypatch: pytest.MonkeyPatch, disconnect: bool
) -> None:
    ledger = WaitingLedger()
    monkeypatch.setattr(runtime_module, "_USAGE_RECEIPT_TIMEOUT_SECONDS", 0.02)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=completion()))
    ) as client:
        runtime = OpenRouterAgentRuntime(
            api_key="fixture-key", model="primary/model", usage_ledger=ledger
        )
        task = asyncio.create_task(runtime._complete(client, [], []))
        await asyncio.wait_for(ledger.writing.wait(), timeout=1)
        if disconnect:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
        else:
            with pytest.raises(ModelUsageError, match="model_accounting_unavailable"):
                await asyncio.wait_for(task, timeout=1)
    assert ledger.write_cancelled and not ledger.finished


async def test_receipt_failure_stops_the_tool_loop() -> None:
    class FailingLedger(Ledger):
        async def finish_call(self, call_id: UUID, usage: ModelCallUsage) -> None:
            raise RuntimeError("database private details")

    ledger = FailingLedger()
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=completion())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        runtime = OpenRouterAgentRuntime(
            api_key="fixture-key", model="primary/model", usage_ledger=ledger, client=client
        )
        events = [
            event
            async for event in runtime.run_turn(
                system_prompt="", history=[], message="hello", toolset=Tools()
            )
        ]
    assert len(requests) == 1
    assert isinstance(events[-1], TurnError) and events[-1].code == "model_accounting_unavailable"
    assert "private" not in events[-1].message


async def test_provider_key_totals_are_read_only_cached_and_whitelisted() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "usage": 4.25,
                    "usage_daily": 0,
                    "limit": 20,
                    "limit_remaining": 15.75,
                    "label": "secret-key-prefix",
                    "hash": "private-id",
                    "limit_reset": "monthly",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        usage = OpenRouterKeyUsage("fixture-key", client=client)
        first, second = await usage.get(), await usage.get()
    assert first == second and len(requests) == 1
    assert requests[0].method == "GET" and requests[0].url.path == "/api/v1/key"
    assert first["usage_daily"] == "0" and first["usage_monthly"] is None
    assert "secret-key-prefix" not in json.dumps(first) and "private-id" not in json.dumps(first)


async def test_failed_key_read_does_not_invent_zero_balance() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(401))
    ) as client:
        assert (await OpenRouterKeyUsage("fixture-key", client=client).get())[
            "status"
        ] == "unavailable"
    assert (await OpenRouterKeyUsage("").get())["status"] == "not_configured"
