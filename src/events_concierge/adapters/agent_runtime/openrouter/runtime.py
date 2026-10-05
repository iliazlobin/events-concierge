"""A plain tool-use loop over OpenRouter's OpenAI-compatible endpoint.

Deliberately a direct HTTP loop rather than an agent framework. It is small enough to read in
one sitting, adds no dependencies, and -- because it sits behind ``AgentRuntimePort`` -- it is
also the control against which a framework adapter has to justify itself. If a framework cannot
beat this on trajectory accuracy, tool-schema fidelity, and containment, it is not paying rent.

Two guards matter more than they look:

* ``max_model_calls`` bounds the loop. Frameworks default this into the hundreds; a runaway
  tool loop against a paid endpoint is the expensive failure mode here.
* A tool never raises past ``ConciergeToolset.invoke``. An exception inside a tool would
  otherwise kill the stream mid-turn, and the user would see a truncated answer rather than an
  honest one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import httpx

from ....agent.blocks import blocks_for_tool_result, dedupe_blocks
from ....domain.model_usage import ModelCallUsage, ModelUsageError, response_usage
from ....ports.agent import (
    AgentBlock,
    AgentTurnEvent,
    ConciergeToolset,
    ProseBlock,
    TurnBlock,
    TurnDelta,
    TurnEnd,
    TurnError,
    TurnToolEnd,
    TurnToolStart,
)
from ....ports.model_usage import ModelUsageLedger

_log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
_USAGE_RECEIPT_TIMEOUT_SECONDS = 10.0


class OpenRouterAgentRuntime:
    """AgentRuntimePort over OpenRouter chat completions."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        fallback_model: str | None = None,
        base_url: str = DEFAULT_BASE_URL,
        max_model_calls: int = 8,
        max_tool_calls: int = 8,
        timeout_seconds: float = 60.0,
        client: httpx.AsyncClient | None = None,
        app_title: str = "events-concierge",
        usage_ledger: ModelUsageLedger,
    ) -> None:
        if not api_key.strip():
            raise ValueError("OpenRouter API key must not be empty")
        if not model.strip():
            raise ValueError("OpenRouter model must not be empty")
        self._api_key = api_key
        self._model = model
        self._fallback_model = fallback_model
        self._base_url = base_url.rstrip("/")
        self._max_model_calls = max_model_calls
        self._max_tool_calls = max_tool_calls
        self._timeout = timeout_seconds
        self._client = client
        self._app_title = app_title
        self._usage_ledger = usage_ledger

    def _tool_schemas(self, toolset: ConciergeToolset) -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": dict(spec.json_schema),
                },
            }
            for spec in toolset.describe()
        ]

    async def _complete(
        self,
        client: httpx.AsyncClient,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "temperature": 0,
            "usage": {"include": True},
            # Pin routing and forbid training/logging collection. OpenRouter is a router: without
            # this the request may land with whichever upstream is cheapest, including tiers whose
            # policy permits retaining prompts.
            "provider": {"data_collection": "deny", "allow_fallbacks": True},
        }
        if self._fallback_model:
            body["models"] = [self._model, self._fallback_model]
        call_id = uuid4()
        try:
            admission = await self._usage_ledger.begin_call(call_id, self._model)
        except Exception as error:
            raise ModelUsageError("model_accounting_unavailable") from error
        if admission != "allowed":
            raise ModelUsageError(admission)
        try:
            response = await client.post(
                f"{self._base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self._api_key}", "X-Title": self._app_title},
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload: dict[str, Any] = response.json()
            if not isinstance(payload, dict):
                raise ValueError("invalid model response")
            measured = response_usage(payload)
        except asyncio.CancelledError:
            try:
                await self._finish_usage(
                    call_id, ModelCallUsage(status="interrupted", error_code="cancelled")
                )
            except Exception:
                _log.warning("agent_cancelled_usage_pending")
            raise
        except (httpx.HTTPError, ValueError) as error:
            code = (
                f"http_{error.response.status_code}"
                if isinstance(error, httpx.HTTPStatusError)
                else "request_failed"
            )
            measured = ModelCallUsage(status="failed", error_code=code)
            if isinstance(error, httpx.HTTPStatusError):
                try:
                    error_payload = error.response.json()
                    if isinstance(error_payload, dict):
                        measured = replace(
                            response_usage(error_payload), status="failed", error_code=code
                        )
                except ValueError:
                    pass
            await self._finish_usage(call_id, measured)
            if isinstance(error, ValueError):
                raise ModelUsageError("model_response_invalid") from error
            raise
        await self._finish_usage(call_id, measured)
        if measured.status == "failed":
            raise ModelUsageError("model_response_error")
        return payload

    async def _finish_usage(self, call_id: UUID, measured: ModelCallUsage) -> None:
        async def persist() -> None:
            async with asyncio.timeout(_USAGE_RECEIPT_TIMEOUT_SECONDS):
                await self._usage_ledger.finish_call(call_id, measured)

        # A disconnected client must not roll back a known provider charge. Retain
        # and await the receipt task, including repeated cancellation, until its
        # bounded write finishes; then restore the caller's cancellation.
        receipt = asyncio.create_task(persist())
        cancellation: asyncio.CancelledError | None = None
        while not receipt.done():
            try:
                await asyncio.shield(receipt)
            except asyncio.CancelledError as error:
                cancellation = cancellation or error
            except Exception:
                break
        if cancellation is not None:
            try:
                receipt.result()
            except (Exception, asyncio.CancelledError):
                _log.warning("agent_cancelled_usage_pending")
            raise cancellation
        try:
            receipt.result()
        except Exception as error:
            raise ModelUsageError("model_accounting_unavailable") from error

    async def run_turn(
        self,
        *,
        system_prompt: str,
        history: Sequence[Mapping[str, Any]],
        message: str,
        toolset: ConciergeToolset,
    ) -> AsyncIterator[AgentTurnEvent]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *[dict(turn) for turn in history],
            {"role": "user", "content": message},
        ]
        tools = self._tool_schemas(toolset)
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient()
        model_calls = 0
        tool_calls = 0
        input_tokens = 0
        output_tokens = 0
        pending: list[AgentBlock] = []

        try:
            while model_calls < self._max_model_calls:
                try:
                    payload = await self._complete(client, messages, tools)
                except ModelUsageError as exc:
                    messages_by_code = {
                        "budget_exhausted": "The application's model budget is exhausted. Review budgets in admin.",
                        "cost_unknown": "A previous model charge is unknown. Review usage in admin before continuing.",
                    }
                    yield TurnError(
                        code=exc.code,
                        message=messages_by_code.get(
                            exc.code,
                            "Model accounting or the provider response is unavailable. No further model calls were made.",
                        ),
                    )
                    return
                except httpx.HTTPStatusError as exc:
                    detail = exc.response.text[:200]
                    _log.warning(
                        "agent_model_http_error", extra={"status": exc.response.status_code}
                    )
                    yield TurnError(code=f"model_http_{exc.response.status_code}", message=detail)
                    return
                except httpx.HTTPError as exc:
                    yield TurnError(code="model_unreachable", message=str(exc)[:200])
                    return

                model_calls += 1
                measured = response_usage(payload)
                input_tokens += measured.input_tokens or 0
                output_tokens += measured.output_tokens or 0

                choices = payload.get("choices") or []
                if not choices:
                    yield TurnError(code="model_empty_response", message="no choices returned")
                    return
                assistant = choices[0].get("message") or {}
                requested = assistant.get("tool_calls") or []
                text = assistant.get("content") or ""

                if not requested:
                    if text:
                        yield TurnDelta(text=text)
                    blocks: list[AgentBlock] = []
                    if text.strip():
                        blocks.append(ProseBlock(text=text))
                    blocks.extend(dedupe_blocks(pending))
                    for index, block in enumerate(blocks):
                        yield TurnBlock(block=block, index=index)
                    yield TurnEnd(
                        text=text,
                        model_calls=model_calls,
                        tool_calls=tool_calls,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        stop_reason=choices[0].get("finish_reason") or "end_turn",
                        blocks=blocks,
                    )
                    return

                messages.append(
                    {"role": "assistant", "content": text or None, "tool_calls": requested}
                )

                for call in requested:
                    if tool_calls >= self._max_tool_calls:
                        # Refuse rather than silently truncate: the model is told it hit the
                        # budget, so it can summarize what it already has.
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id", ""),
                                "content": json.dumps(
                                    {
                                        "status": "refused",
                                        "hint": "tool-call budget for this turn is exhausted; "
                                        "answer with what you already have",
                                    }
                                ),
                            }
                        )
                        continue

                    function = call.get("function") or {}
                    name = str(function.get("name") or "")
                    try:
                        arguments = json.loads(function.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    if not isinstance(arguments, dict):
                        arguments = {}

                    yield TurnToolStart(
                        call_id=str(call.get("id") or ""), tool=name, arguments=arguments
                    )
                    started = time.monotonic()
                    result = await toolset.invoke(name, arguments)
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    tool_calls += 1

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id", ""),
                            "content": json.dumps(result.as_model_json(), default=str),
                        }
                    )
                    yield TurnToolEnd(
                        call_id=str(call.get("id") or ""),
                        tool=name,
                        status=result.status,
                        duration_ms=elapsed_ms,
                        summary=_summarize(result.as_model_json()),
                    )
                    pending.extend(
                        blocks_for_tool_result(name, result.status, result.payload, result.render)
                    )

            yield TurnError(
                code="model_call_budget_exhausted",
                message=f"turn exceeded {self._max_model_calls} model calls",
            )
        finally:
            if owns_client:
                await client.aclose()


def _summarize(payload: Mapping[str, Any]) -> str:
    """A short, server-authored description of a tool outcome, safe to show a user.

    Server-authored on purpose: rendering model prose or raw tool output into the UI would put
    third-party listing text on the display path.
    """
    if "events" in payload:
        events = payload["events"]
        return f"{len(events)} event{'s' if len(events) != 1 else ''}"
    if "days" in payload:
        return f"{payload.get('total', 0)} across {len(payload['days'])} days"
    return str(payload.get("status", ""))
