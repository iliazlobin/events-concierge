"""System-prompt assembly.

The prompt is rendered here rather than passed through a framework's ``instruction=`` string.
Frameworks commonly treat ``{name}`` in an instruction as a state template, which would turn
every literal brace in the prompt into a substitution -- and a prompt is exactly the place
where a silent substitution failure is hardest to notice. Substitution happens once, here,
against an explicit sentinel.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

RUNTIME_SENTINEL = "<<<RUNTIME_BLOCK>>>"
DEFAULT_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_PROMPT_PATH = Path(__file__).with_name("system_v1.md")


@lru_cache(maxsize=1)
def _template() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


MAX_SHOWN_REFS = 12


def render_runtime_block(
    now: datetime,
    *,
    profile: str = "",
    shown: Sequence[tuple[str, str]] = (),
    selected: Sequence[str] = (),
    selected_entities: Sequence[str] = (),
    quotes: Sequence[str] = (),
) -> str:
    """The only per-turn part of the prompt: the date, the user, and what is already on screen.

    ``shown`` closes a gap that otherwise costs a wasted search on every follow-up. Conversation
    history carries prose, not tool results, so a ref minted last turn is invisible this turn --
    and a model asked "who are the hosts?" will re-run a search just to obtain a ref it was
    already given. Restating the handful of visible refs is a couple of dozen tokens and removes
    a whole model+tool round trip.
    """
    local = now.astimezone(DEFAULT_TIME_ZONE)
    lines = [f"Today is {local.strftime('%A, %Y-%m-%d')}, in America/Los_Angeles."]
    if profile:
        lines.append(f"About this user: {profile} Do not narrate any of this.")
    if shown:
        listed = "; ".join(f"{ref}={title[:60]}" for ref, title in list(shown)[-MAX_SHOWN_REFS:])
        lines.append(
            f"Already on screen from earlier in this conversation: {listed}. "
            "Use these refs directly for a follow-up about one of them -- do not search again "
            "just to get a ref."
        )
    if selected:
        # Named with titles, and stated as an exclusive list. Listing the selection as bare refs
        # beside the larger "already on screen" roster invited the model to read "these" as
        # everything visible and pass all of them, which answers a question nobody asked.
        titles = dict(shown)
        named = "; ".join(f"{ref} ({titles[ref]})" if ref in titles else ref for ref in selected)
        lines.append(
            f"SELECTION: the user has picked exactly these {len(selected)} on screen and is "
            f"asking about them and nothing else: {named}. Pass EXACTLY these refs to "
            f"get_selected_events -- not the other refs listed above, however many are on "
            f"screen. If their question seems to be about a wider set, answer about the "
            f"selection anyway and say that is what you read."
        )
    if selected_entities:
        lines.append(
            f"SELECTED ORGANIZERS: {', '.join(selected_entities)}. Read them with get_organizer "
            "and answer about them."
        )
    if selected or selected_entities or quotes:
        # The composer shows a single count across all three kinds. Without the breakdown the
        # model reports only the kind it happened to read -- "you selected two events" against a
        # chip reading "4 in context" -- and the user cannot tell which of them is wrong.
        parts = [
            f"{len(selected)} event(s)" if selected else "",
            f"{len(selected_entities)} organizer(s)" if selected_entities else "",
            f"{len(quotes)} highlighted passage(s)" if quotes else "",
        ]
        composition = ", ".join(part for part in parts if part)
        total = len(selected) + len(selected_entities) + len(quotes)
        lines.append(
            f"The user's selection is {total} item(s) in total: {composition}. If you describe "
            f"what they picked, describe all of it -- the count they see counts every kind."
        )
    if quotes:
        # The user's own highlight of text already on screen, so it carries their authority --
        # unlike listing text arriving in a tool result, which is data and never an instruction.
        passages = " | ".join(f'"{quote}"' for quote in quotes)
        lines.append(
            f"HIGHLIGHTED TEXT: the user selected these passages from the conversation and is "
            f"asking about them: {passages}. Treat them as part of their question."
        )
    return "\n".join(lines)


def build_system_prompt(
    now: datetime | None = None,
    *,
    profile: str = "",
    shown: Sequence[tuple[str, str]] = (),
    selected: Sequence[str] = (),
    selected_entities: Sequence[str] = (),
    quotes: Sequence[str] = (),
) -> str:
    """Render the full system prompt for one turn."""
    moment = now or datetime.now(tz=DEFAULT_TIME_ZONE)
    return _template().replace(
        RUNTIME_SENTINEL,
        render_runtime_block(
            moment,
            profile=profile,
            shown=shown,
            selected=selected,
            selected_entities=selected_entities,
            quotes=quotes,
        ),
    )
