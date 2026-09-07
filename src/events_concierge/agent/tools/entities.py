"""Event-detail and organizer tools -- the follow-up half of the conversation.

Search answers "what's on". These answer everything that comes next: who is running it, what
else have they run, is this a series, what else is on that night. Without them a follow-up like
"who are the hosts?" has nowhere to go but back through search, which is how the old regex chat
ended up answering it with nineteen events matching the substring "who".

The entity index is built only from listings that name a host, so it spans a small slice of the
catalog. Every result says so in numbers rather than leaving the model to infer it.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from ...adapters.postgres.catalog_entities import PostgresCatalogEntityRepository
from ...domain.catalog_browse import CatalogBrowseEvent
from ...domain.catalog_entities import CatalogEntity, CatalogEntityDetail
from ...domain.events import CanonicalEvent
from ...ports.agent import ToolResult
from ...ports.repositories import CatalogRepository
from .projections import DEFAULT_TIME_ZONE, render_when, web_item

_log = logging.getLogger(__name__)

MAX_ORGANIZER_RESULTS = 10
MAX_ORGANIZER_EVENTS = 8
MAX_SELECTED_EVENTS = 12


def _invalid(hint: str) -> ToolResult:
    return ToolResult(status="invalid_argument", hint=hint)


def _people(names: tuple[str, ...], limit: int = 12) -> list[str]:
    return list(names[:limit])


class EntityTools:
    """Event detail plus the organizer graph, sharing the search tools' ref namespace."""

    def __init__(
        self,
        catalog: CatalogRepository,
        entities: PostgresCatalogEntityRepository,
        resolve_event_ref: Any,
    ) -> None:
        self._catalog = catalog
        self._entities = entities
        self._resolve_event_ref = resolve_event_ref
        self._entity_refs: dict[str, str] = {}
        self._next_ref = 1

    def _mint(self, entity_id: str) -> str:
        """Stable per organizer, for the same reason event refs are (see CatalogTools._mint)."""
        for existing, known in self._entity_refs.items():
            if known == entity_id:
                return existing
        ref = f"O{self._next_ref}"
        self._next_ref += 1
        self._entity_refs[ref] = entity_id
        return ref

    async def get_event(self, ref: str) -> ToolResult:
        """Everything published about one event the user has already been shown."""
        canonical_event_id = self._resolve_event_ref(ref)
        if canonical_event_id is None:
            return ToolResult(
                status="not_found",
                hint=(
                    f"'{ref}' is not an event from this conversation. Only use a ref that a "
                    "search in this conversation returned."
                ),
            )
        try:
            event = await self._catalog.get(UUID(canonical_event_id))
        except Exception:
            _log.exception("agent_tool_failed", extra={"tool": "get_event"})
            return ToolResult(status="unavailable", hint="the catalog could not be reached")
        if event is None:
            return ToolResult(status="not_found", hint="that event is no longer in the catalog")
        # get() returns the canonical row without its source observations, so the render
        # projection is built from an empty-sourced wrapper: the card degrades to no provider
        # label rather than inventing one.
        return ToolResult(
            status="ok",
            payload={"event": _event_detail(event, ref)},
            render={"events": [{**web_item(CatalogBrowseEvent(event, ())), "ref": ref}]},
        )

    async def get_selected_events(self, refs: str = "") -> ToolResult:
        """Read every event the user has selected on screen, in one call.

        The per-ref alternative is what exhausted a turn's tool budget in practice: eight refs
        meant eight round trips and nothing left for the answer. One call also lets the model
        compare them, which is most of what a selection is for.
        """
        wanted = [r.strip().upper() for r in refs.split(",") if r.strip()]
        if not wanted:
            return ToolResult(
                status="invalid_argument",
                hint="pass the selected refs as a comma-separated list, e.g. 'E2,E5'",
            )
        found: list[dict[str, Any]] = []
        rich: list[dict[str, Any]] = []
        missing: list[str] = []
        for ref in wanted[:MAX_SELECTED_EVENTS]:
            canonical_event_id = self._resolve_event_ref(ref)
            if canonical_event_id is None:
                missing.append(ref)
                continue
            try:
                event = await self._catalog.get(UUID(canonical_event_id))
            except Exception:
                _log.exception("agent_tool_failed", extra={"tool": "get_selected_events"})
                return ToolResult(status="unavailable", hint="the catalog could not be reached")
            if event is None:
                missing.append(ref)
                continue
            found.append(_event_detail(event, ref))
            rich.append({**web_item(CatalogBrowseEvent(event, ())), "ref": ref})
        if not found:
            return ToolResult(
                status="not_found",
                hint="none of those refs are from this conversation",
            )
        return ToolResult(
            status="ok",
            payload={"count": len(found), "events": found, "unresolved": missing},
            render={"events": rich},
        )

    async def search_organizers(self, q: str = "", limit: int = 8) -> ToolResult:
        """Find organizers, hosts, and speakers by name. A name is required.

        The underlying index has no activity ordering, so an empty query returns an arbitrary
        alphabetical slice -- "a1mobile, Adish Jain, ..." -- which reads like a ranked answer and
        is not one. Refusing the empty query is more useful than serving a list the model would
        then have to caveat.
        """
        if not q.strip():
            return _invalid(
                "search_organizers needs a name to look for. This index cannot rank organizers "
                "by activity, so there is no 'top organizers' listing. To find who runs the most "
                "of something, search the events and read their organizers instead."
            )
        bounded = max(1, min(int(limit or 8), MAX_ORGANIZER_RESULTS))
        try:
            rows = await self._entities.list(query=q.strip() or None, kinds=(), limit=bounded)
        except ValueError as exc:
            return ToolResult(status="invalid_argument", hint=str(exc))
        except Exception:
            _log.exception("agent_tool_failed", extra={"tool": "search_organizers"})
            return ToolResult(status="unavailable", hint="the organizer index could not be read")

        if not rows:
            return ToolResult(
                status="empty",
                payload={"filters_applied": {"q": q.strip()}},
                hint=(
                    "The organizer index only covers listings that name a host, which is a small "
                    "slice of the catalog. A missing name usually means no listing named them, "
                    "not that they do not exist. Say that rather than implying they are unknown."
                ),
            )
        return ToolResult(
            status="ok",
            payload={
                "count": len(rows),
                "organizers": [_entity_card(row, self._mint(str(row.entity_id))) for row in rows],
            },
        )

    async def get_organizer(self, ref: str) -> ToolResult:
        """One organizer: what they run, how often, where, and who with."""
        entity_id = self._entity_refs.get(ref.strip().upper())
        if entity_id is None:
            return ToolResult(
                status="not_found",
                hint=(
                    f"'{ref}' is not an organizer from this conversation. Call search_organizers "
                    "first and use a ref it returned."
                ),
            )
        try:
            detail = await self._entities.get(UUID(entity_id), event_limit=MAX_ORGANIZER_EVENTS * 3)
        except Exception:
            _log.exception("agent_tool_failed", extra={"tool": "get_organizer"})
            return ToolResult(status="unavailable", hint="the organizer index could not be read")
        if detail is None:
            return ToolResult(status="not_found", hint="that organizer is no longer indexed")
        return ToolResult(status="ok", payload={"organizer": _entity_detail(detail, ref)})


