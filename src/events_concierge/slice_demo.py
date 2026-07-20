"""End-to-end vertical slice against real Postgres + mocked cloud: intake -> discover -> rank (feed)
-> select -> register/handoff -> calendar -> lifecycle. ``make slice`` provisions and later drops a
fresh migrated database, so the smoke cannot leave demo lifecycle state in the local runtime database.

Demonstrates both honest-split lanes: a Meetup member-group event registers autonomously and lands on
the calendar; a free-crawl event routes to a pre-filled handoff task. Doubles as a smoke test."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from .adapters.crawl.source import PublicJsonLdSource
from .adapters.mock.calendar import MockCalendar
from .adapters.mock.consent import MockRegistrationConsentEvidence
from .adapters.mock.discovery_policy import MockDiscoveryPolicyReader
from .adapters.mock.sources import ConfirmingSource
from .adapters.policy.discovery import StoreBackedDiscoveryPolicyGate
from .composition import Container, build_container
from .config import get_settings
from .domain.credentials import Tenant
from .domain.enums import GroupCondition, Modality, Source
from .domain.events import CandidateEvent
from .domain.ids import registration_workflow_id
from .domain.policy import SourcePolicy
from .domain.request import EventRequest, RankedCandidate, TimeWindow
from .infra.db import dispose_engine
from .infra.logging import configure_logging, get_logger

_log = get_logger("slice")


async def _rank_slice_targets(
    container: Container,
    request: EventRequest,
    target_ids: set[UUID],
) -> dict[UUID, RankedCandidate]:
    """Page the actual scroll until every newly discovered smoke target is ranked."""
    ranked_targets: dict[UUID, RankedCandidate] = {}
    cursor: str | None = None
    rank = 0
    print("\n=== ranked feed (the scroll) ===")
    while True:
        feed = await container.feed.build_feed(request, limit=25, cursor=cursor)
        for item in feed.items:
            rank += 1
            print(
                f"  {rank}. [{item.score:.3f}] {item.canonical_event.title}"
                f"  ({item.conflict_verdict.value};"
                f" lanes={[lane.value for lane in item.lane_plan]})"
            )
            if item.canonical_event.canonical_event_id in target_ids:
                ranked_targets[item.canonical_event.canonical_event_id] = item
        if target_ids.issubset(ranked_targets) or feed.next_cursor is None:
            return ranked_targets
        cursor = feed.next_cursor


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, local=True)

    tag = uuid4().hex
    city = f"New York Slice {tag}"
    start = datetime.now(UTC) + timedelta(days=2, hours=3)
    crawl_ev = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"jazz-{tag}",
        title=f"Friday Jazz Night at Blue Note {tag}",
        start_at=start,
        registration_url=f"https://example.com/events/jazz-{tag}",
        city=city,
        description="live jazz music, free entry",
        is_free=True,
    )
    meetup_ev = CandidateEvent(
        source=Source.MEETUP,
        source_event_id=f"mtg-{tag}",
        title=f"NYC Python Meetup {tag}",
        start_at=start + timedelta(minutes=30),
        registration_url=f"https://meetup.com/nyc-python/events/mtg-{tag}",
        city=city,
        description="tech coding meetup for developers",
        is_free=True,
    )

    crawl_source = PublicJsonLdSource(
        user_agent=settings.crawl_user_agent, fixture_events=[crawl_ev]
    )
    meetup_source = ConfirmingSource(Source.MEETUP, fixture_events=[meetup_ev])

    def membership(source: Source) -> GroupCondition:
        return GroupCondition.MEMBER if source is Source.MEETUP else GroupCondition.UNKNOWN

    calendar = MockCalendar()
    registration_consent = MockRegistrationConsentEvidence()
    discovery_policy_gate = StoreBackedDiscoveryPolicyGate(
        MockDiscoveryPolicyReader(
            {
                Source.PUBLIC_JSONLD: SourcePolicy(
                    source=Source.PUBLIC_JSONLD,
                    automation_allowed={Modality.BROWSER: True},
                ),
                Source.MEETUP: SourcePolicy(
                    source=Source.MEETUP,
                    automation_allowed={Modality.API: True},
                ),
            }
        )
    )
    container = build_container(
        settings,
        discovery_sources=[crawl_source, meetup_source],
        register_sources={Source.MEETUP: meetup_source},
        membership_resolver=membership,
        calendar=calendar,
        registration_consent=registration_consent,
        discovery_policy_gate=discovery_policy_gate,
    )

    tenant_id = uuid4()
    tenant = Tenant(
        tenant_id=tenant_id,
        oidc_subject=f"oidc|slice-{tag}",
        notify_email=f"slice-{tag}@example.com",
        relay_inbox=f"slice-{tag}@u.concierge.test",
    )
    await container.tenant_repo.add(tenant)
    registration_consent.seed(uuid4(), tenant_id, Source.MEETUP, Modality.API)

    request_id = uuid4()
    request = await container.parser.parse(
        tenant.tenant_id, request_id, "find me something fun friday evening, jazz or a tech meetup"
    )
    # Keep the reusable global catalog from swamping this smoke with unrelated history while still
    # preserving the parser's category and intent work. Exact target assertions below ensure the
    # registration leg cannot bypass ranking if this window or the feed ever regresses.
    request.constraints = replace(
        request.constraints,
        time_window=TimeWindow(
            start=start - timedelta(seconds=1),
            end=start + timedelta(hours=3),
        ),
    )
    await container.request_repo.add(request)
    _log.info("parsed request", categories=request.constraints.categories)

    canonical = await container.discovery.discover(request.constraints)
    _log.info("discovered", canonical_events=len(canonical))

    target_ids = {event.canonical_event_id for event in canonical}
    ranked_targets = await _rank_slice_targets(container, request, target_ids)
    assert target_ids == set(ranked_targets), "discovered slice events must survive ranking"
    assert all(item.registerable for item in ranked_targets.values())

    print("\n=== registration outcomes ===")
    registered = handoffs = 0
    for item in ranked_targets.values():
        event = item.canonical_event
        wid = registration_workflow_id(tenant.tenant_id, event.canonical_event_id)
        result = await container.registration.register_event(
            tenant.tenant_id,
            event,
            wid,
            item.lane_plan,
        )
        print(
            f"  {event.title}: {result.status.value} (lane={result.lane.value if result.lane else '-'})"
        )
        if result.calendar_event_id:
            registered += 1
        if result.handoff_task_id:
            handoffs += 1

    cal_entries = calendar.entries(tenant.tenant_id)
    print("\n=== invariants ===")
    print(f"  calendar entries written: {len(cal_entries)}")
    for entry in cal_entries:
        print(f"    - {entry.title}  id={entry.calendar_event_id[:16]}...  tz={entry.time_zone}")

    assert registered >= 1, "expected at least one autonomous registration (Meetup member lane)"
    assert handoffs >= 1, "expected at least one handoff (free-crawl discovery-only source)"
    assert len(cal_entries) == registered, "one calendar entry per confirmed registration"
    print("\nSLICE OK: autonomous + handoff lanes both exercised end-to-end.")

    await dispose_engine()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
