"""Default per-source routing matrix (FR-10.2).

The durable action-boundary PDP is seeded from the same ratified values by migration 0057.  This
mapping remains only the feed's advisory lane-planning default; every source mutation re-reads the
owner-controlled PostgreSQL policy snapshot and fails closed (FR-5.9, ADR-004).
"""

from __future__ import annotations

from .domain.enums import Modality, Source
from .domain.policy import SourcePolicy


def default_source_policies() -> dict[Source, SourcePolicy]:
    return {
        # Meetup: API register permitted (member-scoped, on SLA); browser refused by the router.
        Source.MEETUP: SourcePolicy(
            source=Source.MEETUP, automation_allowed={Modality.API: True, Modality.BROWSER: False}
        ),
        # Luma: browser best-effort permitted.
        Source.LUMA: SourcePolicy(
            source=Source.LUMA, automation_allowed={Modality.BROWSER: True, Modality.API: False}
        ),
        # Discovery-only / handoff sources: no autonomous register.
        Source.EVENTBRITE: SourcePolicy(source=Source.EVENTBRITE, automation_allowed={}),
        Source.TICKETMASTER: SourcePolicy(source=Source.TICKETMASTER, automation_allowed={}),
        Source.SERPAPI: SourcePolicy(source=Source.SERPAPI, automation_allowed={}),
        Source.PUBLIC_JSONLD: SourcePolicy(source=Source.PUBLIC_JSONLD, automation_allowed={}),
        # Partiful: disabled by default.
        Source.PARTIFUL: SourcePolicy(
            source=Source.PARTIFUL, automation_allowed={}, quarantined=True
        ),
    }
