"""Conversational agent contract: the seam between a reasoning framework and this system.

The reasoning framework is an implementation detail. It appears only under
``adapters/agent_runtime/<name>/`` and reaches the rest of the system through the protocols
below. Nothing in ``domain/`` or ``application/`` may import a framework, and no framework
type may appear in a signature here -- that containment is the whole point of this module,
and it is what makes the framework choice a one-day question rather than a rewrite.

Tool parameter schemas are deliberately restricted to flat, non-union scalars. Several model
gateways (notably LiteLLM's OpenAI-compatible translation) drop ``anyOf`` from a tool schema,
so a ``str | None`` parameter silently loses its nullability on the way to the model. A
sentinel default ("" for text, 0 for counts) expresses "not supplied" in a way that survives
every gateway.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

__all__ = [
    "AgentBlock",
    "AgentRuntimePort",
    "AgentTurnEvent",
    "ConciergeToolset",
    "EntitiesBlock",
    "EventsBlock",
    "NoticeBlock",
    "PeopleBlock",
    "ProseBlock",
    "TallyBlock",
    "ToolResult",
    "ToolSafety",
    "ToolSpec",
    "ToolStatus",
    "TurnBlock",
    "TurnDelta",
    "TurnEnd",
    "TurnError",
    "TurnToolEnd",
    "TurnToolStart",
]


class ToolSafety(StrEnum):
    """How much authority a tool carries, which decides whether the model may call it freely."""

    READ = "read"
    """Read-only and reversible. The runtime may call it without asking anyone."""

    IDEMPOTENT_WRITE = "idempotent_write"
    """Mutates, but converges on replay under a server-derived key."""

    CONFIRMED_WRITE = "confirmed_write"
    """Has a real-world effect. Requires explicit user confirmation across a turn boundary."""


ToolStatus = Literal[
    "ok",
    "empty",
    "not_found",
    "invalid_argument",
    "conflict",
    "unavailable",
    "confirmation_required",
    "refused",
]
"""The closed outcome vocabulary every tool answers in.

