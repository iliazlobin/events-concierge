"""``POST /v1/chat`` -- the conversational surface, as Server-Sent Events.

SSE rather than a WebSocket: ADR-012's CSP is ``connect-src 'self'``, Next route handlers cannot
perform an Upgrade handshake, the proxy strips ``upgrade`` as hop-by-hop, and a turn is
half-duplex anyway. A plain ``fetch`` + reader also keeps real HTTP status codes on the initial
response and gives the client ``AbortController`` for stop-generation.

Exactly one ``end`` or ``error`` frame terminates every stream, so a client can always tell a
finished answer from a truncated one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import asdict, is_dataclass
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..ports.agent import (
    AgentRuntimePort,
    ConciergeToolset,
    TurnBlock,
    TurnEnd,
    TurnError,
    TurnToolEnd,
    TurnToolStart,
)
from .blocks import blocks_for_tool_result, shown_refs
from .prompt import build_system_prompt

_log = logging.getLogger(__name__)

KEEPALIVE_SECONDS = 15.0
MAX_HISTORY_MESSAGES = 16
MAX_QUOTE_CHARS = 600


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)
    conversation_id: str = Field(default="", max_length=64)
    selected_refs: list[str] = Field(default_factory=list, max_length=50)
    """Event cards the user picked out on screen, as an explicit context for this turn."""

    selected_entity_refs: list[str] = Field(default_factory=list, max_length=50)
    """Organizer cards the user picked out."""

    selected_quotes: list[str] = Field(default_factory=list, max_length=20)
    """Passages the user highlighted. Bounded, and treated as words the user chose to repeat."""


def _known_refs(toolset: ConciergeToolset, refs: Sequence[str]) -> list[str]:
    """Keep only refs this conversation minted, preserving the user's order."""
    resolve = getattr(getattr(toolset, "catalog_tools", None), "resolve_ref", None)
    if resolve is None:
        return []
    seen: set[str] = set()
    kept: list[str] = []
    for raw in refs:
        ref = str(raw).strip().upper()
        if ref in seen or resolve(ref) is None:
            continue
        seen.add(ref)
        kept.append(ref)
    return kept


def _known_entity_refs(toolset: ConciergeToolset, refs: Sequence[str]) -> list[str]:
    """Keep only organizer refs this conversation minted."""
    entity_tools = getattr(toolset, "entity_tools", None)
    known = getattr(entity_tools, "_entity_refs", None) if entity_tools else None
    if not isinstance(known, dict):
        return []
    seen: set[str] = set()
    kept: list[str] = []
    for raw in refs:
        ref = str(raw).strip().upper()
        if ref in seen or ref not in known:
            continue
        seen.add(ref)
        kept.append(ref)
    return kept


def _frame(event: str, payload: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, default=str)}\n\n"


def _block_json(block: Any) -> dict[str, Any]:
    return asdict(block) if is_dataclass(block) and not isinstance(block, type) else {}


class ConversationMemory:
    """Per-conversation transcript, visible refs, and toolset, in process.

    Deliberately not a database table yet: nothing here survives a restart, which is the right
    trade while the surface is still changing. The ``ConversationStore`` port is where a durable
    implementation lands without the routes noticing.

    The toolset is per conversation because refs live inside it. One shared instance would let
    "the second one" in one conversation resolve to an event surfaced in another -- and would
    grow a ref table without bound for the life of the process.
    """

    def __init__(self, toolset_factory: Callable[[UUID], ConciergeToolset]) -> None:
        self._toolset_factory = toolset_factory
        self._toolsets: dict[str, ConciergeToolset] = {}
        self._history: dict[str, list[dict[str, Any]]] = {}
        self._shown: dict[str, list[tuple[str, str]]] = {}

    def toolset(self, key: str, tenant_id: UUID) -> ConciergeToolset:
        toolset = self._toolsets.get(key)
        if toolset is None:
            toolset = self._toolset_factory(tenant_id)
            self._toolsets[key] = toolset
        return toolset

    def history(self, key: str) -> list[dict[str, Any]]:
        return self._history.setdefault(key, [])

    def shown(self, key: str) -> list[tuple[str, str]]:
        return self._shown.setdefault(key, [])

    def record(self, key: str, user_text: str, reply: str, refs: Sequence[tuple[str, str]]) -> None:
        history = self.history(key)
        history.append({"role": "user", "content": user_text})
        history.append({"role": "assistant", "content": reply})
        del history[: max(0, len(history) - MAX_HISTORY_MESSAGES)]
        self.shown(key).extend(refs)


