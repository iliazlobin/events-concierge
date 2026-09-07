"""Saved-filter tools: reuse a named search, tweak it, run it, keep it.

These sit on the existing ``saved_catalog_filters`` store rather than inventing a second one, so
anything saved from chat shows up in the web filter picker and vice versa. The stored payload is
therefore the *client's* ``CatalogFilters`` vocabulary, not this module's flat tool vocabulary,
and everything here translates in both directions.

Two design calls worth stating, because they are what make presets useful rather than tidy:

**A preset stores intent, not resolved dates.** "Free Friday nights" saved as
``datePreset: "week"`` means the coming week every time it runs; saved as
``customStart: 2026-08-28`` it means one Friday in the past, forever, and quietly returns
nothing a week later. Only an explicitly-dated request pins its dates.

**There is no cross-user popularity here**, so nothing pretends there is. "Popular" is the
tenant's own most-used selections by ``last_used_at``, and the starters are derived from measured
catalog density -- a starter that returns zero events would be worse than no starter at all.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from ...ports.agent import ToolResult
from ...ports.saved_catalog_filters import SavedCatalogFilter, SavedCatalogFilterRepository

_log = logging.getLogger(__name__)

MAX_PRESET_NAME = 60

# Bay Area starters, each chosen because the catalog actually carries that slice. They are
# suggestions, not rows: nothing is written until the user saves one.
STARTER_PRESETS: tuple[dict[str, Any], ...] = (
    {
        "name": "AI this week",
        "why": "the densest well-covered slice: hosts and prices are published for most of it",
        "filters": {"topics": "ai", "location_scope": "bay_area", "date_preset": "week"},
    },
    {
        "name": "Free evenings in SF",
        "why": "free events are explicitly published, so this filter is trustworthy",
        "filters": {"price": "free", "city": "san francisco", "date_preset": "week"},
    },
    {
        "name": "This weekend, anything",
        "why": "the weekend is the busiest window and the least filtered",
        "filters": {"location_scope": "bay_area", "date_preset": "weekend"},
    },
    {
        "name": "Music nearby",
        "why": "music is well tagged across library, civic and venue sources",
        "filters": {"topics": "music", "location_scope": "bay_area", "date_preset": "week"},
    },
)

_EMPTY_CLIENT_FILTERS: dict[str, Any] = {
    "query": "",
    "sort": "soonest",
    "datePreset": "week",
    "customStart": "",
    "customEnd": "",
    "sourceKeys": [],
    "city": "",
    "cities": [],
    "locationScopes": [],
    "price": "any",
    "priceComparison": "any",
    "priceMinDollars": "",
    "priceMaxDollars": "",
    "topics": [],
}


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _normalize_city(value: str) -> str:
    return "".join(ch for ch in value.lower() if ch.isalnum())


def to_client_filters(agent_filters: dict[str, Any]) -> dict[str, Any]:
    """Agent vocabulary -> the web's ``CatalogFilters``, which is what gets stored."""
    payload = dict(_EMPTY_CLIENT_FILTERS)
    payload["query"] = str(agent_filters.get("q") or "")
    payload["topics"] = _csv(str(agent_filters.get("topics") or ""))
    cities = [_normalize_city(c) for c in _csv(str(agent_filters.get("city") or ""))]
    payload["cities"] = cities
    payload["city"] = cities[0] if cities else ""
    payload["locationScopes"] = _csv(str(agent_filters.get("location_scope") or ""))
    price = str(agent_filters.get("price") or "")
    payload["price"] = price if price in {"free", "paid", "unknown"} else "any"

    date_from = str(agent_filters.get("date_from") or "")
    date_to = str(agent_filters.get("date_to") or "")
    preset = str(agent_filters.get("date_preset") or "")
    if preset in {"all", "today", "week", "weekend", "month"}:
        payload["datePreset"] = preset
    elif date_from and date_to:
        # An explicitly-dated ask is pinned; everything else stays relative so the preset keeps
        # meaning the same thing next week.
        payload["datePreset"] = "custom"
        payload["customStart"] = date_from
        payload["customEnd"] = date_to
    return payload


def to_agent_filters(payload: dict[str, Any]) -> dict[str, Any]:
    """``CatalogFilters`` -> the flat vocabulary the search tool takes."""
    cities = payload.get("cities") or ([payload["city"]] if payload.get("city") else [])
    preset = str(payload.get("datePreset") or "week")
    agent: dict[str, Any] = {
        "q": str(payload.get("query") or ""),
        "topics": ",".join(payload.get("topics") or []),
        "city": ",".join(str(c) for c in cities),
        "location_scope": ",".join(payload.get("locationScopes") or []),
        "price": "" if payload.get("price") in (None, "any") else str(payload["price"]),
        "date_preset": preset,
        "date_from": "",
        "date_to": "",
    }
    if preset == "custom":
        agent["date_from"] = str(payload.get("customStart") or "")
        agent["date_to"] = str(payload.get("customEnd") or "")
    return agent


