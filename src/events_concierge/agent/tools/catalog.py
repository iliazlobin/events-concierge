"""Catalog read tools.

These call ``PostgresCatalogRepository`` directly rather than looping back through HTTP. The
catalog family is tenant-neutral by ADR-001 -- the API's own handlers ``del tenant_id`` before
touching it -- so ``system_session_scope`` inside the repository is the correct access path and
an in-process call saves a serialize/parse round trip per tool call.

Every parameter is a flat scalar with a sentinel default. See ``ports/agent.py`` for why unions
are unsafe across model gateways.
"""

from __future__ import annotations

import logging
from typing import Any

from ...adapters.postgres.catalog import PostgresCatalogRepository
from ...domain.catalog_browse import CatalogBrowseEvent
from ...domain.event_semantics import CATALOG_TOPICS, TOPIC_LABELS
from ...ports.agent import ToolResult
from .projections import (
    DEFAULT_TIME_ZONE,
    coverage_block,
    day_rows,
    event_card,
    utc_window,
    web_item,
)

_log = logging.getLogger(__name__)

MAX_SEARCH_LIMIT = 10
LOCATION_SCOPES = frozenset({"bay_area", "manhattan", "los_angeles_area"})
TOPIC_SLUGS = ", ".join(sorted(CATALOG_TOPICS))


def _csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _normalize_city(value: str) -> str:
    """Match the catalog's own city normalization: casefold and strip non-alphanumerics."""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _invalid(hint: str) -> ToolResult:
    return ToolResult(status="invalid_argument", hint=hint)


def _local_day(row: CatalogBrowseEvent) -> str:
    """The event's own local start day, which is what a user means by 'on Saturday'."""
    return row.canonical_event.start_at.astimezone(DEFAULT_TIME_ZONE).strftime("%Y-%m-%d")