``empty`` is separate from ``ok`` on purpose. A zero-row result is this catalog's most
dangerous outcome, not its most benign one: the browse query matches ``q`` as a literal
substring, so a perfectly reasonable phrase returns nothing at all with no error. A tool that
reported that as ``ok`` with an empty list would invite the model to conclude the world is
empty. ``empty`` carries the filters that were applied and a ranked ``try_next``, which turns
the failure into a loop the model can climb out of.
"""


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """One tool as the model sees it.

    ``description`` is the whole specification -- it is sent to the model verbatim, and no
    gateway reliably forwards per-parameter prose. Parameter semantics therefore belong in the
    description text, not only in the schema.
    """

    name: str
    description: str
    json_schema: Mapping[str, Any]
    safety: ToolSafety = ToolSafety.READ


@dataclass(frozen=True, slots=True)
class ToolResult:
    """What a tool hands back. Never raises past the toolset boundary.

    An exception escaping into the runtime aborts the whole turn, so a tool that cannot answer
    returns ``unavailable`` and lets the model say so honestly instead.
    """

    status: ToolStatus
    payload: Mapping[str, Any] = field(default_factory=dict)
    hint: str | None = None
    """What the model should consider next. Advice for the model, never control flow."""

    render: Mapping[str, Any] = field(default_factory=dict)
    """Full records for the client, which the model never sees.

    The model and the browser want opposite things. The model needs the smallest projection that
    supports an answer, because every field is paid for on every subsequent turn; the browser
    wants the whole record, because it renders descriptions, maps, calendar links and entity
    chips at no token cost at all.

    Splitting them is also the security seam: scraped third-party prose reaches the display path,
    where it is inert, without ever reaching the reasoning loop, where it is an injection vector.
    """

    def as_model_json(self) -> dict[str, Any]:
        """Render the envelope the model actually receives. ``render`` is deliberately absent."""
        body: dict[str, Any] = {"status": self.status, **self.payload}
        if self.hint:
            body["hint"] = self.hint
        return body


@runtime_checkable
class ConciergeToolset(Protocol):
    """The complete set of capabilities exposed to a reasoning model."""

    def describe(self) -> Sequence[ToolSpec]:
        """Every tool the model may call this turn."""
        ...

    async def invoke(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        """Run one tool. Returns a closed-status envelope for every outcome, including failure."""
        ...


@dataclass(frozen=True, slots=True)
class ProseBlock:
    """Assistant prose. Restricted markdown: paragraphs, emphasis, simple lists."""

    text: str
    kind: Literal["prose"] = "prose"


@dataclass(frozen=True, slots=True)
class EventsBlock:
    """Event cards for the client to render with its own components."""

    items: Sequence[Mapping[str, Any]]
    label: str = ""
    kind: Literal["events"] = "events"


@dataclass(frozen=True, slots=True)
class EntitiesBlock:
    """Organizer/host cards."""

    items: Sequence[Mapping[str, Any]]
    label: str = ""
    kind: Literal["entities"] = "entities"


@dataclass(frozen=True, slots=True)
class PeopleBlock:
    """Who is behind a set of events, grouped by event.

    Names are the most link-worthy thing in this catalog -- an organizer leads to everything else
    they run -- and prose is where a name stops being a link. Emitting them structurally keeps
    each one connected to the entity graph the product already has.
    """

    groups: Sequence[Mapping[str, Any]]
    label: str = ""
    kind: Literal["people"] = "people"


@dataclass(frozen=True, slots=True)
class TallyBlock:
    """A small set of labelled counts -- per-day totals, a breakdown by organizer."""

    rows: Sequence[Mapping[str, Any]]
    label: str = ""
    total: int = 0
    kind: Literal["tally"] = "tally"


@dataclass(frozen=True, slots=True)
class NoticeBlock:
    """A structural caveat the answer rests on.

    Emitted by the server from measured coverage rather than left to the model to remember. A
    thin-data warning that depends on the model choosing to write it is a warning that goes
    missing exactly when the data is worst.
    """

    text: str
    tone: Literal["coverage", "degraded", "policy"] = "coverage"
    known: int = 0
    total: int = 0
    kind: Literal["notice"] = "notice"


AgentBlock = (
    ProseBlock | EventsBlock | EntitiesBlock | PeopleBlock | TallyBlock | NoticeBlock
)


@dataclass(frozen=True, slots=True)
class TurnBlock:
    """One renderable block became available."""

    block: AgentBlock
    index: int


@dataclass(frozen=True, slots=True)
class TurnDelta:
    """A fragment of assistant prose."""

    text: str


@dataclass(frozen=True, slots=True)
class TurnToolStart:
    """A tool call is about to run, with the arguments the runtime validated."""

    call_id: str
    tool: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class TurnToolEnd:
    """A tool call finished."""

    call_id: str
    tool: str
    status: ToolStatus
    duration_ms: int
    summary: str = ""


@dataclass(frozen=True, slots=True)
class TurnEnd:
    """The turn completed normally."""

    text: str
    model_calls: int
    tool_calls: int
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = "end_turn"
    blocks: Sequence[AgentBlock] = ()


@dataclass(frozen=True, slots=True)
class TurnError:
    """The turn failed. Exactly one of TurnEnd or TurnError terminates every stream."""

    code: str
    message: str


AgentTurnEvent = TurnBlock | TurnDelta | TurnToolStart | TurnToolEnd | TurnEnd | TurnError


class AgentRuntimePort(Protocol):
    """Drives one conversational turn to completion."""

    def run_turn(
        self,
        *,
        system_prompt: str,
        history: Sequence[Mapping[str, Any]],
        message: str,
        toolset: ConciergeToolset,
    ) -> AsyncIterator[AgentTurnEvent]:
        """Stream one turn. Terminates with exactly one TurnEnd or TurnError."""
        ...
