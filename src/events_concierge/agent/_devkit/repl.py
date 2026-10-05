"""Terminal REPL for the concierge agent, against the live catalog.

The smallest possible loop that exercises the real thing: real repository, real catalog rows,
real model, real tools. No HTTP edge, no SSE, no browser. If the agent is wrong, it is wrong
here first, and it is far cheaper to see it here.

    make agent-repl

``--once`` runs a single prompt and exits, which is what the eval harness uses.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from ...adapters.agent_runtime.openrouter import OpenRouterAgentRuntime
from ...adapters.postgres.catalog import PostgresCatalogRepository
from ...adapters.postgres.catalog_entities import PostgresCatalogEntityRepository
from ...adapters.postgres.model_usage import PostgresModelUsageStore
from ...adapters.ranking.embedding import DeterministicEmbedding
from ...config import get_settings
from ...infra.db import init_engine
from ...ports.agent import (
    TurnBlock,
    TurnDelta,
    TurnEnd,
    TurnError,
    TurnToolEnd,
    TurnToolStart,
)
from ..blocks import shown_refs
from ..prompt import build_system_prompt
from ..toolset import ConciergeToolsetImpl

DIM = "\033[2m"
BOLD = "\033[1m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
OFF = "\033[0m"

MAX_HISTORY_TURNS = 8


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _build_runtime() -> OpenRouterAgentRuntime:
    api_key = _env("EC_OPENROUTER_API_KEY")
    if not api_key:
        raise SystemExit(
            "EC_OPENROUTER_API_KEY is not set. Add it to .env (which is gitignored) and re-run."
        )
    return OpenRouterAgentRuntime(
        api_key=api_key,
        model=_env("EC_AGENT_MODEL", "deepseek/deepseek-v4-flash"),
        fallback_model=_env("EC_AGENT_MODEL_FALLBACK") or None,
        max_model_calls=int(_env("EC_AGENT_MAX_MODEL_CALLS_PER_TURN", "8")),
        max_tool_calls=int(_env("EC_AGENT_MAX_TOOL_CALLS_PER_TURN", "8")),
        usage_ledger=PostgresModelUsageStore(),
    )


async def _run_turn(
    runtime: OpenRouterAgentRuntime,
    toolset: ConciergeToolsetImpl,
    history: list[dict[str, Any]],
    message: str,
    shown: list[tuple[str, str]],
    *,
    verbose: bool,
) -> None:
    answer = ""
    rendered: list[str] = []
    async for event in runtime.run_turn(
        system_prompt=build_system_prompt(shown=shown),
        history=history,
        message=message,
        toolset=toolset,
    ):
        match event:
            case TurnToolStart(tool=tool, arguments=arguments):
                supplied = {k: v for k, v in arguments.items() if v not in ("", 0, None)}
                print(f"  {DIM}-> {tool}({_compact(supplied)}){OFF}", flush=True)
            case TurnToolEnd(tool=tool, status=status, duration_ms=ms, summary=summary):
                tone = YELLOW if status != "ok" else DIM
                print(f"  {tone}<- {tool}: {status} {summary} {ms}ms{OFF}", flush=True)
            case TurnBlock(block=block, index=index):
                rendered.append(f"  [{index}] {type(block).__name__}: {_describe(block)}")
            case TurnDelta(text=text):
                answer = text
            case TurnEnd() as end:
                shown.extend(shown_refs(end.blocks))
                print(f"\n{end.text}\n")
                if rendered:
                    print(f"{DIM}blocks the UI would render:{OFF}")
                    for line in rendered:
                        print(f"{DIM}{line}{OFF}")
                    print()
                if verbose:
                    print(
                        f"{DIM}[{end.model_calls} model calls, {end.tool_calls} tool calls, "
                        f"{end.input_tokens} in / {end.output_tokens} out, "
                        f"stop={end.stop_reason}]{OFF}\n"
                    )
                history.append({"role": "user", "content": message})
                history.append({"role": "assistant", "content": end.text})
                del history[: max(0, len(history) - MAX_HISTORY_TURNS * 2)]
            case TurnError(code=code, message=detail):
                print(f"\n{RED}error [{code}] {detail}{OFF}\n")
    if answer and not history:
        print(answer)


def _describe(block: Any) -> str:
    """One line per block, so the terminal shows what a client would draw."""
    items = getattr(block, "items", None)
    if items is not None:
        titles = [str(i.get("title") or i.get("name") or "?") for i in items]
        return f"{len(items)} - " + "; ".join(t[:34] for t in titles[:4])
    rows = getattr(block, "rows", None)
    if rows is not None:
        return f"{len(rows)} rows, total={getattr(block, 'total', 0)}"
    return str(getattr(block, "text", ""))[:90]


def _compact(arguments: dict[str, Any]) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in arguments.items())


async def main() -> None:
    parser = argparse.ArgumentParser(description="Talk to the concierge agent locally.")
    parser.add_argument("--once", help="run a single prompt and exit")
    parser.add_argument("--quiet", action="store_true", help="hide the per-turn token/cost line")
    args = parser.parse_args()

    settings = get_settings()
    init_engine(settings.database_url, pool_size=2)
    toolset = ConciergeToolsetImpl(
        PostgresCatalogRepository(DeterministicEmbedding()),
        PostgresCatalogEntityRepository(),
    )
    runtime = _build_runtime()
    history: list[dict[str, Any]] = []
    shown: list[tuple[str, str]] = []

    if args.once:
        await _run_turn(runtime, toolset, history, args.once, shown, verbose=not args.quiet)
        return

    print(
        f"{BOLD}Events Concierge{OFF} {DIM}({_env('EC_AGENT_MODEL', 'deepseek/deepseek-v4-flash')})"
        f" - ctrl-d to exit{OFF}\n"
    )
    while True:
        try:
            # ``input`` blocks the event loop, which would stall any concurrent tool work.
            message = (await asyncio.to_thread(input, f"{CYAN}you >{OFF} ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not message:
            continue
        if message in {"/quit", "/exit"}:
            return
        try:
            await _run_turn(runtime, toolset, history, message, shown, verbose=not args.quiet)
        except Exception as exc:  # keep the REPL alive across a bad turn
            print(f"{RED}turn failed: {type(exc).__name__}: {exc}{OFF}\n", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
