"""Feed service: hybrid retrieval -> rerank -> mandatory conflict gate -> lane plan, assembled into a
cursor-paginated personalized feed (the scroll surface, FR-4). Registerable candidates are the ones
the conflict gate did not hard-block; each carries the ordered lane plan the saga will try (FR-5.0)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from ..domain.conflict import BusyBlock, evaluate_conflict
from ..domain.enums import ConflictVerdict, GroupCondition, Lane, Modality, Source
from ..domain.events import CanonicalEvent
from ..domain.policy import SourcePolicy
from ..domain.request import EventRequest, Feed, RankedCandidate
from ..domain.routing import LaneInput, build_lane_plan
from ..ports.calendar import CalendarPort
from ..ports.ranking import RankerPort
from ..ports.repositories import CatalogRepository
from .discovery_results import arrange, eligible, freshness, lifecycle

DEFAULT_DURATION = timedelta(hours=2)
MAX_FEED_OFFSET = 10_000
CANDIDATE_POOL_SIZE = 400

# The modality a source is REGISTERED through (discovery-only sources fall through to handoff).
_REGISTER_MODALITY = {Source.MEETUP: Modality.API, Source.LUMA: Modality.BROWSER}

MembershipResolver = Callable[[Source], GroupCondition]


class FeedService:
    def __init__(
        self,
        catalog: CatalogRepository,
        ranker: RankerPort,
        calendar: CalendarPort,
        source_policies: dict[Source, SourcePolicy],
        *,
        meetup_autojoin: bool = False,
        membership_resolver: MembershipResolver | None = None,
    ) -> None:
        self._catalog = catalog
        self._ranker = ranker
        self._calendar = calendar
        self._policies = source_policies
        self._meetup_autojoin = meetup_autojoin
        self._membership = membership_resolver or (lambda _s: GroupCondition.UNKNOWN)

    async def build_feed(
        self, request: EventRequest, *, limit: int = 25, cursor: str | None = None
    ) -> Feed:
        offset = self._cursor_offset(cursor)
        pool = await self._catalog.retrieve(
            request.constraints, request.intent_embedding, CANDIDATE_POOL_SIZE
        )
        now = datetime.now(UTC)
        pool = [event for event in pool if eligible(event, request.constraints, now)]
        ranked = await self._ranker.rerank(request, pool)
        arranged = arrange(ranked, request.constraints, now=now)

        upcoming = [e for e, _ in ranked if lifecycle(e, now) == "upcoming"]
        busy = await self._free_busy_window(request, upcoming) if upcoming else []
        items: list[RankedCandidate] = []
        for event, score, additional_dates in arranged:
            end = event.end_at or (event.start_at + DEFAULT_DURATION)
            verdict = evaluate_conflict(event.start_at, end, busy)
            items.append(
                RankedCandidate(
                    canonical_event=event,
                    score=score,
                    rationale=self._rationale(event, score, verdict),
                    conflict_verdict=verdict,
                    lane_plan=self.lane_plan(event) if lifecycle(event, now) == "upcoming" else (),
                    additional_dates=additional_dates,
                    discovery_state=lifecycle(event, now),
                    source_freshness=freshness(event, now),
                )
            )

        page = items[offset : offset + limit]
        next_offset = offset + limit
        next_cursor = (
            str(next_offset)
            if next_offset <= MAX_FEED_OFFSET and next_offset < len(items)
            else None
        )
        return Feed(
            request_id=request.request_id,
            items=tuple(page),
            next_cursor=next_cursor,
            signals={
                "candidate_events": float(len(pool)),
                "candidate_limit": float(CANDIDATE_POOL_SIZE),
                "distinct_topics_at_20": float(
                    len({topic for item in items[:20] for topic in item.canonical_event.topics})
                ),
                "distinct_choices_at_20": float(len(items[:20])),
                "distinct_organizers_at_20": float(
                    len(
                        {
                            item.canonical_event.organizer_name.casefold()
                            for item in items[:20]
                            if item.canonical_event.organizer_name
                        }
                    )
                ),
            },
        )

    @staticmethod
    def _cursor_offset(cursor: str | None) -> int:
        if cursor is None:
            return 0
        if (
            not cursor.isascii()
            or not cursor.isdecimal()
            or (len(cursor) > 1 and cursor.startswith("0"))
        ):
            raise ValueError("feed cursor must be a canonical nonnegative decimal")
        offset = int(cursor)
        if offset > MAX_FEED_OFFSET:
            raise ValueError(f"feed cursor cannot exceed {MAX_FEED_OFFSET}")
        return offset

    async def _free_busy_window(
        self, request: EventRequest, events: list[CanonicalEvent]
    ) -> list[BusyBlock]:
        if request.constraints.time_window is not None:
            start = request.constraints.time_window.start
            end = request.constraints.time_window.end
        elif events:
            start = min(e.start_at for e in events)
            end = max((e.end_at or e.start_at + DEFAULT_DURATION) for e in events)
        else:
            return []
        return await self._calendar.free_busy(request.tenant_id, start, end)

    def lane_plan(self, event: CanonicalEvent) -> tuple[Lane, ...]:
        """The ordered lane plan for one event's registerable (source, modality) options (FR-5.0)."""
        inputs = []
        for link in event.source_links:
            modality = _REGISTER_MODALITY.get(link.source, Modality.BROWSER)
            policy = self._policies.get(link.source) or SourcePolicy(source=link.source)
            inputs.append(
                LaneInput(
                    source=link.source,
                    modality=modality,
                    policy=policy,
                    group_condition=self._membership(link.source),
                    meetup_autojoin_enabled=self._meetup_autojoin,
                )
            )
        return build_lane_plan(inputs)

    @staticmethod
    def _rationale(event: CanonicalEvent, score: float, verdict: ConflictVerdict) -> str:
        why = f"match score {score:.2f}"
        if verdict is ConflictVerdict.DEMOTE:
            why += "; near an existing commitment"
        elif verdict is ConflictVerdict.BLOCKED:
            why += "; conflicts with a calendar block"
        sources = ", ".join(sorted({link.source.value for link in event.source_links}))
        return f"{why}; found on {sources}"
