"""End-to-end vertical slice against real Postgres + mocked cloud: intake -> discover -> rank (feed)
-> select -> register/handoff -> calendar -> lifecycle. Run with `make slice` (needs `make up migrate`).

Demonstrates both honest-split lanes: a Meetup member-group event registers autonomously and lands on
the calendar; a free-crawl event routes to a pre-filled handoff task. Doubles as a smoke test."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from .adapters.crawl.source import PublicJsonLdSource
from .adapters.mock.calendar import MockCalendar
from .adapters.mock.discovery_policy import MockDiscoveryPolicyReader
from .adapters.mock.sources import ConfirmingSource
from .adapters.policy.discovery import StoreBackedDiscoveryPolicyGate
from .composition import build_container
from .config import get_settings
from .domain.credentials import Tenant
from .domain.enums import ConflictVerdict, GroupCondition, Modality, Source
from .domain.events import CandidateEvent
from .domain.ids import registration_workflow_id
from .domain.policy import SourcePolicy
from .infra.db import dispose_engine
from .infra.logging import configure_logging, get_logger

_log = get_logger("slice")


async def run() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, local=True)

    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2, hours=3)
    crawl_ev = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id="jazz-1",
        title="Friday Jazz Night at Blue Note",
        start_at=start,
        registration_url="https://example.com/events/jazz",
        city="New York",
        description="live jazz music, free entry",
        is_free=True,
    )
    meetup_ev = CandidateEvent(
        source=Source.MEETUP,
        source_event_id="mtg-1",
        title="NYC Python Meetup",
        start_at=start + timedelta(minutes=30),
        registration_url="https://meetup.com/nyc-python/events/mtg-1",
        city="New York",
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
        discovery_policy_gate=discovery_policy_gate,
    )

    tenant_id = uuid4()
    tag = tenant_id.hex[:8]
    tenant = Tenant(
        tenant_id=tenant_id,
        oidc_subject=f"oidc|slice-{tag}",
        notify_email=f"slice-{tag}@example.com",
        relay_inbox=f"slice-{tag}@u.concierge.test",
    )
    await container.tenant_repo.add(tenant)

    request_id = uuid4()
    request = await container.parser.parse(
        tenant.tenant_id, request_id, "find me something fun friday evening, jazz or a tech meetup"
    )
    await container.request_repo.add(request)
    _log.info("parsed request", categories=request.constraints.categories)

    canonical = await container.discovery.discover(request.constraints)
    _log.info("discovered", canonical_events=len(canonical))

    feed = await container.feed.build_feed(request, limit=10)
    print("\n=== ranked feed (the scroll) ===")
    for i, item in enumerate(feed.items, 1):
        print(
            f"  {i}. [{item.score:.3f}] {item.canonical_event.title}"
            f"  ({item.conflict_verdict.value}; lanes={[lane.value for lane in item.lane_plan]})"
        )

    print("\n=== registration outcomes ===")
    target_titles = {crawl_ev.title, meetup_ev.title}  # this run's events (catalog is persistent)
    registered = handoffs = 0
    for item in feed.items:
        if item.canonical_event.title not in target_titles:
            continue
        if item.conflict_verdict is ConflictVerdict.BLOCKED:
            continue
        event = item.canonical_event
        wid = registration_workflow_id(tenant.tenant_id, event.canonical_event_id)
        result = await container.registration.register_event(
            tenant.tenant_id, event, wid, item.lane_plan
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