class CatalogTools:
    """Catalog reads, bound to one repository and one ref-minting sequence.

    Refs are minted here and remembered, so a later tool can only be handed an event this
    conversation actually surfaced.
    """

    def __init__(self, catalog: PostgresCatalogRepository) -> None:
        self._catalog = catalog
        self._refs: dict[str, str] = {}
        self._next_ref = 1

    def resolve_ref(self, ref: str) -> str | None:
        """Map a model-supplied ref back to a canonical event id, or None if we never minted it."""
        return self._refs.get(ref.strip().upper())

    def _mint(self, canonical_event_id: str) -> str:
        """Stable per event: a repeat search re-uses the handle the user already saw.

        Minting a fresh ref for a row the user has seen would make the same event look like
        several, both to the model reasoning about "the second one" and to the client trying
        to de-duplicate what it draws.
        """
        for existing, known in self._refs.items():
            if known == canonical_event_id:
                return existing
        ref = f"E{self._next_ref}"
        self._next_ref += 1
        self._refs[ref] = canonical_event_id
        return ref

    async def search_events(
        self,
        q: str = "",
        topics: str = "",
        city: str = "",
        location_scope: str = "",
        date_from: str = "",
        date_to: str = "",
        price: str = "",
        limit: int = 6,
    ) -> ToolResult:
        """Search the catalog. Validation first, so a bad slug is a hint rather than zero rows."""
        selected_topics = _csv(topics)
        unknown_topics = [t for t in selected_topics if t not in CATALOG_TOPICS]
        if unknown_topics:
            return _invalid(
                f"unknown topic slug(s) {', '.join(unknown_topics)}. Valid slugs: {TOPIC_SLUGS}"
            )

        scopes = _csv(location_scope)
        unknown_scopes = [s for s in scopes if s not in LOCATION_SCOPES]
        if unknown_scopes:
            return _invalid(
                f"unknown location_scope {', '.join(unknown_scopes)}. "
                "Valid: bay_area, manhattan, los_angeles_area"
            )

        if price and price not in {"free", "paid"}:
            return _invalid("price must be 'free', 'paid', or empty")

        if bool(date_from) != bool(date_to):
            return _invalid("supply both date_from and date_to, or neither")

        window: tuple[Any, Any] = (None, None)
        if date_from:
            try:
                window = utc_window(date_from, date_to)
            except ValueError:
                return _invalid("dates must be local calendar dates as YYYY-MM-DD")
            if window[1] < window[0]:
                return _invalid("date_to cannot be earlier than date_from")

        bounded = max(1, min(int(limit or 6), MAX_SEARCH_LIMIT))
        # The catalog matches a window by interval OVERLAP, so an event that began the previous
        # evening and is still running qualifies. That is right for an agenda and wrong for a
        # question: "this weekend" does not mean a Friday show that ran past midnight. Those
        # leading rows also sort first, so a small page can be entirely leftovers. Over-fetch,
        # then keep only events whose local start day falls inside the requested range.
        overlap_headroom = 3 if date_from else 1
        try:
            rows, _providers = await self._catalog.browse_current(
                after=None,
                limit=bounded * overlap_headroom + 1,
                starts_after=window[0],
                starts_before=window[1],
                query=q.strip() or None,
                cities=tuple(_normalize_city(c) for c in _csv(city)),
                location_scopes=scopes,
                price=price or None,
                price_max_cents=None,
                topics=selected_topics,
                sort="soonest",
                include_providers=False,
            )
        except Exception:
            # An exception escaping a tool aborts the whole turn; the model can work with an
            # honest "unavailable" and cannot work with a dead stream.
            _log.exception("agent_tool_failed", extra={"tool": "search_events"})
            return ToolResult(status="unavailable", hint="the catalog could not be reached")

        if date_from:
            rows = [row for row in rows if date_from <= _local_day(row) <= date_to]
        more = len(rows) > bounded
        page = rows[:bounded]

        if not page:
            return ToolResult(
                status="empty",
                payload={
                    "filters_applied": {
                        k: v
                        for k, v in {
                            "q": q.strip(),
                            "topics": topics,
                            "city": city,
                            "location_scope": location_scope,
                            "date_from": date_from,
                            "date_to": date_to,
                            "price": price,
                        }.items()
                        if v
                    },
                    "try_next": self._try_next(q, selected_topics, city, scopes, price),
                },
                hint=(
                    "Zero matches is common here and rarely means nothing exists. Work through "
                    "try_next before telling the user there is nothing."
                ),
            )

        refs = [self._mint(str(row.canonical_event.canonical_event_id)) for row in page]
        return ToolResult(
            status="ok",
            payload={
                "count": len(page),
                "more_available": more,
                "coverage": coverage_block(page),
                "events": [event_card(row, ref) for row, ref in zip(page, refs, strict=True)],
            },
            # The browser gets the whole record; the model above gets the compact card.
            render={
                "events": [
                    {**web_item(row), "ref": ref} for row, ref in zip(page, refs, strict=True)
                ]
            },
        )

    @staticmethod
    def _try_next(
        q: str,
        topics: tuple[str, ...],
        city: str,
        scopes: tuple[str, ...],
        price: str,
    ) -> list[dict[str, Any]]:
        """Rank the repairs most likely to turn this empty result into a real one.

        Ordered by how often each cause is the real one. A multi-word ``q`` is far and away the
        most common: the predicate is a literal substring match over the event text, so "live
        jazz music tonight" cannot match anything, ever.
        """
        suggestions: list[dict[str, Any]] = []
        if len(q.split()) > 1:
            suggestions.append(
                {
                    "why": "q is a literal substring match, so a phrase matches nothing",
                    "args": {"q": q.split(maxsplit=1)[0]},
                }
            )
        if q and topics:
            suggestions.append(
                {"why": "drop the keyword and keep the topic filter", "args": {"q": ""}}
            )
        if q and not topics:
            guess = _topic_guess(q)
            if guess:
                suggestions.append(
                    {
                        "why": f"filter by the {guess} topic instead of the keyword",
                        "args": {"q": "", "topics": guess},
                    }
                )
        if price:
            suggestions.append(
                {
                    "why": "price is unpublished for roughly three quarters of the catalog, "
                    "so a price filter hides events that may well qualify",
                    "args": {"price": ""},
                }
            )
        if city and not scopes:
            suggestions.append(
                {
                    "why": "widen from one city to the metro",
                    "args": {"city": "", "location_scope": "bay_area"},
                }
            )
        suggestions.append({"why": "widen the date window", "args": {"date_to": "later"}})
        return suggestions[:4]

    async def count_events_by_day(
        self,
        date_from: str,
        date_to: str,
        topics: str = "",
        city: str = "",
        location_scope: str = "",
        price: str = "",
    ) -> ToolResult:
        """Count per local day. One aggregate query instead of paging a whole range."""
        selected_topics = _csv(topics)
        unknown_topics = [t for t in selected_topics if t not in CATALOG_TOPICS]
        if unknown_topics:
            return _invalid(
                f"unknown topic slug(s) {', '.join(unknown_topics)}. Valid slugs: {TOPIC_SLUGS}"
            )
        scopes = _csv(location_scope)
        if any(s not in LOCATION_SCOPES for s in scopes):
            return _invalid("location_scope must be bay_area, manhattan, or los_angeles_area")
        if price and price not in {"free", "paid"}:
            return _invalid("price must be 'free', 'paid', or empty")
        if not (date_from and date_to):
            return _invalid("both date_from and date_to are required, as YYYY-MM-DD")

        try:
            starts_after, starts_before = utc_window(date_from, date_to)
        except ValueError:
            return _invalid("dates must be local calendar dates as YYYY-MM-DD")

        try:
            days = await self._catalog.list_day_facets(
                starts_after=starts_after,
                starts_before=starts_before,
                query=None,
                cities=tuple(_normalize_city(c) for c in _csv(city)),
                location_scopes=scopes,
                price=price or None,
                price_max_cents=None,
                topics=selected_topics,
                time_zone="America/Los_Angeles",
            )
        except Exception:
            _log.exception("agent_tool_failed", extra={"tool": "count_events_by_day"})
            return ToolResult(status="unavailable", hint="the catalog could not be reached")

        if not days:
            return ToolResult(
                status="empty",
                payload={"filters_applied": {"date_from": date_from, "date_to": date_to}},
                hint="no events in that window under those filters; try widening it",
            )

        # Same overlap semantics as the search window: drop buckets outside the asked range.
        rows = [row for row in day_rows(days) if date_from <= row["day"] <= date_to]
        if not rows:
            return ToolResult(
                status="empty",
                payload={"filters_applied": {"date_from": date_from, "date_to": date_to}},
                hint="no events start in that window under those filters",
            )
        return ToolResult(
            status="ok",
            payload={"days": rows, "total": sum(r["count"] for r in rows)},
        )


def _topic_guess(q: str) -> str:
    """Best-effort keyword -> topic slug, used only to suggest a repair."""
    lowered = q.lower()
    for slug, label in TOPIC_LABELS.items():
        if slug in lowered or label.lower() in lowered:
            return slug
    for slug, words in {
        "music": ("jazz", "concert", "band", "gig", "dj", "orchestra", "choir"),
        "ai": ("llm", "machine learning", "ml", "genai", "agent"),
        "technology": ("hackathon", "developer", "coding", "engineer"),
        "food-drink": ("dinner", "brunch", "tasting", "cooking", "wine", "beer"),
        "outdoors": ("hike", "hiking", "walk", "kayak", "climb"),
        "wellness": ("yoga", "meditation", "pilates", "breathwork"),
        "arts": ("gallery", "museum", "theatre", "theater", "dance", "film"),
    }.items():
        if any(word in lowered for word in words):
            return slug
    return ""
