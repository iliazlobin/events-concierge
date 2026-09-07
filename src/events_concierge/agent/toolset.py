"""The concrete toolset: schemas the model sees, bound to callables it never sees.

Descriptions here are the tool specification. They are sent to the model verbatim and carry
the operational warnings that decide whether a call succeeds -- above all that ``q`` is a
literal substring match, which is the single most common cause of a confidently wrong empty
result in this catalog.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from uuid import UUID

from ..adapters.postgres.catalog import PostgresCatalogRepository
from ..adapters.postgres.catalog_entities import PostgresCatalogEntityRepository
from ..ports.agent import ConciergeToolset, ToolResult, ToolSafety, ToolSpec
from ..ports.saved_catalog_filters import SavedCatalogFilterRepository
from .tools.catalog import LOCATION_SCOPES, MAX_SEARCH_LIMIT, TOPIC_SLUGS, CatalogTools
from .tools.entities import MAX_ORGANIZER_RESULTS, EntityTools
from .tools.presets import PresetTools

_SEARCH_DESCRIPTION = f"""Search the local events catalog by keyword, topic, city, date and price.

Use this for any concrete "what's on" question: a keyword, a specific day or range, a city, a
topic. This is the default discovery tool.

Do NOT use it to count across many days -- count_events_by_day answers that for a fraction of
the cost. Results are ordered strictly by start time, never by relevance, so never present them
as "the best" ones; they are the soonest matching ones.

CRITICAL - how `q` behaves: it is a LITERAL SUBSTRING match over the event's title, description,
venue, city and organizer. It is not semantic and it does not tokenize. q="live jazz music
tonight" matches NOTHING. q="jazz" matches many. Pass at most one or two words, or leave it
empty and filter by `topics` instead.

Parameters:
  q: one keyword, or "" for none. See the warning above. Max 160 characters.
  topics: comma-separated topic slugs. Valid slugs ONLY: {TOPIC_SLUGS}. Anything else is
    rejected with the valid list.
  city: comma-separated city names, e.g. "san francisco,oakland". Matching ignores case,
    spaces and punctuation. Never pass an abbreviation like "SF". An unrecognized city matches
    nothing rather than erroring, so prefer location_scope for a whole metro.
  location_scope: comma-separated, from exactly: {", ".join(sorted(LOCATION_SCOPES))}. Combined
    with `city` as OR, not AND. This catalog holds some New York listings, so a search with
    neither city nor scope can return events 3,000 miles away -- always scope a "near me" ask.
  date_from, date_to: local calendar dates as YYYY-MM-DD, both inclusive. Supply both or
    neither. Compute relative dates from today's date in your instructions; never guess it.
  price: "free", "paid", or "" for any. Price is UNPUBLISHED for roughly three quarters of this
    catalog, and price="free" returns only events explicitly published as free -- it excludes
    unknown-price events that may well be free.
  limit: 1 to {MAX_SEARCH_LIMIT}. Default 6.

Returns status "ok" with count, more_available, coverage and events[]. Each card has ref,
title, day, when, and -- only when the source publishes them -- where, price, topics, by,
already_going, signup. `already_going` is how many people have ALREADY signed up. It is NOT
capacity and NOT remaining spots -- the catalog has no capacity data, so never say "20 spots"
or "room for 20". `signup` is the source's posture: open, waitlist, or sold_out. `unknown` lists the fields this source does not publish: report those as
unpublished, never guess them. `coverage` counts how many rows in THIS page carry each field;
use those numbers rather than any global average. `ref` (e.g. "E3") is how you name an event to
a later tool; never show a ref to the user.

Returns status "empty" with filters_applied and a ranked try_next list of the exact next calls
to make. Work through it before concluding nothing exists, and when you do say so, say what was
actually searched. Returns "invalid_argument" with a hint naming valid values, or "unavailable"
if the catalog could not be reached -- in which case say so and do not answer from memory."""

_COUNT_DESCRIPTION = f"""Count events per local calendar day, with each day's leading topics.