def _event_detail(event: CanonicalEvent, ref: str) -> dict[str, Any]:
    """One event, fully expanded, with every unpublished field named rather than nulled."""
    unknown: list[str] = []
    detail: dict[str, Any] = {
        "ref": ref,
        "title": event.title,
        "when": render_when(event.start_at, event.end_at),
        "day": event.start_at.astimezone(DEFAULT_TIME_ZONE).strftime("%Y-%m-%d"),
    }

    where = ", ".join(p for p in (event.venue_name, event.city_norm) if p)
    detail["where"] = where or None
    if not where:
        unknown.append("where")

    # Roles are listed separately because they are genuinely different claims: an organizer is
    # the account that published the listing, a host runs the room, a speaker is on the bill.
    # Collapsing them into "hosts" would assert something the source did not say.
    if event.organizer_name:
        detail["organizer"] = event.organizer_name
    if event.host_names:
        detail["hosts"] = _people(event.host_names)
    if event.speaker_names:
        detail["speakers"] = _people(event.speaker_names)
    if event.partner_names:
        detail["partners"] = _people(event.partner_names)
    if not any((event.organizer_name, event.host_names, event.speaker_names)):
        unknown.append("who")

    if event.price_status.value == "unknown":
        unknown.append("price")
    else:
        detail["price"] = event.price_status.value

    if event.topics:
        detail["topics"] = list(event.topics)
    else:
        unknown.append("topics")

    if event.attendance_count is not None:
        detail["already_going"] = event.attendance_count
    else:
        unknown.append("already_going")

    if event.registration_status.value != "unknown":
        detail["signup"] = event.registration_status.value
    if event.event_status.value != "scheduled":
        detail["status"] = event.event_status.value

    links = [link.registration_url for link in event.source_links if link.registration_url]
    if links:
        detail["signup_url"] = links[0]
    detail["sources"] = [link.source.value for link in event.source_links]
    detail["unknown"] = unknown
    return detail


def _entity_card(entity: CatalogEntity, ref: str) -> dict[str, Any]:
    card: dict[str, Any] = {
        "ref": ref,
        "name": entity.display_name,
        "kind": entity.kind,
        "events_in_catalog": entity.event_count,
        "roles": list(entity.roles),
    }
    if entity.city:
        card["city"] = entity.city
    if entity.summary:
        card["summary"] = entity.summary
    return card


def _entity_detail(detail: CatalogEntityDetail, ref: str) -> dict[str, Any]:
    entity = detail.entity
    body: dict[str, Any] = _entity_card(entity, ref)
    insights = detail.insights
    if insights is not None:
        body["activity"] = {
            "upcoming": insights.upcoming_count,
            "past": insights.past_count,
            "events_per_month": insights.events_per_month,
            "top_topics": list(insights.top_topics[:5]),
            "top_venues": list(insights.top_venues[:3]),
            "free_events": insights.free_count,
            "paid_events": insights.paid_count,
        }
        if insights.typical_attendance is not None:
            body["activity"]["typical_already_going"] = insights.typical_attendance
        if insights.collaborators:
            body["works_with"] = [
                {"name": c.display_name, "shared_events": c.shared_event_count}
                for c in insights.collaborators[:5]
            ]
    upcoming = [event for event in detail.events if not event.is_past][:MAX_ORGANIZER_EVENTS]
    body["upcoming_events"] = [
        {
            "title": event.title,
            "when": render_when(event.start_at, event.end_at),
            "where": ", ".join(p for p in (event.venue_name, event.city) if p) or None,
            "roles": list(event.roles),
        }
        for event in upcoming
    ]
    # Facts come from named public profiles the operator reviewed, never from an open web fetch.
    if detail.external_facts:
        body["public_profile_facts"] = [
            {"key": fact.fact_key, "value": fact.value} for fact in detail.external_facts[:6]
        ]
    return body