def describe(agent_filters: dict[str, Any]) -> str:
    """One plain-English line, so a save or an apply can be confirmed without reading JSON."""
    parts: list[str] = []
    if agent_filters.get("topics"):
        parts.append(str(agent_filters["topics"]).replace(",", ", "))
    if agent_filters.get("q"):
        parts.append(f'matching "{agent_filters["q"]}"')
    if agent_filters.get("price"):
        parts.append(f"{agent_filters['price']} only")
    where = agent_filters.get("city") or agent_filters.get("location_scope")
    if where:
        parts.append(f"in {str(where).replace('_', ' ').replace(',', ', ')}")
    preset = agent_filters.get("date_preset")
    if preset == "custom" and agent_filters.get("date_from"):
        parts.append(f"{agent_filters['date_from']} to {agent_filters['date_to']}")
    elif preset and preset != "all":
        parts.append(
            {
                "week": "this week",
                "weekend": "this weekend",
                "today": "today",
                "month": "this month",
            }.get(str(preset), str(preset))
        )
    return ", ".join(parts) if parts else "everything upcoming"


class PresetTools:
    """Saved-filter reads and writes, bound to one tenant."""

    def __init__(self, repository: SavedCatalogFilterRepository, tenant_id: UUID) -> None:
        self._repository = repository
        self._tenant_id = tenant_id
        self._refs: dict[str, UUID] = {}
        self._next_ref = 1
        # The last filter actually run this conversation, so "save this" has a referent.
        self.last_run: dict[str, Any] | None = None

    def _mint(self, saved_filter_id: UUID) -> str:
        for existing, known in self._refs.items():
            if known == saved_filter_id:
                return existing
        ref = f"F{self._next_ref}"
        self._next_ref += 1
        self._refs[ref] = saved_filter_id
        return ref

    def resolve(self, ref: str) -> UUID | None:
        return self._refs.get(ref.strip().upper())

    def _card(self, saved: SavedCatalogFilter) -> dict[str, Any]:
        agent = to_agent_filters(saved.payload)
        return {
            "ref": self._mint(saved.saved_filter_id),
            "name": saved.name,
            "describes": describe(agent),
            "last_used": saved.last_used_at.isoformat() if saved.last_used_at else None,
            "filters": agent,
        }

    async def list_filter_presets(self) -> ToolResult:
        """The tenant's saved selections, most-recently-used first, plus starters."""
        try:
            stored = await self._repository.list_filters(self._tenant_id)
        except Exception:
            _log.exception("agent_tool_failed", extra={"tool": "list_filter_presets"})
            return ToolResult(status="unavailable", hint="saved filters could not be read")

        starters = [
            {
                "name": s["name"],
                "describes": describe(s["filters"]),
                "why": s["why"],
                "filters": s["filters"],
                "ref": "",
            }
            for s in STARTER_PRESETS
        ]
        return ToolResult(
            status="ok",
            payload={
                "saved": [self._card(row) for row in stored],
                "starters": starters,
            },
            hint=(
                "`saved` is this user's own, ordered by most recently used -- there is no "
                "cross-user popularity in this system, so never describe one as popular or "
                "trending. `starters` are suggestions with no ref: to keep one, run it and then "
                "save_filter_preset."
            ),
        )

    async def apply_filter_preset(
        self,
        ref: str = "",
        name: str = "",
        topics: str = "",
        city: str = "",
        location_scope: str = "",
        price: str = "",
        date_preset: str = "",
        q: str = "",
    ) -> ToolResult:
        """Resolve a saved selection, apply any overrides, and hand back the merged filter.

        Overrides are a *delta*: only the fields supplied replace the saved ones, so "my AI one
        but in Oakland" is one call rather than a reconstruction the user has to check.
        """
        base: dict[str, Any]
        source_name = name
        if ref:
            saved_filter_id = self.resolve(ref)
            if saved_filter_id is None:
                return ToolResult(
                    status="not_found",
                    hint=f"'{ref}' is not a preset from this conversation; call "
                    "list_filter_presets first",
                )
            try:
                touched = await self._repository.touch_filter(self._tenant_id, saved_filter_id)
            except ValueError:
                return ToolResult(status="not_found", hint="that preset no longer exists")
            except Exception:
                _log.exception("agent_tool_failed", extra={"tool": "apply_filter_preset"})
                return ToolResult(status="unavailable", hint="saved filters could not be read")
            base = to_agent_filters(touched.payload)
            source_name = touched.name
        elif name:
            starter = next(
                (s for s in STARTER_PRESETS if s["name"].casefold() == name.casefold()), None
            )
            if starter is None:
                return ToolResult(
                    status="not_found",
                    hint="no starter by that name; call list_filter_presets for the exact names",
                )
            base = dict(starter["filters"])
        else:
            return ToolResult(status="invalid_argument", hint="supply either ref or name")

        overrides = {
            "topics": topics,
            "city": city,
            "location_scope": location_scope,
            "price": price,
            "date_preset": date_preset,
            "q": q,
        }
        merged = {**base, **{k: v for k, v in overrides.items() if v}}
        changed = sorted(k for k, v in overrides.items() if v and base.get(k) != v)
        self.last_run = merged
        return ToolResult(
            status="ok",
            payload={
                "preset": source_name,
                "changed": changed,
                "describes": describe(merged),
                "search_arguments": {k: v for k, v in merged.items() if k != "date_preset" and v},
                "date_preset": merged.get("date_preset", "week"),
            },
            hint=(
                "This resolved the filter but did NOT run it. Call search_events with "
                "search_arguments, converting date_preset into date_from/date_to from today's "
                "date, then tell the user which preset you used and what you changed."
            ),
        )

    async def save_filter_preset(
        self,
        name: str,
        topics: str = "",
        city: str = "",
        location_scope: str = "",
        price: str = "",
        date_preset: str = "",
        q: str = "",
    ) -> ToolResult:
        """Save a named selection. Supplying nothing but a name saves the last filter run."""
        clean = " ".join(name.split()).strip()
        if not clean or len(clean) > MAX_PRESET_NAME:
            return ToolResult(
                status="invalid_argument",
                hint=f"a preset name is required, at most {MAX_PRESET_NAME} characters",
            )
        explicit = {
            "topics": topics,
            "city": city,
            "location_scope": location_scope,
            "price": price,
            "date_preset": date_preset,
            "q": q,
        }
        supplied = {k: v for k, v in explicit.items() if v}
        agent = {**(self.last_run or {}), **supplied} if supplied else dict(self.last_run or {})
        if not agent:
            return ToolResult(
                status="invalid_argument",
                hint="nothing to save: run a search first, or pass the filter fields explicitly",
            )
        try:
            stored = await self._repository.save_filter(
                self._tenant_id, name=clean, payload=to_client_filters(agent)
            )
        except ValueError as error:
            return ToolResult(
                status="conflict",
                hint=(
                    "a preset with that name already exists; call update_filter_preset to "
                    "replace it, or pick another name"
                    if "too many" not in str(error)
                    else "the saved-filter limit is reached; delete one first"
                ),
            )
        except Exception:
            _log.exception("agent_tool_failed", extra={"tool": "save_filter_preset"})
            return ToolResult(status="unavailable", hint="the preset was not saved")
        return ToolResult(
            status="ok",
            payload={"preset": self._card(stored), "rendered": f"{clean} — {describe(agent)}"},
            hint="Echo `rendered` back so the user can correct it, in one short clause.",
        )

    async def update_filter_preset(
        self,
        ref: str,
        name: str = "",
        topics: str = "",
        city: str = "",
        location_scope: str = "",
        price: str = "",
        date_preset: str = "",
        q: str = "",
    ) -> ToolResult:
        """Change a saved selection in place, keeping its ref."""
        saved_filter_id = self.resolve(ref)
        if saved_filter_id is None:
            return ToolResult(
                status="not_found",
                hint=f"'{ref}' is not a preset from this conversation",
            )
        try:
            current = next(
                (
                    row
                    for row in await self._repository.list_filters(self._tenant_id)
                    if row.saved_filter_id == saved_filter_id
                ),
                None,
            )
            if current is None:
                return ToolResult(status="not_found", hint="that preset no longer exists")
            base = to_agent_filters(current.payload)
            overrides = {
                "topics": topics,
                "city": city,
                "location_scope": location_scope,
                "price": price,
                "date_preset": date_preset,
                "q": q,
            }
            merged = {**base, **{k: v for k, v in overrides.items() if v}}
            stored = await self._repository.save_filter(
                self._tenant_id,
                name=" ".join((name or current.name).split()).strip(),
                payload=to_client_filters(merged),
                saved_filter_id=saved_filter_id,
            )
        except ValueError:
            return ToolResult(status="conflict", hint="another preset already uses that name")
        except Exception:
            _log.exception("agent_tool_failed", extra={"tool": "update_filter_preset"})
            return ToolResult(status="unavailable", hint="the preset was not updated")
        return ToolResult(
            status="ok",
            payload={
                "preset": self._card(stored),
                "rendered": f"{stored.name} — {describe(to_agent_filters(stored.payload))}",
            },
        )

    async def delete_filter_preset(self, ref: str) -> ToolResult:
        """Remove a saved selection."""
        saved_filter_id = self.resolve(ref)
        if saved_filter_id is None:
            return ToolResult(status="not_found", hint=f"'{ref}' is not a preset here")
        try:
            removed = await self._repository.delete_filter(self._tenant_id, saved_filter_id)
        except Exception:
            _log.exception("agent_tool_failed", extra={"tool": "delete_filter_preset"})
            return ToolResult(status="unavailable", hint="the preset was not deleted")
        if not removed:
            return ToolResult(status="not_found", hint="that preset no longer exists")
        return ToolResult(status="ok", payload={"deleted": ref})