First stop for "how many", "which day", "is there anything on X", "is the weekend busy". Far
cheaper than listing events, so use it to narrow before you call search_events.

Parameters:
  date_from, date_to: local calendar dates as YYYY-MM-DD, both inclusive. Required.
  topics: comma-separated topic slugs, or "". Valid slugs ONLY: {TOPIC_SLUGS}
  city: comma-separated city names, or "". Never an abbreviation.
  location_scope: {", ".join(sorted(LOCATION_SCOPES))}, or "".
  price: "free", "paid", or "".

Returns status "ok" with days[] -- each {{day, count, top_topics}} -- and a total. Counts are
distinct events, so a day's count is not the sum of its topic buckets. Returns "empty" if the
window holds nothing under those filters."""


_GET_EVENT_DESCRIPTION = """Everything published about ONE event you have already shown the user.

Use this for a follow-up about ONE specific event: who is speaking, the full role breakdown,
the signup link. Prefer it over searching again -- a second search cannot answer a question
about a particular event, it just returns a different list.

DO NOT call this once per row to survey a list. Every card from search_events ALREADY carries
`by` (the organizer or first host) and `unknown` (what that source does not publish), so a
question like "who is hosting the most of these" is answered by reading the cards you already
have. Calling this per row exhausts the turn's tool budget and tells you nothing the cards did
not. Reach for it only when you need a field the card does not carry.

Parameters:
  ref: the short handle from an earlier result, e.g. "E3". You cannot pass a title, a URL, or an
    id you read anywhere else -- only a ref a tool in THIS conversation returned.

Returns status "ok" with the event, expanded. It separates `organizer` (the account that
published the listing), `hosts` (who runs the room), `speakers` (who is on the bill) and
`partners`, because those are different claims and most sources publish at most one of them.
`unknown` lists what this source does not publish -- "who" appearing there means nobody is
named at all, which is the normal case for library and civic listings.

Returns "not_found" if the ref is not from this conversation."""

_GET_SELECTED_DESCRIPTION = """Read every event the user has SELECTED on screen, in one call.

When your instructions say the user has selected refs, this is how you read them. Call it once
with all of them -- never get_event per ref, which burns the turn's tool budget and tells you
nothing extra.

Parameters:
  refs: comma-separated handles, e.g. "E2,E5,E9". Pass EXACTLY the refs the SELECTION
    line names and no others, even when more refs are on screen.

Returns the same expanded record get_event returns, for each. `unresolved` lists any ref that is
not from this conversation. Up to 12 at a time."""


_SEARCH_ORGANIZERS_DESCRIPTION = f"""Find organizers, hosts, and speakers by name.

Use when the user asks about a person or group rather than an event: "what does Noisebridge
run", "who is X", "any more from that organizer".

IMPORTANT: this index is built ONLY from listings that name a host, which is a small slice of
the catalog -- most library, civic, and university listings name nobody. A name missing from
this index usually means no listing named them, NOT that they do not exist or are inactive. Say
that plainly rather than implying the person is unknown.

This index CANNOT rank organizers by activity, so there is no "top organizers" listing and an
empty q is rejected. To answer "who runs the most X", search the events and read the organizer
off each card instead.

Parameters:
  q: a name or part of one. Required.
  limit: 1 to {MAX_ORGANIZER_RESULTS}. Default 8.

Returns status "ok" with organizers[], each carrying a ref (e.g. "O1") for get_organizer,
name, kind, roles, and how many catalog events they appear in. Returns "empty" if nothing
matched."""

_GET_ORGANIZER_DESCRIPTION = """One organizer in depth: what they run, how often, where, with whom.

Use for "what else do they do", "are they active", "is this a regular thing".

Parameters:
  ref: a handle from search_organizers, e.g. "O1". Only a ref from THIS conversation.

Returns status "ok" with the organizer, their upcoming events, an activity summary (upcoming
and past counts, events per month, usual topics and venues, free vs paid split) and who they
co-host with. Counts describe only what is in this catalog -- they are not the organizer's
full history, so do not present them as one. Any `public_profile_facts` come from a reviewed
public profile; attribute them as such and never as something you looked up."""


_LIST_PRESETS_DESCRIPTION = """The user's saved filter selections, plus suggested starters.

