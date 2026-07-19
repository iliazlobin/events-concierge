"""End-to-end slice integration test: intake -> discover -> rank -> register/handoff -> calendar,
exercising both honest-split lanes (autonomous Meetup + free-crawl handoff) against real Postgres."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from events_concierge.adapters.crawl.source import PublicJsonLdSource
from events_concierge.adapters.mock.discovery_policy import MockDiscoveryPolicyReader
from events_concierge.adapters.mock.sources import ConfirmingSource
from events_concierge.adapters.policy.discovery import StoreBackedDiscoveryPolicyGate
from events_concierge.composition import build_container
from events_concierge.config import get_settings
from events_concierge.domain.credentials import Tenant
from events_concierge.domain.enums import GroupCondition, LifecycleState, Modality, Source
from events_concierge.domain.events import CandidateEvent
from events_concierge.domain.ids import calendar_event_id, registration_workflow_id
from events_concierge.domain.policy import SourcePolicy
from events_concierge.slice_demo import run as run_slice_demo

pytestmark = pytest.mark.integration


async def test_documented_slice_demo_replays_against_a_persistent_catalog(
    db: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `make slice` entry point remains meaningful after prior catalog history exists."""
    await run_slice_demo()
    await run_slice_demo()

    output = capsys.readouterr().out
    assert output.count("SLICE OK: autonomous + handoff lanes both exercised end-to-end.") == 2


async def _seed_registration_consent(
    tenant_id: UUID,
    source: Source,
    modality: Modality,
) -> None:
    """Seed owner-recorded P14c evidence; the app role has no grant capability (FR-2.9)."""
    if (source, modality) not in {
        (Source.MEETUP, Modality.API),
        (Source.LUMA, Modality.BROWSER),
    }:
        raise ValueError("fixture consent only supports the closed P14c registration lanes")
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        raise RuntimeError("EC_MIGRATION_URL is required for registration-consent fixtures")
    owner_engine = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner_engine.begin() as connection:
            await connection.execute(
                text("SELECT set_config('app.tenant_id', :tenant_id, true)"),
                {"tenant_id": str(tenant_id)},
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO public.tenant_source_consents (
                        consent_id, tenant_id, source, modality, scope
                    ) VALUES (
                        :consent_id, :tenant_id, :source, :modality, 'registration'
                    )
                    """
                ),
                {
                    "consent_id": uuid4(),
                    "tenant_id": tenant_id,
                    "source": source.value,
                    "modality": modality.value,
                },
            )
    finally:
        await owner_engine.dispose()


async def test_end_to_end_both_lanes(db: None) -> None:
    settings = get_settings()
    start = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3, hours=2)
    tag = uuid4().hex  # keep events unique per run so dedup does not merge across runs

    crawl_ev = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"jazz-{tag}",
        title=f"Jazz Night {tag}",
        start_at=start,
        registration_url="https://example.com/jazz",
        city="New York",
        description="live jazz music free entry",
        is_free=True,
    )
    meetup_ev = CandidateEvent(
        source=Source.MEETUP,
        source_event_id=f"mtg-{tag}",
        title=f"Python Meetup {tag}",
        start_at=start + timedelta(minutes=30),
        registration_url="https://meetup.com/py",
        city="New York",
        description="tech coding meetup",
        is_free=True,
    )

    container = build_container(
        settings,
        discovery_sources=[
            PublicJsonLdSource(user_agent="test", fixture_events=[crawl_ev]),
            ConfirmingSource(Source.MEETUP, fixture_events=[meetup_ev]),
        ],
        register_sources={Source.MEETUP: ConfirmingSource(Source.MEETUP)},
        membership_resolver=lambda s: (
            GroupCondition.MEMBER if s is Source.MEETUP else GroupCondition.UNKNOWN
        ),
        discovery_policy_gate=StoreBackedDiscoveryPolicyGate(
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
        ),
    )

    tenant = Tenant(uuid4(), f"oidc|{tag}", f"{tag}@example.com", f"{tag}@u.concierge.test")
    await container.tenant_repo.add(tenant)
    await _seed_registration_consent(tenant.tenant_id, Source.MEETUP, Modality.API)

    request = await container.parser.parse(tenant.tenant_id, uuid4(), "jazz or a tech meetup")
    canonical = await container.discovery.discover(request.constraints)
    assert len(canonical) == 2  # two distinct events, deduped correctly

    # The catalog is deliberately global and persists across integration invocations, so ranking a
    # short page is not a reliable assertion that newly ingested fixtures appear in its top ten.
    # Feed construction is still exercised here; registration below uses this invocation's exact
    # canonical results to keep the two-lane slice isolated from unrelated catalog history.
    feed = await container.feed.build_feed(request, limit=10)
    assert feed.items

    statuses = {}
    for event in canonical:
        wid = registration_workflow_id(tenant.tenant_id, event.canonical_event_id)
        result = await container.registration.register_event(
            tenant.tenant_id, event, wid, container.feed.lane_plan(event)
        )
        statuses[event.title] = result

    # Meetup member event registered autonomously and wrote the calendar entry with the deterministic id.
    meetup_result = statuses[meetup_ev.title]
    assert meetup_result.status.value == "registered"
    entries = container.calendar.entries(tenant.tenant_id)
    assert len(entries) == 1
    meetup_canonical = next(event for event in canonical if event.title == meetup_ev.title)
    assert entries[0].calendar_event_id == calendar_event_id(
        tenant.tenant_id, meetup_canonical.canonical_event_id
    )
    assert entries[0].private_metadata == {
        "events_concierge.registration_state": "registered",
        "events_concierge.source_event_ids": f'["meetup:{meetup_ev.source_event_id}"]',
    }

    # Free-crawl event routed to a handoff task.
    crawl_result = statuses[crawl_ev.title]
    assert crawl_result.status.value == "handoff"
    assert crawl_result.handoff_task_id is not None

    # Lifecycle persisted at scheduled (re-read via the real workflow id -> idempotent no-op create).
    meetup_wid = registration_workflow_id(tenant.tenant_id, meetup_canonical.canonical_event_id)
    lc = await container.lifecycle_repo.get_or_create(
        tenant.tenant_id, meetup_canonical.canonical_event_id, meetup_wid
    )
    assert lc.state is LifecycleState.SCHEDULED
