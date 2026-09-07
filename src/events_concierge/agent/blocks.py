"""Turn tool results into renderable blocks.

Blocks are derived **by the server from typed tool results**, never parsed out of model prose.
That is the whole design: the model decides what to say, the server decides what to render, and
raw third-party listing text never reaches the layout path. A client that parsed the model's
markdown into components would be one crafted event title away from rendering whatever a
listing's author wanted it to.

It also fixes the thing a prompt cannot guarantee. The coverage caveat -- "only 2 of these 10
name an organizer" -- is emitted here from the measured `coverage` block, so it appears
precisely when the data is thin, whether or not the model remembered to mention it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from ..ports.agent import (
    AgentBlock,
    EntitiesBlock,
    EventsBlock,
    NoticeBlock,
    PeopleBlock,
    TallyBlock,
)

MAX_CARD_BLOCKS_PER_KIND = 2
MAX_EVENTS_PER_BLOCK = 10
THIN_COVERAGE_RATIO = 0.5
MIN_ROWS_FOR_COVERAGE_NOTICE = 2


def blocks_for_tool_result(
    tool: str,
    status: str,
    payload: Mapping[str, Any],
    render: Mapping[str, Any] | None = None,
) -> list[AgentBlock]:
    """Derive the renderable blocks one tool result justifies, if any.

    ``render`` carries the full records when a tool supplies them; the compact model-facing
    payload is the fallback, so a tool that has not been taught the split still renders.
    """
    if status != "ok":
        return _degraded_blocks(tool, status, payload)
    rich = render or {}

    if tool == "search_events":
        events = list(rich.get("events") or payload.get("events") or [])[:MAX_EVENTS_PER_BLOCK]
        if not events:
            return []
        out: list[AgentBlock] = [EventsBlock(items=events)]
        notice = _coverage_notice(payload.get("coverage") or {})
        if notice is not None:
            out.append(notice)
        return out

    if tool == "get_selected_events":
        chosen = list(rich.get("events") or [])
        if not chosen:
            return []
        selection: list[AgentBlock] = [EventsBlock(items=chosen, label="selection")]
        behind = _people_groups(chosen)
        if behind:
            selection.append(PeopleBlock(groups=behind, label="Who is behind these"))
        return selection

    if tool == "get_event":
        detailed = list(rich.get("events") or [])
        if detailed:
            blocks: list[AgentBlock] = [EventsBlock(items=detailed, label="detail")]
            people = _people_groups(detailed)
            if people:
                blocks.append(PeopleBlock(groups=people, label="Who is behind this"))
            return blocks
        event = payload.get("event")
        return [EventsBlock(items=[event], label="detail")] if event else []

    if tool == "count_events_by_day":
        days = list(payload.get("days") or [])
        if not days:
            return []
        return [
            TallyBlock(
                rows=[{"label": day.get("day", ""), "value": day.get("count", 0)} for day in days],
                label="Events per day",
                total=int(payload.get("total") or 0),
            )
        ]

    if tool == "search_organizers":
        organizers = list(payload.get("organizers") or [])
        return [EntitiesBlock(items=organizers)] if organizers else []

    if tool == "get_organizer":
        organizer = payload.get("organizer")
        return [EntitiesBlock(items=[organizer], label="detail")] if organizer else []

    return []


_ROLE_ORDER = (
    ("organizer_name", "organizer"),
    ("host_names", "host"),
    ("speaker_names", "speaker"),
    ("partner_names", "partner"),
)


def _people_groups(events: list[Any]) -> list[dict[str, Any]]:
    """Group the named people and organizations behind each event.

    Roles are kept apart rather than merged into "hosts": an organizer published the listing, a
    host runs the room, a speaker is on the bill. Most sources publish at most one of the three,
    so collapsing them would assert something no source said.
    """
    groups: list[dict[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping):
            continue
        profiles = {
            str(profile.get("name", "")).casefold(): profile.get("profile_url")
            for profile in (event.get("entity_profiles") or [])
            if isinstance(profile, Mapping)
        }
        members: list[dict[str, Any]] = []
        seen: set[str] = set()
        for field, role in _ROLE_ORDER:
            raw = event.get(field)
            names = [raw] if isinstance(raw, str) else list(raw or [])
            for name in names:
                key = str(name).casefold()
                if not name or key in seen:
                    continue
                seen.add(key)
                member: dict[str, Any] = {"name": name, "role": role}
                if profiles.get(key):
                    member["profile_url"] = profiles[key]
                members.append(member)
        if members:
            groups.append(
                {
                    "canonical_event_id": event.get("canonical_event_id"),
                    "event_title": event.get("title"),
                    "ref": event.get("ref"),
                    "members": members,
                }
            )
    return groups


def _degraded_blocks(tool: str, status: str, payload: Mapping[str, Any]) -> list[AgentBlock]:
    """Surface a failed or empty tool call as a visible state, not silence.

    A turn whose tools all failed can still produce fluent prose. Rendering nothing in that case
    lets a degraded answer look identical to a good one.
    """
    if status == "unavailable":
        return [
            NoticeBlock(
                text="The catalog could not be reached, so this answer may be incomplete.",
                tone="degraded",
            )
        ]
    if status == "empty" and tool == "search_events":
        applied = payload.get("filters_applied") or {}
        described = ", ".join(f"{k}={v}" for k, v in applied.items()) or "no filters"
        return [NoticeBlock(text=f"No events matched {described}.", tone="coverage")]
    return []


def _coverage_notice(coverage: Mapping[str, Any]) -> NoticeBlock | None:
    """Warn when the answer rests on a field most of these rows do not publish."""
    total = int(coverage.get("total") or 0)
    if total < MIN_ROWS_FOR_COVERAGE_NOTICE:
        return None
    known = int(coverage.get("by") or 0)
    if known / total > THIN_COVERAGE_RATIO:
        return None
    return NoticeBlock(
        text=(
            f"{known} of these {total} listings name an organizer. "
            "The rest of these sources do not publish one."
        ),
        tone="coverage",
        known=known,
        total=total,
    )


def dedupe_blocks(blocks: list[AgentBlock]) -> list[AgentBlock]:
    """Choose what a turn should actually draw, from everything its tools produced.

    Two shapes have to be told apart, because the right render is the opposite in each:

    * **One thing looked at closely** -- a list search followed by a single detail lookup. The
      list is scaffolding the model used to find the thing; drawing it shows the user eight
      fuzzy name matches beside an answer about one of them. Draw the detail.
    * **Many things surveyed** -- a list followed by a detail lookup per row, which is how a
      question like "who is hosting the most of these" gets answered. Here the details are the
      scaffolding and the list is the subject. Draw the list.

    So: exactly one detail supersedes its list; several details defer to it. Within whatever
    survives, refs are stable per underlying id, so repeats collapse exactly.
    """
    passthrough = [b for b in blocks if not isinstance(b, EventsBlock | EntitiesBlock)]
    chosen: list[EventsBlock | EntitiesBlock] = []
    chosen.extend(_select([b for b in blocks if isinstance(b, EventsBlock)]))
    chosen.extend(_select([b for b in blocks if isinstance(b, EntitiesBlock)]))

    seen: set[str] = set()
    kept: list[AgentBlock] = list(passthrough)
    for block in chosen:
        fresh = [
            item
            for item in block.items
            if isinstance(item, Mapping) and str(item.get("ref", "")) not in seen
        ]
        if not fresh:
            continue
        seen.update(str(item.get("ref", "")) for item in fresh)
        kept.append(
            EventsBlock(items=fresh, label=block.label)
            if isinstance(block, EventsBlock)
            else EntitiesBlock(items=fresh, label=block.label)
        )
    return kept


def _select[T: (EventsBlock, EntitiesBlock)](of_kind: list[T]) -> list[T]:
    """Apply the detail-vs-survey rule within one block kind."""
    if not of_kind:
        return []
    details = [b for b in of_kind if b.label == "detail"]
    lists = [b for b in of_kind if b.label != "detail"]
    if len(details) == 1:
        return details
    if lists:
        return lists[:MAX_CARD_BLOCKS_PER_KIND]
    return details[:MAX_CARD_BLOCKS_PER_KIND]


def shown_refs(blocks: Sequence[AgentBlock]) -> list[tuple[str, str]]:
    """The (ref, title) pairs a finished turn put on screen, for the next turn's prompt."""
    pairs: list[tuple[str, str]] = []
    for block in blocks:
        items = getattr(block, "items", None)
        if not items:
            continue
        for item in items:
            if not isinstance(item, Mapping):
                continue
            ref = str(item.get("ref") or "")
            title = str(item.get("title") or item.get("name") or "")
            if ref and title:
                pairs.append((ref, title))
    return pairs