Use when they mention "my filters", "my usual", "the saved one", or ask what they have saved.
Also worth calling once when someone asks a very open question, so you can offer a preset
instead of guessing.

Returns `saved` -- this user's own selections, most recently used first, each with a ref
(e.g. "F1"), a name, and `describes` (a plain-English summary) -- and `starters`, suggestions
with NO ref that nothing has saved yet.

There is no cross-user popularity data in this system. Never call a preset popular, trending, or
common among users; `saved` is ordered by when THIS user last used it and nothing more."""

_APPLY_PRESET_DESCRIPTION = """Resolve a saved selection, optionally with changes on top.

This is how "use my AI one", "the usual but in Oakland", "same as last time only free" get
answered. Supplied fields REPLACE the preset's, everything else is kept, so you never have to
restate the whole filter.

Parameters:
  ref: a saved preset handle from list_filter_presets, e.g. "F1".
  name: a starter's exact name instead, when the user picked one of those.
  topics, city, location_scope, price, q: overrides. Leave empty to keep the preset's value.
  date_preset: one of all, today, week, weekend, month -- overrides the saved timing.

IMPORTANT: this resolves the filter, it does NOT run it. It returns `search_arguments`; call
search_events with those (turning date_preset into date_from/date_to using today's date), then
say which preset you used and what you changed. Applying also marks the preset as used, which is
what its ordering sorts on."""

_SAVE_PRESET_DESCRIPTION = """Save the filter you just ran under a name.

Use when they say "save this", "remember this one", or clearly want to come back to a search.
Offer it yourself only after a search they reacted well to -- never after every search.

Parameters:
  name: what to call it. Required, at most 60 characters. Prefer their words.
  topics, city, location_scope, price, q, date_preset: only to override the last search; leave
    them all empty to save exactly what you just ran.

Timing is stored as intent, not as dates: a preset saved for "this week" means the coming week
every time it runs, not the week it was created. Only an explicitly-dated search pins its dates.

Returns `rendered`, a one-line summary of what was saved -- echo it back in one short clause so
they can correct it. Returns "conflict" if the name is taken; offer update_filter_preset."""

_UPDATE_PRESET_DESCRIPTION = """Change a saved selection in place, keeping its name and ref.

Use for "update my AI one to include Oakland" or "rename that to Thursday nights". Only the
fields you supply change. To replace a preset wholesale, supply every field you want it to have."""

_DELETE_PRESET_DESCRIPTION = """Delete a saved selection.

Only when the user clearly asks. Confirm which one by name in your reply -- a preset is small but
it is theirs, and deleting the wrong one is annoying to redo."""


def _string(description: str) -> dict[str, Any]:
    return {"type": "string", "description": description}


_PRESET_FILTER_PROPERTIES: dict[str, Any] = {
    "topics": _string("Comma-separated topic slugs, or empty to keep the preset's."),
    "city": _string("Comma-separated city names, or empty to keep the preset's."),
    "location_scope": _string("bay_area, manhattan, los_angeles_area, or empty."),
    "price": _string("'free', 'paid', or empty."),
    "date_preset": _string("all, today, week, weekend, month, or empty."),
    "q": _string("One keyword, or empty. Literal substring match."),
}


class ConciergeToolsetImpl(ConciergeToolset):
    """The catalog, the organizer graph, and the user's saved filter selections."""

    def __init__(
        self,
        catalog: PostgresCatalogRepository,
        entities: PostgresCatalogEntityRepository | None = None,
        saved_filters: SavedCatalogFilterRepository | None = None,
        tenant_id: UUID | None = None,
    ) -> None:
        self.catalog_tools = CatalogTools(catalog)
        self.entity_tools = (
            EntityTools(catalog, entities, self.catalog_tools.resolve_ref)
            if entities is not None
            else None
        )
        # Preset tools need a tenant: the store is RLS-scoped, so without one there is nobody to
        # read or write for and the tools stay off rather than failing per call.
        self.preset_tools = (
            PresetTools(saved_filters, tenant_id)
            if saved_filters is not None and tenant_id is not None
            else None
        )
        self._handlers: dict[str, Callable[..., Awaitable[ToolResult]]] = {
            "search_events": self.catalog_tools.search_events,
            "count_events_by_day": self.catalog_tools.count_events_by_day,
        }
        if self.entity_tools is not None:
            self._handlers["get_event"] = self.entity_tools.get_event
            self._handlers["search_organizers"] = self.entity_tools.search_organizers
            self._handlers["get_organizer"] = self.entity_tools.get_organizer
            self._handlers["get_selected_events"] = self.entity_tools.get_selected_events
        if self.preset_tools is not None:
            self._handlers["list_filter_presets"] = self.preset_tools.list_filter_presets
            self._handlers["apply_filter_preset"] = self.preset_tools.apply_filter_preset
            self._handlers["save_filter_preset"] = self.preset_tools.save_filter_preset
            self._handlers["update_filter_preset"] = self.preset_tools.update_filter_preset
            self._handlers["delete_filter_preset"] = self.preset_tools.delete_filter_preset

    def describe(self) -> Sequence[ToolSpec]:
        return (
            ToolSpec(
                name="search_events",
                description=_SEARCH_DESCRIPTION,
                safety=ToolSafety.READ,
                json_schema={
                    "type": "object",
                    "properties": {
                        "q": _string("One keyword, or empty string. Literal substring match."),
                        "topics": _string(f"Comma-separated slugs from: {TOPIC_SLUGS}"),
                        "city": _string("Comma-separated city names. Never abbreviations."),
                        "location_scope": _string(
                            f"Comma-separated from: {', '.join(sorted(LOCATION_SCOPES))}"
                        ),
                        "date_from": _string("Local date YYYY-MM-DD inclusive, or empty."),
                        "date_to": _string("Local date YYYY-MM-DD inclusive, or empty."),
                        "price": _string("'free', 'paid', or empty for any."),
                        "limit": {
                            "type": "integer",
                            "description": f"1 to {MAX_SEARCH_LIMIT}. Default 6.",
                        },
                    },
                    # Every parameter is required with a sentinel rather than optional: an
                    # optional parameter needs a union type to express "absent", and several
                    # gateways drop unions from the schema on the way to the model.
                    "required": [
                        "q",
                        "topics",
                        "city",
                        "location_scope",
                        "date_from",
                        "date_to",
                        "price",
                        "limit",
                    ],
                },
            ),
            ToolSpec(
                name="count_events_by_day",
                description=_COUNT_DESCRIPTION,
                safety=ToolSafety.READ,
                json_schema={
                    "type": "object",
                    "properties": {
                        "date_from": _string("Local date YYYY-MM-DD inclusive."),
                        "date_to": _string("Local date YYYY-MM-DD inclusive."),
                        "topics": _string(f"Comma-separated slugs from: {TOPIC_SLUGS}, or empty."),
                        "city": _string("Comma-separated city names, or empty."),
                        "location_scope": _string(
                            f"{', '.join(sorted(LOCATION_SCOPES))}, or empty."
                        ),
                        "price": _string("'free', 'paid', or empty."),
                    },
                    "required": [
                        "date_from",
                        "date_to",
                        "topics",
                        "city",
                        "location_scope",
                        "price",
                    ],
                },
            ),
            *(
                ()
                if self.entity_tools is None
                else (
                    ToolSpec(
                        name="get_event",
                        description=_GET_EVENT_DESCRIPTION,
                        safety=ToolSafety.READ,
                        json_schema={
                            "type": "object",
                            "properties": {
                                "ref": _string('An event handle from this conversation, e.g. "E3".')
                            },
                            "required": ["ref"],
                        },
                    ),
                    ToolSpec(
                        name="get_selected_events",
                        description=_GET_SELECTED_DESCRIPTION,
                        safety=ToolSafety.READ,
                        json_schema={
                            "type": "object",
                            "properties": {
                                "refs": _string('Comma-separated selected handles, e.g. "E2,E5".')
                            },
                            "required": ["refs"],
                        },
                    ),
                    ToolSpec(
                        name="search_organizers",
                        description=_SEARCH_ORGANIZERS_DESCRIPTION,
                        safety=ToolSafety.READ,
                        json_schema={
                            "type": "object",
                            "properties": {
                                "q": _string("A name or part of one, or empty for most active."),
                                "limit": {
                                    "type": "integer",
                                    "description": f"1 to {MAX_ORGANIZER_RESULTS}. Default 8.",
                                },
                            },
                            "required": ["q", "limit"],
                        },
                    ),
                    ToolSpec(
                        name="get_organizer",
                        description=_GET_ORGANIZER_DESCRIPTION,
                        safety=ToolSafety.READ,
                        json_schema={
                            "type": "object",
                            "properties": {
                                "ref": _string(
                                    'An organizer handle from search_organizers, e.g. "O1".'
                                )
                            },
                            "required": ["ref"],
                        },
                    ),
                )
            ),
            *(
                ()
                if self.preset_tools is None
                else (
                    ToolSpec(
                        name="list_filter_presets",
                        description=_LIST_PRESETS_DESCRIPTION,
                        safety=ToolSafety.READ,
                        json_schema={"type": "object", "properties": {}, "required": []},
                    ),
                    ToolSpec(
                        name="apply_filter_preset",
                        description=_APPLY_PRESET_DESCRIPTION,
                        safety=ToolSafety.READ,
                        json_schema={
                            "type": "object",
                            "properties": {
                                "ref": _string('A saved preset handle, e.g. "F1", or empty.'),
                                "name": _string("A starter's exact name, or empty."),
                                **_PRESET_FILTER_PROPERTIES,
                            },
                            "required": ["ref", "name", *_PRESET_FILTER_PROPERTIES],
                        },
                    ),
                    ToolSpec(
                        name="save_filter_preset",
                        description=_SAVE_PRESET_DESCRIPTION,
                        safety=ToolSafety.IDEMPOTENT_WRITE,
                        json_schema={
                            "type": "object",
                            "properties": {
                                "name": _string("What to call it. Required, <= 60 characters."),
                                **_PRESET_FILTER_PROPERTIES,
                            },
                            "required": ["name", *_PRESET_FILTER_PROPERTIES],
                        },
                    ),
                    ToolSpec(
                        name="update_filter_preset",
                        description=_UPDATE_PRESET_DESCRIPTION,
                        safety=ToolSafety.IDEMPOTENT_WRITE,
                        json_schema={
                            "type": "object",
                            "properties": {
                                "ref": _string('The preset handle, e.g. "F1".'),
                                "name": _string("A new name, or empty to keep it."),
                                **_PRESET_FILTER_PROPERTIES,
                            },
                            "required": ["ref", "name", *_PRESET_FILTER_PROPERTIES],
                        },
                    ),
                    ToolSpec(
                        name="delete_filter_preset",
                        description=_DELETE_PRESET_DESCRIPTION,
                        safety=ToolSafety.CONFIRMED_WRITE,
                        json_schema={
                            "type": "object",
                            "properties": {"ref": _string('The preset handle, e.g. "F1".')},
                            "required": ["ref"],
                        },
                    ),
                )
            ),
        )

    async def invoke(self, name: str, arguments: Mapping[str, Any]) -> ToolResult:
        handler = self._handlers.get(name)
        if handler is None:
            return ToolResult(
                status="not_found",
                hint=f"no such tool; available: {', '.join(sorted(self._handlers))}",
            )
        # Only declared parameters are forwarded. A model-invented argument is dropped rather
        # than raising a TypeError that would take the turn down with it.
        allowed = set(next(s for s in self.describe() if s.name == name).json_schema["properties"])
        try:
            return await handler(**{k: v for k, v in arguments.items() if k in allowed})
        except TypeError as exc:
            return ToolResult(status="invalid_argument", hint=str(exc))