def install_agent_routes(
    app: FastAPI,
    *,
    runtime: AgentRuntimePort,
    toolset_factory: Callable[[UUID], ConciergeToolset],
    tenant_resolver: Callable[..., Awaitable[UUID]],
    memory: ConversationMemory | None = None,
) -> None:
    """Mount the chat route. Absent an agent runtime this is never called, and /v1/chat 404s.

    ``toolset_factory`` is deferred rather than a built instance: the container is assembled in
    the lifespan handler, well after routes are declared, so resolving it at import time reads a
    state attribute that does not exist yet.
    """
    router = APIRouter()
    store = memory or ConversationMemory(toolset_factory)

    @router.post("/v1/chat")
    # The resolver is passed in and bound as a DEFAULT rather than reached through the
    # annotation: with postponed annotations a parameter typed by a local variable is just an
    # unresolvable string to FastAPI, which then treats it as a query parameter.
    async def chat(
        body: ChatBody,
        tenant_id: UUID = Depends(tenant_resolver),  # noqa: B008 - FastAPI's dependency idiom
    ) -> StreamingResponse:
        # Saved filters are RLS-scoped, so a conversation is keyed by tenant as well as by id:
        # two tenants sharing a conversation id must never share a toolset or a transcript.
        key = f"{tenant_id}:{body.conversation_id or 'default'}"
        toolset = store.toolset(key, tenant_id)
        # A selection is a claim about what is on the user's screen, so it is filtered against
        # the refs this conversation actually minted rather than trusted as sent.
        selected = _known_refs(toolset, body.selected_refs)
        selected_entities = _known_entity_refs(toolset, body.selected_entity_refs)
        quotes = [
            " ".join(quote.split())[:MAX_QUOTE_CHARS]
            for quote in body.selected_quotes
            if quote.strip()
        ]

        async def stream() -> AsyncIterator[str]:
            queue: asyncio.Queue[str | None] = asyncio.Queue()

            async def produce() -> None:
                reply = ""
                blocks: list[Any] = []
                try:
                    # A selection is resolved BEFORE the model runs, and handed to it as an
                    # already-completed tool call. Asking the model to honour "read exactly these"
                    # was measured at roughly half: it would sometimes search the catalog again
                    # and answer about the whole list instead of the two cards the user picked.
                    # Priming removes the choice -- the turn starts holding the selected events --
                    # and costs one indexed read.
                    primed: list[dict[str, Any]] = []
                    if selected:
                        picked = await toolset.invoke(
                            "get_selected_events", {"refs": ",".join(selected)}
                        )
                        if picked.status == "ok":
                            call_id = "selection-0"
                            primed = [
                                {
                                    "role": "assistant",
                                    "content": None,
                                    "tool_calls": [
                                        {
                                            "id": call_id,
                                            "type": "function",
                                            "function": {
                                                "name": "get_selected_events",
                                                "arguments": json.dumps(
                                                    {"refs": ",".join(selected)}
                                                ),
                                            },
                                        }
                                    ],
                                },
                                {
                                    "role": "tool",
                                    "tool_call_id": call_id,
                                    "content": json.dumps(picked.as_model_json(), default=str),
                                },
                            ]
                            for offset, block in enumerate(
                                blocks_for_tool_result(
                                    "get_selected_events",
                                    picked.status,
                                    picked.payload,
                                    picked.render,
                                )
                            ):
                                blocks.append(block)
                                await queue.put(
                                    _frame(
                                        "block",
                                        {"index": offset, "block": _block_json(block)},
                                    )
                                )
                    async for event in runtime.run_turn(
                        system_prompt=build_system_prompt(
                            shown=store.shown(key),
                            selected=selected,
                            selected_entities=selected_entities,
                            quotes=quotes,
                        ),
                        history=[*store.history(key), *primed],
                        message=body.text,
                        toolset=toolset,
                    ):
                        match event:
                            case TurnToolStart(tool=tool, arguments=arguments):
                                supplied = {
                                    k: v for k, v in arguments.items() if v not in ("", 0, None)
                                }
                                await queue.put(
                                    _frame("tool_start", {"tool": tool, "args": supplied})
                                )
                            case TurnToolEnd(tool=tool, status=status, summary=summary):
                                await queue.put(
                                    _frame(
                                        "tool_end",
                                        {"tool": tool, "status": status, "summary": summary},
                                    )
                                )
                            case TurnBlock(block=block, index=index):
                                # Offset by blocks already emitted for the primed selection, not
                                # by the primed MESSAGE count, or indexes collide or skip.
                                del index
                                await queue.put(
                                    _frame(
                                        "block",
                                        {
                                            "index": len(blocks),
                                            "block": _block_json(block),
                                        },
                                    )
                                )
                                blocks.append(block)
                            case TurnEnd() as end:
                                reply = end.text
                                store.record(key, body.text, reply, shown_refs(end.blocks))
                                await queue.put(
                                    _frame(
                                        "end",
                                        {
                                            "text": end.text,
                                            "tool_calls": end.tool_calls,
                                            "model_calls": end.model_calls,
                                        },
                                    )
                                )
                            case TurnError(code=code, message=detail):
                                await queue.put(_frame("error", {"code": code, "message": detail}))
                            case _:
                                continue
                except Exception as exc:
                    # The turn dies here rather than as a torn stream; the client always sees a
                    # terminal frame and can say honestly that nothing was acted on.
                    _log.exception("agent_turn_failed")
                    await queue.put(
                        _frame(
                            "error",
                            {"code": "turn_failed", "message": type(exc).__name__},
                        )
                    )
                finally:
                    await queue.put(None)

            task = asyncio.create_task(produce())
            yield _frame("start", {"conversation_id": key})
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                    except TimeoutError:
                        # Comment frames keep intermediaries from treating a thinking model as a
                        # dead connection.
                        yield ": ping\n\n"
                        continue
                    if item is None:
                        return
                    yield item
            finally:
                task.cancel()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store, no-transform", "X-Accel-Buffering": "no"},
        )

    app.include_router(router)
