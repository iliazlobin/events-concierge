"""Postgres source-registry and durable-refresh ledger integration tests (FR-10.3/NFR-8)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from events_concierge.adapters.bibliocommons.source import _BIBLIOCOMMONS_PUBLISHERS
from events_concierge.adapters.luma_calendar import reviewed_calendar_api_id
from events_concierge.adapters.postgres.catalog_observations import (
    PostgresCatalogObservationRepository,
)
from events_concierge.adapters.postgres.catalog_paged_promotion import (
    PostgresCatalogPagedRefreshPromoter,
)
from events_concierge.adapters.postgres.catalog_refresh_commit import (
    PostgresCatalogRefreshCommitter,
)
from events_concierge.adapters.postgres.catalog_sources import PostgresCatalogSourceRepository
from events_concierge.application.catalog_refresh import catalog_refresh_lease_seconds
from events_concierge.composition import build_container
from events_concierge.config import get_settings
from events_concierge.domain.catalog_sources import (
    CatalogSource,
    CatalogSourcePage,
    catalog_candidate_content_hash,
    observation_for,
)
from events_concierge.domain.enums import (
    CatalogRefreshClaimOutcome,
    CatalogRefreshRunStatus,
    CatalogSourceMode,
    PriceStatus,
    RegistrationStatus,
    Source,
)
from events_concierge.domain.events import CandidateEvent, EventEntityProfile
from events_concierge.infra.db import system_session_scope

pytestmark = pytest.mark.integration


async def test_entity_profile_database_validator_rejects_search_and_detached_identities(
    db: None,
) -> None:
    async def valid(profiles: list[dict[str, str]]) -> bool:
        async with system_session_scope() as session:
            return bool(
                (
                    await session.execute(
                        text(
                            """
                            SELECT public.fn_event_entity_profiles_valid(
                                CAST(:profiles AS jsonb),
                                :organizer_name,
                                CAST(:host_names AS text[]),
                                CAST(:speaker_names AS text[]),
                                CAST(:partner_names AS text[])
                            )
                            """
                        ),
                        {
                            "profiles": json.dumps(profiles, separators=(",", ":")),
                            "organizer_name": "City Clerk",
                            "host_names": ["Ada Lovelace"],
                            "speaker_names": [],
                            "partner_names": [],
                        },
                    )
                ).scalar_one()
            )

    direct = {
        "name": "Ada Lovelace",
        "role": "host",
        "kind": "person",
        "profile_url": "https://www.linkedin.com/in/ada-lovelace",
    }
    search = {
        **direct,
        "profile_url": (
            "https://www.linkedin.com/search/results/people/?keywords=Ada%20Lovelace"
        ),
    }
    detached = {
        **direct,
        "name": "Private Attendee",
        "profile_url": "https://www.linkedin.com/in/private-attendee",
    }

    assert await valid([direct]) is True
    assert await valid([search]) is False
    assert await valid([detached]) is False


async def test_registry_contains_only_reviewed_bounded_regional_publishers(db: None) -> None:
    """The registry records only reviewed, region-bounded public sources (FR-10.3)."""
    repository = PostgresCatalogSourceRepository()
    approved_luma = await repository.get("luma-sf")
    assert approved_luma is not None
    assert approved_luma.enabled is True
    assert approved_luma.handoff_only is True
    assert approved_luma.seed_url == (
        "https://api.luma.com/discover/get-paginated-events"
        "?discover_place_api_id=discplace-BDj7GNbGlsF7Cka&pagination_limit=25"
    )
    assert approved_luma.display_name == "Luma Bay Area"
    assert approved_luma.approved_origins == (
        "https://api.luma.com",
        "https://api2.luma.com",
    )
    assert approved_luma.allows_url(approved_luma.seed_url) is True
    assert approved_luma.allows_url("https://luma.com/sf") is False
    assert approved_luma.allows_url(
        "https://api2.luma.com/event/get?event_api_id=evt-reviewed"
    ) is True
    assert approved_luma.mode is CatalogSourceMode.LUMA_DISCOVER_JSON
    assert approved_luma.page_limit == 40
    assert approved_luma.source_revision == 2
    # 0117 disabled this calendar believing the Discover cursor superseded it. Discover is a
    # one-event-per-calendar shelf, so it never did; 0163 restored it.
    genai_calendar = await repository.get("luma-genai-sf")
    assert genai_calendar is not None
    assert genai_calendar.enabled is True
    assert genai_calendar.mode is CatalogSourceMode.LUMA_CALENDAR_JSON
    expected_jsonld_seeds: dict[str, str] = {}
    for source_key, seed_url in expected_jsonld_seeds.items():
        approved = await repository.get(source_key)
        assert approved is not None
        assert approved.enabled is True
        assert approved.handoff_only is True
        assert approved.seed_url == seed_url
        assert approved.region == "bay_area_9_county"
        assert approved.mode is CatalogSourceMode.PUBLIC_JSONLD
        assert approved.page_limit == 1


async def test_every_reviewed_luma_calendar_row_resolves_to_a_calendar_identity(
    db: None,
) -> None:
    """The registry, not a Python allowlist, is what admits a Luma host calendar.

    ``LumaCalendarCatalogFetcher`` derives the calendar it walks from ``seed_url`` alone, so a row
    whose seed is not the exact reviewed cursor shape is a source that fails at its first request
    rather than at review.  This pins that registry-to-adapter contract for *every* row at once,
    which is what makes adding a calendar a data change instead of a code change.
    """
    repository = PostgresCatalogSourceRepository()
    refreshable = await repository.list_refreshable(datetime.now(UTC))
    calendars = [
        source
        for source in refreshable
        if source.mode is CatalogSourceMode.LUMA_CALENDAR_JSON
        and not source.source_key.startswith("test-")
    ]

    assert calendars, "the reviewed fleet must retain at least one Luma host calendar"
    for source in calendars:
        assert source.handoff_only is True
        # Both origins: the listing cursor and the detail record the adapter follows for every
        # retained event. A row left on the listing origin alone refuses to run (migration 0169).
        assert source.approved_origins == ("https://api.luma.com", "https://api2.luma.com")
        assert source.allows_url(source.seed_url) is True
        assert source.allows_url(
            "https://api2.luma.com/event/get?event_api_id=evt-reviewed"
        ) is True
        calendar_api_id = reviewed_calendar_api_id(source.seed_url)
        assert calendar_api_id is not None, f"{source.source_key} seed is not the reviewed cursor"
        assert calendar_api_id.startswith("cal-")
        # page_limit is a cliff, not a budget: exceeding it discards the whole refresh, so a
        # reviewed calendar must carry real headroom over the pages it needs today.
        assert source.page_limit >= 20, f"{source.source_key} has too little page headroom"
        # And an UPPER bound, because _lease_seconds_for reserves page_limit * (1 + 25) paced units
        # for a Luma row: past the one-hour lease ceiling it returns None and the source is SKIPPED
        # with no run recorded at all — coverage disappears with no failure to look at.
        lease = catalog_refresh_lease_seconds(source, floor_seconds=300)
        assert lease is not None, (
            f"{source.source_key} page_limit {source.page_limit} exceeds the one-hour lease budget"
        )

    keys = {source.source_key for source in calendars}
    assert len(keys) == len(calendars)
    seeds = {reviewed_calendar_api_id(source.seed_url) for source in calendars}
    assert len(seeds) == len(calendars), "two reviewed rows point at the same calendar"


async def test_every_bibliocommons_row_agrees_with_its_code_pinned_page_cap(db: None) -> None:
    """A BiblioCommons page cap lives in two places and both must say the same number.

    ``_publisher_for_source`` refuses a row whose ``page_limit`` differs from its code profile, so
    raising the reviewed cap in a migration alone does not widen the walk -- it takes the source
    dark *before* its first request, which looks nothing like the cap failure it was meant to fix.
    """
    repository = PostgresCatalogSourceRepository()
    for source_key, publisher in _BIBLIOCOMMONS_PUBLISHERS.items():
        source = await repository.get(source_key)
        if source is None or not source.enabled:
            continue
        assert source.page_limit == publisher.page_limit, (
            f"{source_key} registry cap {source.page_limit} != code cap {publisher.page_limit}"
        )
        if publisher.min_interval_ms is not None:
            assert source.min_interval_ms == publisher.min_interval_ms


async def test_registry_contains_reviewed_new_york_luma_source(db: None) -> None:
    repository = PostgresCatalogSourceRepository()
    approved_nyc = await repository.get("luma-nyc")
    assert approved_nyc is not None
    assert approved_nyc.enabled is True
    assert approved_nyc.handoff_only is True
    assert approved_nyc.display_name == "Luma New York"
    assert approved_nyc.seed_url == (
        "https://api.luma.com/discover/get-paginated-events"
        "?discover_place_api_id=discplace-Izx1rQVSh8njYpP&pagination_limit=25"
    )
    assert approved_nyc.approved_origins == (
        "https://api.luma.com",
        "https://api2.luma.com",
    )
    assert approved_nyc.region == "new_york_metro"
    assert approved_nyc.mode is CatalogSourceMode.LUMA_DISCOVER_JSON
    assert approved_nyc.page_limit == 40
    assert approved_nyc.source_revision == 1


async def test_registry_contains_reviewed_livewhale_sources(db: None) -> None:
    repository = PostgresCatalogSourceRepository()
    expected_livewhale_sources = {
        "berkeley-events": (
            "https://events.berkeley.edu/live/json/events/response_fields/location,summary,description",
            False,
        ),
        "scu-events": (
            "https://events.scu.edu/live/json/events/response_fields/location,summary,description",
            False,
        ),
        "smccd-events": (
            "https://events.smccd.edu/live/json/events/response_fields/location,summary,description",
            False,
        ),
    }
    for source_key, (seed_url, enabled) in expected_livewhale_sources.items():
        approved = await repository.get(source_key)
        assert approved is not None
        assert approved.enabled is enabled
        assert approved.handoff_only is True
        assert approved.seed_url == seed_url
        assert approved.region == "bay_area_9_county"
        assert approved.mode is CatalogSourceMode.LIVEWHALE_JSON
        assert approved.page_limit == 3

    expected_typed_sources = {
        "sf-gov-related-events": (
            "https://api.sf.gov/api/related-events/?list=upcoming&locale=en&page=1&groupby=date",
            ("https://api.sf.gov",),
            CatalogSourceMode.SF_GOV_JSON,
            25,
        ),
        "datasf-our415-events": (
            "https://data.sfgov.org/resource/8i3s-ih2a.json",
            ("https://data.sfgov.org",),
            CatalogSourceMode.DATASF_OUR415,
            25,
        ),
        "sccld-milpitas-events": (
            "https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?locations=MI",
            ("https://gateway.bibliocommons.com",),
            CatalogSourceMode.BIBLIOCOMMONS_RSS,
            15,
        ),
        "sccld-saratoga-events": (
            "https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?locations=SA",
            ("https://gateway.bibliocommons.com",),
            CatalogSourceMode.BIBLIOCOMMONS_RSS,
            15,
        ),
        "sccld-all-physical-branches-events": (
            "https://gateway.bibliocommons.com/v2/libraries/sccl/rss/events?"
            "locations=CA&locations=CU&locations=GI&locations=LA&locations=MI&locations=MH&"
            "locations=SA&locations=WO",
            ("https://gateway.bibliocommons.com",),
            CatalogSourceMode.BIBLIOCOMMONS_RSS,
            # Raised from 50 by migration 0167 after the feed outgrew that cap and went dark.
            120,
        ),
        "palo-alto-library-events": (
            "https://gateway.bibliocommons.com/v2/libraries/paloalto/rss/events",
            ("https://gateway.bibliocommons.com",),
            CatalogSourceMode.BIBLIOCOMMONS_RSS,
            15,
        ),
        "smcl-millbrae-events": (
            "https://gateway.bibliocommons.com/v2/libraries/smcl/rss/events?locations=1M",
            ("https://gateway.bibliocommons.com",),
            CatalogSourceMode.BIBLIOCOMMONS_RSS,
            15,
        ),
        "smcl-all-physical-branches-events": (
            "https://gateway.bibliocommons.com/v2/libraries/smcl/rss/events?"
            "locations=1A&locations=1B&locations=1R&locations=1E&locations=1F&locations=1H&"
            "locations=1M&locations=1N&locations=1Z&locations=1P&locations=1V&locations=1S&"
            "locations=1W",
            ("https://gateway.bibliocommons.com",),
            CatalogSourceMode.BIBLIOCOMMONS_RSS,
            150,
        ),
        "alameda-county-library-all-physical-branches-events": (
            "https://gateway.bibliocommons.com/v2/libraries/aclibrary/rss/events?"
            "locations=ALB&locations=CSV&locations=CTV&locations=CHY&locations=DUB&locations=FRM&"
            "locations=NWK&locations=NLS&locations=SLZ&locations=UCY",
            ("https://gateway.bibliocommons.com",),
            CatalogSourceMode.BIBLIOCOMMONS_RSS,
            40,
        ),
        "alameda-county-library-fremont-events": (
            "https://gateway.bibliocommons.com/v2/libraries/aclibrary/rss/events?locations=FRM",
            ("https://gateway.bibliocommons.com",),
            CatalogSourceMode.BIBLIOCOMMONS_RSS,
            7,
        ),
        "oakland-public-library-events": (
            "https://gateway.bibliocommons.com/v2/libraries/oaklandlibrary/rss/events?"
            "locations=81A&locations=AAA&locations=ASA&locations=BRA&locations=CCA&locations=DMA&"
            "locations=EAA&locations=ELA&locations=GGA&locations=KGA&locations=LVA&locations=MEA&"
            "locations=MOA&locations=OHR&locations=PMA&locations=RRA&locations=TMA&locations=WAS&"
            "locations=XXA&locations=XXJ&locations=XXY&locations=676de3ef74596c36004dc6bd&"
            "locations=6a0c99a4e8af4a2f00739408&locations=6a517185e9de6536001ad4d7",
            ("https://gateway.bibliocommons.com",),
            CatalogSourceMode.BIBLIOCOMMONS_RSS,
            60,
        ),
        "san-jose-public-library-events": (
            "https://gateway.bibliocommons.com/v2/libraries/sjpl/rss/events?"
            "locations=00&locations=01&locations=02&locations=03&locations=04&locations=05&"
            "locations=06&locations=07&locations=08&locations=09&locations=10&locations=11&"
            "locations=12&locations=14&locations=15&locations=16&locations=17&locations=18&"
            "locations=19&locations=21&locations=22&locations=23&locations=24&locations=25&"
            "locations=26",
            ("https://gateway.bibliocommons.com",),
            CatalogSourceMode.BIBLIOCOMMONS_RSS,
            # Raised from 160 by migration 0167 after the feed outgrew that cap and went dark.
            320,
        ),
        "contra-costa-county-library-events": (
            "https://gateway.bibliocommons.com/v2/libraries/ccclib/rss/events?"
            "locations=43&locations=7&locations=25&locations=19&locations=14&locations=60&"
            "locations=4&locations=21&locations=26&locations=24&locations=11&locations=16&"
            "locations=6&locations=55&locations=9&locations=94&locations=1&locations=17&"
            "locations=12&locations=15&locations=27&locations=23&locations=8&locations=2&"
            "locations=13",
            ("https://gateway.bibliocommons.com",),
            CatalogSourceMode.BIBLIOCOMMONS_RSS,
            105,
        ),
        "marin-county-free-library-events": (
            "https://gateway.bibliocommons.com/v2/libraries/marinlibrary/rss/events?"
            "locations=MB&locations=MC&locations=MM&locations=MF&locations=MI&locations=MA&"
            "locations=MN&locations=MP&locations=MH&locations=MS",
            ("https://gateway.bibliocommons.com",),
            CatalogSourceMode.BIBLIOCOMMONS_RSS,
            25,
        ),
        "san-jose-legistar-meetings": (
            "https://webapi.legistar.com/v1/SanJose/Events",
            ("https://webapi.legistar.com",),
            CatalogSourceMode.SAN_JOSE_LEGISTAR,
            5,
        ),
        "sunnyvale-legistar-meetings": (
            "https://webapi.legistar.com/v1/SunnyvaleCA/Events",
            ("https://webapi.legistar.com",),
            CatalogSourceMode.SUNNYVALE_LEGISTAR,
            5,
        ),
        "alameda-legistar-meetings": (
            "https://webapi.legistar.com/v1/Alameda/Events",
            ("https://webapi.legistar.com",),
            CatalogSourceMode.ALAMEDA_LEGISTAR,
            5,
        ),
        "oakland-legistar-meetings": (
            "https://webapi.legistar.com/v1/Oakland/Events",
            ("https://webapi.legistar.com",),
            CatalogSourceMode.OAKLAND_LEGISTAR,
            5,
        ),
        "berkeley-public-library-events": (
            "https://berkeleypubliclibrary.libnet.info/eeventcaldata",
            ("https://berkeleypubliclibrary.libnet.info",),
            CatalogSourceMode.COMMUNICO_JSON,
            1,
        ),
        "omca-events": (
            "https://museumca.org/wp-json/tribe/events/v1/events",
            ("https://museumca.org",),
            CatalogSourceMode.TRIBE_EVENTS_JSON,
            5,
        ),
        "gardens-golden-gate-park-events": (
            "https://gggp.org/wp-json/tribe/events/v1/events",
            ("https://gggp.org",),
            CatalogSourceMode.TRIBE_EVENTS_JSON,
            5,
        ),
        "stanford-events": (
            "https://events.stanford.edu/api/2/events",
            ("https://events.stanford.edu",),
            CatalogSourceMode.LOCALIST_JSON,
            20,
        ),
        "sjsu-events": (
            "https://events.sjsu.edu/api/2/events",
            ("https://events.sjsu.edu",),
            CatalogSourceMode.LOCALIST_JSON,
            20,
        ),
        "ucsf-events": (
            "https://calendar.ucsf.edu/api/2/events",
            ("https://calendar.ucsf.edu",),
            CatalogSourceMode.LOCALIST_JSON,
            20,
        ),
        "mountain-view-library-events": (
            "https://mountainview.libcal.com/ical_subscribe.php?src=p&cid=8800",
            ("https://mountainview.libcal.com",),
            CatalogSourceMode.LIBCAL_ICS,
            1,
        ),
        "sf-rec-park-events": (
            "https://sfrecpark.org/RSSFeed.aspx?CID=Main-Calendar-14&ModID=58",
            ("https://sfrecpark.org",),
            CatalogSourceMode.CIVIC_ENGAGE_RSS,
            1,
        ),
        "campbell-events": (
            "https://www.campbellca.gov/RSSFeed.aspx?CID=Recreation-Community-Services-29&ModID=58",
            ("https://www.campbellca.gov",),
            CatalogSourceMode.CIVIC_ENGAGE_RSS,
            1,
        ),
        "los-altos-events": (
            "https://www.losaltosca.gov/RSSFeed.aspx?CID=All-calendar.xml&ModID=58",
            ("https://www.losaltosca.gov",),
            CatalogSourceMode.CIVIC_ENGAGE_RSS,
            1,
        ),
        "midpen-events": (
            "https://www.openspace.org/get-involved/events-activities?page=0",
            ("https://www.openspace.org",),
            CatalogSourceMode.MIDPEN_HTML,
            9,
        ),
        "usfca-main-campus-events": (
            "https://www.usfca.edu/life-at-usf/events?field_campus%5B179%5D=179",
            ("https://www.usfca.edu",),
            CatalogSourceMode.USFCA_HTML,
            1,
        ),
        "calperformances-events": (
            "https://calperformances.org/wp-json/wp/v2/cp_event?per_page=100&page=1",
            ("https://calperformances.org",),
            CatalogSourceMode.CAL_PERFORMANCES_JSON,
            4,
        ),
        "berkeley-rep-shows": (
            "https://www.berkeleyrep.org/shows",
            ("https://www.berkeleyrep.org", "https://tickets.berkeleyrep.org"),
            CatalogSourceMode.BERKELEY_REP_HTML,
            1,
        ),
        "ybca-calendar": (
            "https://ybca.org/calendar/",
            ("https://ybca.org",),
            CatalogSourceMode.YBCA_HTML,
            1,
        ),
        "oakland-city-events": (
            "https://www.oaklandca.gov/sitemap.xml",
            ("https://www.oaklandca.gov",),
            CatalogSourceMode.OAKLAND_HTML,
            160,
        ),
    }
    for source_key, (seed_url, origins, mode, page_limit) in expected_typed_sources.items():
        approved = await repository.get(source_key)
        assert approved is not None
        _assert_catalog_source_activation(source_key, approved)
        assert approved.handoff_only is True
        assert approved.seed_url == seed_url
        assert approved.approved_origins == origins
        assert approved.region == "bay_area_9_county"
        assert approved.mode is mode
        assert approved.page_limit == page_limit

    await _assert_special_catalog_source_controls(repository)


async def _assert_special_catalog_source_controls(
    repository: PostgresCatalogSourceRepository,
) -> None:
    """Verify source-specific cadence and policy fences outside the main registry table (FR-10.3)."""
    ybca = await repository.get("ybca-calendar")
    assert ybca is not None
    assert ybca.min_interval_ms == 10_000

    oakland = await repository.get("oakland-city-events")
    assert oakland is not None
    assert oakland.refresh_interval_minutes == 1_440
    assert oakland.min_interval_ms == 5_000

    marin = await repository.get("marin-county-free-library-events")
    assert marin is not None
    _assert_marin_registry_controls(marin)

    smcl_physical = await repository.get("smcl-all-physical-branches-events")
    assert smcl_physical is not None
    _assert_smcl_physical_registry_controls(smcl_physical)

    sccld_physical = await repository.get("sccld-all-physical-branches-events")
    assert sccld_physical is not None
    _assert_sccld_physical_registry_controls(sccld_physical)

    alameda_physical = await repository.get("alameda-county-library-all-physical-branches-events")
    assert alameda_physical is not None
    _assert_alameda_physical_registry_controls(alameda_physical)

    alameda_legistar = await repository.get("alameda-legistar-meetings")
    assert alameda_legistar is not None
    _assert_alameda_legistar_registry_controls(alameda_legistar)

    oakland_legistar = await repository.get("oakland-legistar-meetings")
    assert oakland_legistar is not None
    _assert_oakland_legistar_registry_controls(oakland_legistar)


def _assert_marin_registry_controls(marin: CatalogSource) -> None:
    """Keep the reviewed Marin seed's cadence and policy fence exact (FR-3.1/FR-10.3)."""
    assert marin.display_name == "Marin County Free Library Events"
    assert marin.publisher == "Marin County Free Library"
    assert marin.handoff_only is True
    assert marin.enabled is True
    assert marin.reviewed_at is not None
    assert marin.review_expires_at is None
    assert marin.refresh_interval_minutes == 360
    assert marin.min_interval_ms == 5_000


def _assert_catalog_source_activation(source_key: str, source: CatalogSource) -> None:
    """Keep superseded and authorization-held registry sources from resuming egress (FR-10.3/NFR-8)."""
    if source_key in {
        "alameda-county-library-fremont-events",
        "sccld-milpitas-events",
        "sccld-saratoga-events",
        "smcl-millbrae-events",
        "omca-events",
        "gardens-golden-gate-park-events",
        "los-altos-events",
        "midpen-events",
        "berkeley-public-library-events",
        "usfca-main-campus-events",
        "calperformances-events",
        "berkeley-rep-shows",
        "ybca-calendar",
        "sf-gov-related-events",
        "datasf-our415-events",
        "stanford-events",
        "sjsu-events",
        "ucsf-events",
    }:
        assert source.enabled is False
        return
    assert source.enabled is True


def _assert_smcl_physical_registry_controls(smcl_physical: CatalogSource) -> None:
    """Keep the reviewed SMCL physical-branch seed's policy fence exact (FR-3.1/FR-10.3)."""
    assert smcl_physical.display_name == "San Mateo County Libraries: All Physical Branch Events"
    assert smcl_physical.publisher == "San Mateo County Libraries"
    assert smcl_physical.handoff_only is True
    assert smcl_physical.enabled is True
    assert smcl_physical.reviewed_at is not None
    assert smcl_physical.review_expires_at is None
    assert smcl_physical.refresh_interval_minutes == 360
    assert smcl_physical.min_interval_ms == 5_000


def _assert_sccld_physical_registry_controls(sccld_physical: CatalogSource) -> None:
    """Keep the reviewed SCCLD physical-branch seed's policy fence exact (FR-3.1/FR-10.3)."""
    assert (
        sccld_physical.display_name
        == "Santa Clara County Library District: All Physical Branch Events"
    )
    assert sccld_physical.publisher == "Santa Clara County Library District"
    assert sccld_physical.handoff_only is True
    assert sccld_physical.enabled is True
    assert sccld_physical.reviewed_at is not None
    assert sccld_physical.review_expires_at is None
    assert sccld_physical.refresh_interval_minutes == 360
    assert sccld_physical.min_interval_ms == 5_000


def _assert_alameda_physical_registry_controls(alameda_physical: CatalogSource) -> None:
    """Keep the reviewed Alameda physical-branch seed's policy fence exact (FR-3.1/FR-10.3)."""
    assert alameda_physical.display_name == "Alameda County Library: All Physical Branch Events"
    assert alameda_physical.publisher == "Alameda County Library"
    assert alameda_physical.handoff_only is True
    assert alameda_physical.enabled is True
    assert alameda_physical.reviewed_at is not None
    assert alameda_physical.review_expires_at is None
    assert alameda_physical.refresh_interval_minutes == 360
    assert alameda_physical.min_interval_ms == 5_000


def _assert_alameda_legistar_registry_controls(alameda_legistar: CatalogSource) -> None:
    """Keep Alameda's reviewed municipal meeting contract exact (FR-3.1/FR-10.3/FR-10.4)."""
    assert alameda_legistar.display_name == "Alameda Public Meetings"
    assert alameda_legistar.publisher == "City of Alameda City Clerk"
    assert alameda_legistar.handoff_only is True
    assert alameda_legistar.enabled is True
    assert alameda_legistar.reviewed_at is not None
    assert alameda_legistar.review_expires_at is None
    assert alameda_legistar.refresh_interval_minutes == 360
    assert alameda_legistar.min_interval_ms == 1_500


def _assert_oakland_legistar_registry_controls(oakland_legistar: CatalogSource) -> None:
    """Keep Oakland's reviewed municipal meeting contract exact (FR-3.1/FR-10.3/FR-10.4)."""
    assert oakland_legistar.display_name == "Oakland Public Meetings"
    assert oakland_legistar.publisher == "City of Oakland City Clerk"
    assert oakland_legistar.handoff_only is True
    assert oakland_legistar.enabled is True
    assert oakland_legistar.reviewed_at is not None
    assert oakland_legistar.review_expires_at is None
    assert oakland_legistar.refresh_interval_minutes == 360
    assert oakland_legistar.min_interval_ms == 1_500


async def test_catalog_refresh_control_plane_is_capability_only_for_app_role(db: None) -> None:
    """The runtime can read reviewed seeds, not mutate them or inspect durable lease rows (NFR-8)."""
    async with system_session_scope() as session:
        privileges = dict(
            (
                await session.execute(
                    text(
                        """
                        SELECT has_table_privilege(
                                   current_user,
                                   'public.catalog_sources',
                                   'SELECT'
                               ) AS source_read_access,
                               has_table_privilege(
                                   current_user,
                                   'public.catalog_sources',
                                   'INSERT,UPDATE,DELETE'
                               ) AS source_write_access,
                               has_table_privilege(
                                   current_user,
                                   'public.catalog_refresh_runs',
                                   'SELECT,INSERT,UPDATE,DELETE'
                               ) AS refresh_table_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_claim_catalog_refresh(text,text,integer,uuid)',
                                   'EXECUTE'
                               ) AS claim_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_has_live_catalog_refresh_lease(text,text,uuid)',
                                   'EXECUTE'
                               ) AS live_lease_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_complete_catalog_refresh(text,text,uuid,integer,integer)',
                                   'EXECUTE'
                               ) AS complete_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_fail_catalog_refresh(text,text,uuid,text)',
                                   'EXECUTE'
                               ) AS fail_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_list_due_catalog_refreshes(timestamptz,integer)',
                                   'EXECUTE'
                               ) AS due_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_get_catalog_refresh_run(text,text)',
                                   'EXECUTE'
                               ) AS run_access
                        """
                    )
                )
            )
            .mappings()
            .one()
        )

    assert privileges == {
        "source_read_access": True,
        "source_write_access": False,
        "refresh_table_access": False,
        "claim_access": True,
        "live_lease_access": True,
        "complete_access": True,
        "fail_access": True,
        "due_access": True,
        "run_access": True,
    }

    statements = (
        text("INSERT INTO public.catalog_sources (source_key) VALUES ('catalog-source-denied')"),
        text("UPDATE public.catalog_sources SET enabled = false"),
        text("DELETE FROM public.catalog_sources"),
        text("SELECT * FROM public.catalog_refresh_runs"),
        text(
            """
            INSERT INTO public.catalog_refresh_runs
                (source_key, run_key, status, started_at, attempt_count)
            VALUES ('catalog-source-denied', 'manual:denied', 'running', clock_timestamp(), 0)
            """
        ),
        text("UPDATE public.catalog_refresh_runs SET attempt_count = attempt_count + 1"),
        text("DELETE FROM public.catalog_refresh_runs"),
    )
    for statement in statements:
        with pytest.raises(Exception, match="permission denied"):
            async with system_session_scope() as session:
                await session.execute(statement)

    async with system_session_scope() as session:
        malformed_claim = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_claim_catalog_refresh(
                        NULL::text, NULL::text, 0, NULL::uuid
                    )
                    """
                )
            )
        ).scalar_one()
        malformed_live_lease = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_has_live_catalog_refresh_lease(
                        NULL::text, NULL::text, NULL::uuid
                    )
                    """
                )
            )
        ).scalar_one()
        malformed_complete = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_complete_catalog_refresh(
                        NULL::text, NULL::text, NULL::uuid, -1, -1
                    )
                    """
                )
            )
        ).scalar_one()
        malformed_fail = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_fail_catalog_refresh(
                        NULL::text, NULL::text, NULL::uuid, NULL::text
                    )
                    """
                )
            )
        ).scalar_one()
        malformed_due = (
            await session.execute(
                text("SELECT * FROM public.fn_list_due_catalog_refreshes(NULL, 0)")
            )
        ).all()
        malformed_run = (
            await session.execute(
                text("SELECT * FROM public.fn_get_catalog_refresh_run(NULL, NULL)")
            )
        ).all()

    assert malformed_claim == "invalid"
    assert malformed_live_lease is False
    assert malformed_complete is False
    assert malformed_fail is False
    assert malformed_due == []
    assert malformed_run == []


async def test_p15b_paged_catalog_control_plane_is_capability_only_and_reclaims_a_staged_page(
    db: None,
) -> None:
    """P15b pages are only staged/paused through fixed functions, never raw app-role DML (NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    source = await repository.get("san-jose-legistar-meetings")
    assert source is not None
    assert source.source_revision >= 1
    run_key = f"manual:p15b-{uuid4().hex}"
    remote_id = str(10**12 + (uuid4().int % 10**11))
    candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"p15b:{remote_id}",
        title="P15b staged civic meeting",
        start_at=datetime.now(UTC) + timedelta(days=30),
        registration_url=f"https://sanjose.legistar.com/MeetingDetail.aspx?LEGID={remote_id}",
        venue_name="City Hall",
    )

    async with system_session_scope() as session:
        privileges = dict(
            (
                await session.execute(
                    text(
                        """
                        SELECT has_table_privilege(
                                   current_user,
                                   'public.catalog_refresh_progress',
                                   'SELECT,INSERT,UPDATE,DELETE'
                               ) AS progress_access,
                               has_table_privilege(
                                   current_user,
                                   'public.catalog_refresh_stage_pages',
                                   'SELECT,INSERT,UPDATE,DELETE'
                               ) AS pages_access,
                               has_table_privilege(
                                   current_user,
                                   'public.catalog_refresh_stage_candidates',
                                   'SELECT,INSERT,UPDATE,DELETE'
                               ) AS candidates_access,
                               has_table_privilege(
                                   current_user,
                                   'public.catalog_refresh_stage_event_ids',
                                   'SELECT,INSERT,UPDATE,DELETE'
                               ) AS event_ids_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_prepare_paged_catalog_refresh(text,text,uuid,integer)',
                                   'EXECUTE'
                               ) AS prepare_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_stage_paged_catalog_refresh_page(text,text,uuid,integer,integer,integer,jsonb,jsonb)',
                                   'EXECUTE'
                               ) AS legacy_stage_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_stage_paged_catalog_refresh_page_v2(text,text,uuid,integer,integer,integer,jsonb,jsonb)',
                                   'EXECUTE'
                               ) AS stage_v2_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_stage_paged_catalog_refresh_page_v3(text,text,uuid,integer,integer,integer,jsonb,jsonb)',
                                   'EXECUTE'
                               ) AS stage_v3_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_stage_paged_catalog_refresh_page_v4(text,text,uuid,integer,integer,integer,jsonb,jsonb)',
                                   'EXECUTE'
                               ) AS stage_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_read_paged_catalog_refresh_stage(text,text,uuid,integer)',
                                   'EXECUTE'
                               ) AS legacy_read_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_read_paged_catalog_refresh_stage_v2(text,text,uuid,integer)',
                                   'EXECUTE'
                               ) AS read_v2_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_read_paged_catalog_refresh_stage_v3(text,text,uuid,integer)',
                                   'EXECUTE'
                               ) AS read_v3_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_read_paged_catalog_refresh_stage_v4(text,text,uuid,integer)',
                                   'EXECUTE'
                               ) AS read_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_pause_paged_catalog_refresh(text,text,uuid,text)',
                                   'EXECUTE'
                               ) AS pause_access,
                               has_function_privilege(
                                   current_user,
                                   'public.fn_abort_paged_catalog_refresh(text,text,uuid,text)',
                                   'EXECUTE'
                               ) AS abort_access
                        """
                    )
                )
            )
            .mappings()
            .one()
        )
    assert privileges == {
        "progress_access": False,
        "pages_access": False,
        "candidates_access": False,
        "event_ids_access": False,
        "prepare_access": True,
        "legacy_stage_access": False,
        "stage_v2_access": True,
        "stage_v3_access": True,
        "stage_access": True,
        "legacy_read_access": False,
        "read_v2_access": True,
        "read_v3_access": True,
        "read_access": True,
        "pause_access": True,
        "abort_access": True,
    }
    for statement in (
        text("SELECT * FROM public.catalog_refresh_progress"),
        text("SELECT * FROM public.catalog_refresh_stage_pages"),
        text("SELECT * FROM public.catalog_refresh_stage_candidates"),
        text("SELECT * FROM public.catalog_refresh_stage_event_ids"),
    ):
        with pytest.raises(Exception, match="permission denied"):
            async with system_session_scope() as session:
                await session.execute(statement)

    first = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert first.lease_token is not None
    prepared = await repository.prepare_paged_refresh(
        source.source_key,
        run_key,
        lease_token=first.lease_token,
        source_revision=source.source_revision,
    )
    assert prepared.status == "ready"
    assert prepared.progress is not None
    assert prepared.progress.next_page == 0
    staged = await repository.stage_paged_page(
        source.source_key,
        run_key,
        lease_token=first.lease_token,
        source_revision=source.source_revision,
        page=CatalogSourcePage(0, 100, (candidate,), (remote_id,)),
    )
    assert staged.status == "more"
    paused = await repository.get_refresh_run(source.source_key, run_key)
    assert paused is not None
    assert paused.status is CatalogRefreshRunStatus.PAUSED

    second = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert second.lease_token is not None
    prepared_again = await repository.prepare_paged_refresh(
        source.source_key,
        run_key,
        lease_token=second.lease_token,
        source_revision=source.source_revision,
    )
    assert prepared_again.status == "ready"
    assert prepared_again.progress is not None
    assert prepared_again.progress.next_page == 1
    terminal = await repository.stage_paged_page(
        source.source_key,
        run_key,
        lease_token=second.lease_token,
        source_revision=source.source_revision,
        page=CatalogSourcePage(1, 0, (), ()),
    )
    assert terminal.status == "terminal"
    assert (
        await repository.complete_refresh(
            source.source_key,
            run_key,
            lease_token=second.lease_token,
            candidate_count=0,
            canonical_count=0,
        )
        is False
    )
    assert (
        await repository.abort_paged_refresh(
            source.source_key,
            run_key,
            lease_token=second.lease_token,
            error="fixture cleanup",
        )
        is True
    )


async def test_p15b_terminal_stage_promotes_catalog_observation_and_run_in_one_transaction(
    db: None,
) -> None:
    """A terminal normalized page creates one catalog/provenance effect before run success (NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    source = await repository.get("san-jose-legistar-meetings")
    assert source is not None
    run_key = f"manual:p15b-promote-{uuid4().hex}"
    remote_id = str(2 * 10**12 + (uuid4().int % 10**11))
    start_at = datetime.now(ZoneInfo("America/Los_Angeles")) + timedelta(days=60)
    candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"p15b-promoted:{remote_id}",
        title="P15b promoted civic meeting",
        start_at=start_at,
        end_at=start_at + timedelta(hours=2),
        registration_url=f"https://sanjose.legistar.com/MeetingDetail.aspx?LEGID={remote_id}",
        venue_name="City Hall",
        city="San Jose",
        price_status=PriceStatus.PAID,
        price_min_cents=1_500,
        price_max_cents=3_000,
        price_currency="usd",
        organizer_name="Office of the City Clerk",
        host_names=("Planning Commission",),
        speaker_names=("Jordan Lee",),
        partner_names=("Department of Transportation",),
        entity_profiles=(
            EventEntityProfile(
                name="Office of the City Clerk",
                role="organizer",
                kind="organization",
                profile_url="https://www.sanjoseca.gov/city-clerk",
            ),
            EventEntityProfile(
                name="Jordan Lee",
                role="speaker",
                kind="person",
                profile_url="https://www.linkedin.com/in/jordan-lee",
            ),
        ),
        attendance_count=184,
        registration_status=RegistrationStatus.OPEN,
    )
    claim = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert claim.lease_token is not None
    prepared = await repository.prepare_paged_refresh(
        source.source_key,
        run_key,
        lease_token=claim.lease_token,
        source_revision=source.source_revision,
    )
    assert prepared.status == "ready"
    terminal = await repository.stage_paged_page(
        source.source_key,
        run_key,
        lease_token=claim.lease_token,
        source_revision=source.source_revision,
        page=CatalogSourcePage(0, 1, (candidate,), (remote_id,)),
    )
    assert terminal.status == "terminal"

    container = build_container(get_settings())
    promoter = PostgresCatalogPagedRefreshPromoter(
        container.catalog,
        container.catalog_observation_repo,
    )
    promoted = await promoter.promote_paged_refresh(
        source.source_key,
        run_key,
        lease_token=claim.lease_token,
        source_revision=source.source_revision,
    )

    assert (promoted.candidate_count, promoted.canonical_count) == (1, 1)
    completed = await repository.get_refresh_run(source.source_key, run_key)
    assert completed is not None
    assert completed.status is CatalogRefreshRunStatus.SUCCEEDED
    assert (completed.candidate_count, completed.canonical_count) == (1, 1)
    observations = await PostgresCatalogObservationRepository().list_for_source(source.source_key)
    observation = next(
        item for item in observations if item.source_event_id == candidate.source_event_id
    )
    assert observation.last_run_key == run_key
    assert observation.content_hash == catalog_candidate_content_hash(candidate)
    assert (
        observation.price_min_cents,
        observation.price_max_cents,
        observation.price_currency,
    ) == (1_500, 3_000, "USD")
    async with system_session_scope() as session:
        enrichment = (
            await session.execute(
                text(
                    """
                    SELECT price_min_cents, price_max_cents, price_currency,
                           organizer_name, host_names, speaker_names, partner_names,
                           entity_profiles, attendance_count, registration_status
                    FROM public.canonical_events
                    WHERE canonical_event_id = :canonical_event_id
                    """
                ),
                {"canonical_event_id": observation.canonical_event_id},
            )
        ).one()
    assert enrichment.organizer_name == candidate.organizer_name
    assert (
        enrichment.price_min_cents,
        enrichment.price_max_cents,
        enrichment.price_currency,
    ) == (1_500, 3_000, "USD")
    assert tuple(enrichment.host_names) == candidate.host_names
    assert tuple(enrichment.speaker_names) == candidate.speaker_names
    assert tuple(enrichment.partner_names) == candidate.partner_names
    assert enrichment.entity_profiles == [
        profile.as_payload() for profile in candidate.entity_profiles
    ]
    assert enrichment.attendance_count == candidate.attendance_count
    assert enrichment.registration_status == candidate.registration_status.value


async def test_p15b_source_revision_change_discards_a_prior_page_stage_before_reclaim(
    db: None,
) -> None:
    """An owner request-contract edit cannot combine page zero with a new Legistar revision."""
    repository = PostgresCatalogSourceRepository()
    source = await repository.get("san-jose-legistar-meetings")
    assert source is not None
    run_key = f"manual:p15b-revision-{uuid4().hex}"
    remote_id = str(3 * 10**12 + (uuid4().int % 10**11))
    candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"p15b-revision:{remote_id}",
        title="P15b revision fixture meeting",
        start_at=datetime.now(UTC) + timedelta(days=75),
        registration_url=f"https://sanjose.legistar.com/MeetingDetail.aspx?LEGID={remote_id}",
        venue_name="City Hall",
    )
    first = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert first.lease_token is not None
    prepared = await repository.prepare_paged_refresh(
        source.source_key,
        run_key,
        lease_token=first.lease_token,
        source_revision=source.source_revision,
    )
    assert prepared.status == "ready"
    staged = await repository.stage_paged_page(
        source.source_key,
        run_key,
        lease_token=first.lease_token,
        source_revision=source.source_revision,
        page=CatalogSourcePage(0, 100, (candidate,), (remote_id,)),
    )
    assert staged.status == "more"

    try:
        await _set_owner_source_min_interval(source.source_key, source.min_interval_ms + 1)
        revised = await repository.get(source.source_key)
        assert revised is not None
        assert revised.source_revision > source.source_revision
        second = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
        assert second.lease_token is not None
        reset = await repository.prepare_paged_refresh(
            source.source_key,
            run_key,
            lease_token=second.lease_token,
            source_revision=revised.source_revision,
        )
        assert reset.status == "ready"
        assert reset.progress is not None
        assert reset.progress.next_page == 0
        assert reset.progress.staged_raw_count == 0
        assert reset.progress.staged_candidate_count == 0
        assert (
            await repository.abort_paged_refresh(
                source.source_key,
                run_key,
                lease_token=second.lease_token,
                error="fixture cleanup after source revision reset",
            )
            is True
        )
    finally:
        await _set_owner_source_min_interval(source.source_key, source.min_interval_ms)


async def test_p15e_prepare_capability_admits_only_the_four_exact_reviewed_legistar_profiles(
    db: None,
) -> None:
    """P15e cannot turn the generic staging capability into another city's cursor (FR-10.3/NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    for source_key in (
        "san-jose-legistar-meetings",
        "sunnyvale-legistar-meetings",
        "alameda-legistar-meetings",
        "oakland-legistar-meetings",
    ):
        source = await repository.get(source_key)
        assert source is not None
        assert source.has_paged_http_get
        if source_key != "san-jose-legistar-meetings":
            assert source.source_revision >= 2
        run_key = f"manual:paged-profile-{source_key}-{uuid4().hex}"
        claim = await repository.claim_refresh(source_key, run_key, lease_seconds=120)
        assert claim.lease_token is not None
        prepared = await repository.prepare_paged_refresh(
            source_key,
            run_key,
            lease_token=claim.lease_token,
            source_revision=source.source_revision,
        )
        assert prepared.status == "ready"
        assert prepared.progress is not None
        assert (
            await repository.abort_paged_refresh(
                source_key,
                run_key,
                lease_token=claim.lease_token,
                error="fixture cleanup",
            )
            is True
        )

    nonpaged = await repository.get("mountain-view-library-events")
    assert nonpaged is not None
    assert not nonpaged.has_paged_http_get
    unapproved_run = f"manual:unapproved-paged-profile-{uuid4().hex}"
    claim = await repository.claim_refresh(nonpaged.source_key, unapproved_run, lease_seconds=120)
    assert claim.lease_token is not None
    assert (
        await repository.prepare_paged_refresh(
            nonpaged.source_key,
            unapproved_run,
            lease_token=claim.lease_token,
            source_revision=nonpaged.source_revision,
        )
    ).status == "invalid"
    assert (
        await repository.fail_refresh(
            nonpaged.source_key,
            unapproved_run,
            lease_token=claim.lease_token,
            error="fixture cleanup",
        )
        is True
    )


async def test_p15e_prepare_rejects_owner_endpoint_or_origin_drift_before_any_page_stage(
    db: None,
) -> None:
    """The SQL capability itself refuses a reviewed key/mode whose transport contract changed (FR-10.3)."""
    repository = PostgresCatalogSourceRepository()
    source = await repository.get("oakland-legistar-meetings")
    assert source is not None
    run_key = f"manual:p15e-transport-contract-{uuid4().hex}"
    claim = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert claim.lease_token is not None

    try:
        await _set_owner_source_transport_contract(
            source.source_key,
            seed_url="https://unreviewed.example.test/v1/Oakland/Events",
            approved_origins=("https://unreviewed.example.test",),
        )
        assert (
            await repository.prepare_paged_refresh(
                source.source_key,
                run_key,
                lease_token=claim.lease_token,
                source_revision=source.source_revision,
            )
        ).status == "invalid"
    finally:
        await _set_owner_source_transport_contract(
            source.source_key,
            seed_url=source.seed_url,
            approved_origins=source.approved_origins,
        )
        assert (
            await repository.fail_refresh(
                source.source_key,
                run_key,
                lease_token=claim.lease_token,
                error="fixture cleanup after source transport-contract rejection",
            )
            is True
        )


@pytest.mark.parametrize(
    ("source_key", "phase", "handoff_host"),
    [
        ("sunnyvale-legistar-meetings", "p15c", "sunnyvaleca.legistar.com"),
        ("alameda-legistar-meetings", "p15d", "alameda.legistar.com"),
        ("oakland-legistar-meetings", "p15e", "oakland.legistar.com"),
    ],
)
async def test_p15c_p15d_p15e_city_terminal_stage_promotes_through_the_same_atomic_capability(
    db: None,
    source_key: str,
    phase: str,
    handoff_host: str,
) -> None:
    """Each reviewed city profile gets one no-raw terminal stage/promotion transaction (NFR-1/NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    source = await repository.get(source_key)
    assert source is not None
    run_key = f"manual:{phase}-promote-{uuid4().hex}"
    remote_id = str(4 * 10**12 + (uuid4().int % 10**11))
    candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"{phase}-promoted:{remote_id}",
        title=f"{phase.upper()} promoted civic meeting",
        start_at=datetime.now(UTC) + timedelta(days=50),
        registration_url=f"https://{handoff_host}/MeetingDetail.aspx?LEGID={remote_id}",
        venue_name="City Hall",
    )
    claim = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert claim.lease_token is not None
    assert (
        await repository.prepare_paged_refresh(
            source.source_key,
            run_key,
            lease_token=claim.lease_token,
            source_revision=source.source_revision,
        )
    ).status == "ready"
    assert (
        await repository.stage_paged_page(
            source.source_key,
            run_key,
            lease_token=claim.lease_token,
            source_revision=source.source_revision,
            page=CatalogSourcePage(0, 1, (candidate,), (remote_id,)),
        )
    ).status == "terminal"

    container = build_container(get_settings())
    promoter = PostgresCatalogPagedRefreshPromoter(
        container.catalog,
        container.catalog_observation_repo,
    )
    promoted = await promoter.promote_paged_refresh(
        source.source_key,
        run_key,
        lease_token=claim.lease_token,
        source_revision=source.source_revision,
    )

    assert (promoted.candidate_count, promoted.canonical_count) == (1, 1)
    completed = await repository.get_refresh_run(source.source_key, run_key)
    assert completed is not None
    assert completed.status is CatalogRefreshRunStatus.SUCCEEDED
    observations = await PostgresCatalogObservationRepository().list_for_source(source.source_key)
    assert any(item.source_event_id == candidate.source_event_id for item in observations)


@pytest.mark.parametrize(
    ("source_key", "phase", "handoff_host"),
    [
        ("san-jose-legistar-meetings", "p15b", "sanjose.legistar.com"),
        ("sunnyvale-legistar-meetings", "p15c", "sunnyvaleca.legistar.com"),
        ("alameda-legistar-meetings", "p15d", "alameda.legistar.com"),
        ("oakland-legistar-meetings", "p15e", "oakland.legistar.com"),
    ],
)
async def test_paged_stage_rejects_an_owner_contract_edit_that_won_during_the_source_get(
    db: None,
    source_key: str,
    phase: str,
    handoff_host: str,
) -> None:
    """Each city stage locks/rechecks its source row, so old pages cannot enter a new contract."""
    repository = PostgresCatalogSourceRepository()
    source = await repository.get(source_key)
    assert source is not None
    run_key = f"manual:{phase}-stage-contract-race-{uuid4().hex}"
    remote_id = str(5 * 10**12 + (uuid4().int % 10**11))
    candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"{phase}-stage-race:{remote_id}",
        title=f"{phase.upper()} stage contract race",
        start_at=datetime.now(UTC) + timedelta(days=45),
        registration_url=f"https://{handoff_host}/MeetingDetail.aspx?LEGID={remote_id}",
        venue_name="City Hall",
    )
    claim = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert claim.lease_token is not None
    assert (
        await repository.prepare_paged_refresh(
            source.source_key,
            run_key,
            lease_token=claim.lease_token,
            source_revision=source.source_revision,
        )
    ).status == "ready"

    try:
        await _set_owner_source_min_interval(source.source_key, source.min_interval_ms + 1)
        with pytest.raises(DBAPIError, match="source contract is no longer current"):
            await repository.stage_paged_page(
                source.source_key,
                run_key,
                lease_token=claim.lease_token,
                source_revision=source.source_revision,
                page=CatalogSourcePage(0, 1, (candidate,), (remote_id,)),
            )
        running = await repository.get_refresh_run(source.source_key, run_key)
        assert running is not None
        assert running.status is CatalogRefreshRunStatus.RUNNING
        assert (
            await repository.abort_paged_refresh(
                source.source_key,
                run_key,
                lease_token=claim.lease_token,
                error="fixture cleanup after staged contract race",
            )
            is True
        )
    finally:
        await _set_owner_source_min_interval(source.source_key, source.min_interval_ms)


@pytest.mark.parametrize(
    ("source_key", "phase", "handoff_host"),
    [
        ("san-jose-legistar-meetings", "p15b", "sanjose.legistar.com"),
        ("sunnyvale-legistar-meetings", "p15c", "sunnyvaleca.legistar.com"),
        ("alameda-legistar-meetings", "p15d", "alameda.legistar.com"),
        ("oakland-legistar-meetings", "p15e", "oakland.legistar.com"),
    ],
)
async def test_paged_promotion_rejects_an_owner_contract_edit_and_rolls_back_catalog_writes(
    db: None,
    source_key: str,
    phase: str,
    handoff_host: str,
) -> None:
    """A city revision committed before promotion cannot publish its old terminal stage (NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    source = await repository.get(source_key)
    assert source is not None
    run_key = f"manual:{phase}-promotion-contract-race-{uuid4().hex}"
    remote_id = str(6 * 10**12 + (uuid4().int % 10**11))
    candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"{phase}-promotion-race:{remote_id}",
        title=f"{phase.upper()} promotion contract race",
        start_at=datetime.now(UTC) + timedelta(days=55),
        registration_url=f"https://{handoff_host}/MeetingDetail.aspx?LEGID={remote_id}",
        venue_name="City Hall",
    )
    claim = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert claim.lease_token is not None
    assert (
        await repository.prepare_paged_refresh(
            source.source_key,
            run_key,
            lease_token=claim.lease_token,
            source_revision=source.source_revision,
        )
    ).status == "ready"
    assert (
        await repository.stage_paged_page(
            source.source_key,
            run_key,
            lease_token=claim.lease_token,
            source_revision=source.source_revision,
            page=CatalogSourcePage(0, 1, (candidate,), (remote_id,)),
        )
    ).status == "terminal"
    container = build_container(get_settings())
    promoter = PostgresCatalogPagedRefreshPromoter(
        container.catalog,
        container.catalog_observation_repo,
    )

    try:
        await _set_owner_source_min_interval(source.source_key, source.min_interval_ms + 1)
        with pytest.raises(DBAPIError, match="source contract is no longer current"):
            await promoter.promote_paged_refresh(
                source.source_key,
                run_key,
                lease_token=claim.lease_token,
                source_revision=source.source_revision,
            )
        running = await repository.get_refresh_run(source.source_key, run_key)
        assert running is not None
        assert running.status is CatalogRefreshRunStatus.RUNNING
        observations = await PostgresCatalogObservationRepository().list_for_source(
            source.source_key
        )
        assert all(item.source_event_id != candidate.source_event_id for item in observations)
        assert (
            await repository.abort_paged_refresh(
                source.source_key,
                run_key,
                lease_token=claim.lease_token,
                error="fixture cleanup after promotion contract race",
            )
            is True
        )
    finally:
        await _set_owner_source_min_interval(source.source_key, source.min_interval_ms)


async def test_unreviewed_source_cannot_create_a_catalog_refresh_run(db: None) -> None:
    """The claim capability cannot manufacture a lease for an owner-unreviewed source (FR-10.3)."""
    source_key = f"test-unreviewed-{uuid4().hex}"
    source = CatalogSource(
        source_key=source_key,
        display_name="Unreviewed source fixture",
        publisher="Events Concierge tests",
        seed_url="https://unreviewed.example.test/catalog",
        approved_origins=("https://unreviewed.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=None,
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1_500,
    )
    await _seed_owner_source(source)

    run_key = "manual:unreviewed"
    async with system_session_scope() as session:
        outcome = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_claim_catalog_refresh(
                        :source_key, :run_key, 60, :lease_token
                    )
                    """
                ),
                {
                    "source_key": source_key,
                    "run_key": run_key,
                    "lease_token": uuid4(),
                },
            )
        ).scalar_one()

    assert outcome == "unavailable"
    assert await PostgresCatalogSourceRepository().get_refresh_run(source_key, run_key) is None


async def test_registry_seed_and_refresh_lease_converge_across_retries(db: None) -> None:
    """One source/run key has one owner; failed work can be reclaimed but success cannot (NFR-8)."""
    repository = PostgresCatalogSourceRepository()

    now = datetime.now(UTC).replace(microsecond=0)
    source_key = f"test-registry-{uuid4().hex}"
    source = CatalogSource(
        source_key=source_key,
        display_name="Registry integration fixture",
        publisher="Events Concierge tests",
        seed_url="https://events.example.test/catalog",
        approved_origins=("https://events.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=now,
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1_500,
    )
    await _seed_owner_source(source)

    assert await repository.get(source_key) == source
    assert source_key in {item.source_key for item in await repository.list_refreshable(now)}

    run_key = "manual:once"
    first = await repository.claim_refresh(source_key, run_key, lease_seconds=60)
    assert first.outcome is CatalogRefreshClaimOutcome.ACQUIRED
    assert first.lease_token is not None

    busy = await repository.claim_refresh(source_key, run_key, lease_seconds=60)
    assert busy.outcome is CatalogRefreshClaimOutcome.BUSY
    assert (
        await repository.complete_refresh(
            source_key,
            run_key,
            lease_token=first.lease_token,
            candidate_count=3,
            canonical_count=2,
        )
        is True
    )
    completed = await repository.get_refresh_run(source_key, run_key)
    assert completed is not None
    assert completed.status is CatalogRefreshRunStatus.SUCCEEDED
    assert (completed.candidate_count, completed.canonical_count, completed.attempt_count) == (
        3,
        2,
        1,
    )
    assert (
        await repository.claim_refresh(source_key, run_key, lease_seconds=60)
    ).outcome is CatalogRefreshClaimOutcome.SUCCEEDED

    retry_key = "manual:retry"
    failed = await repository.claim_refresh(source_key, retry_key, lease_seconds=60)
    assert failed.lease_token is not None
    assert (
        await repository.fail_refresh(
            source_key,
            retry_key,
            lease_token=failed.lease_token,
            error="fixture failure",
        )
        is True
    )
    retried = await repository.claim_refresh(source_key, retry_key, lease_seconds=60)
    assert retried.outcome is CatalogRefreshClaimOutcome.ACQUIRED
    assert retried.lease_token is not None
    assert retried.lease_token != failed.lease_token
    assert (
        await repository.complete_refresh(
            source_key,
            retry_key,
            lease_token=failed.lease_token,
            candidate_count=7,
            canonical_count=6,
        )
        is False
    )
    assert (
        await repository.complete_refresh(
            source_key,
            retry_key,
            lease_token=retried.lease_token,
            candidate_count=7,
            canonical_count=6,
        )
        is True
    )
    retried_completed = await repository.get_refresh_run(source_key, retry_key)
    assert retried_completed is not None
    assert retried_completed.status is CatalogRefreshRunStatus.SUCCEEDED
    assert (retried_completed.candidate_count, retried_completed.canonical_count) == (7, 6)
    assert retried_completed.attempt_count == 2
    assert (
        await repository.claim_refresh(source_key, retry_key, lease_seconds=60)
    ).outcome is CatalogRefreshClaimOutcome.SUCCEEDED

    container = build_container(get_settings())
    candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"observed-{uuid4().hex}",
        title=f"Observed source fixture {uuid4().hex}",
        start_at=now + timedelta(days=90),
        registration_url="https://events.example.test/observed",
        city=source_key,
        is_free=True,
    )
    canonical = (await container.catalog.upsert_candidates([candidate]))[0]
    observations = PostgresCatalogObservationRepository()
    first_observation = observation_for(source_key, candidate, canonical.canonical_event_id)
    await observations.record([first_observation], run_key)

    recorded = await observations.list_for_source(source_key)
    assert len(recorded) == 1
    assert recorded[0].canonical_event_id == canonical.canonical_event_id
    assert recorded[0].last_run_key == run_key
    assert recorded[0].content_hash == first_observation.content_hash


async def test_due_refresh_selection_uses_manual_success_as_its_durable_cadence_cursor(
    db: None,
) -> None:
    """One manual success suppresses a scheduled slot until the persisted interval elapses (NFR-1/8)."""
    repository = PostgresCatalogSourceRepository()
    now = datetime.now(UTC).replace(microsecond=0)
    source_key = f"test-cadence-{uuid4().hex}"
    source = CatalogSource(
        source_key=source_key,
        display_name="Cadence integration fixture",
        publisher="Events Concierge tests",
        seed_url="https://cadence.example.test/catalog",
        approved_origins=("https://cadence.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=now - timedelta(hours=2),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1_500,
    )
    await _seed_owner_source(source)

    initial_due = await repository.list_due_refreshes(now, limit=500)
    initial = next(item for item in initial_due if item.source.source_key == source_key)
    assert initial.last_succeeded_at is None
    assert initial.due_at == source.reviewed_at

    manual_run_key = "manual:cadence-cursor"
    claim = await repository.claim_refresh(
        source_key,
        manual_run_key,
        lease_seconds=60,
    )
    assert claim.lease_token is not None
    assert (
        await repository.complete_refresh(
            source_key,
            manual_run_key,
            lease_token=claim.lease_token,
            candidate_count=0,
            canonical_count=0,
        )
        is True
    )

    completed = await repository.get_refresh_run(source_key, manual_run_key)
    assert completed is not None
    assert completed.completed_at is not None
    before_due = await repository.list_due_refreshes(
        completed.completed_at + timedelta(minutes=59),
        limit=500,
    )
    assert source_key not in {item.source.source_key for item in before_due}

    due_at = completed.completed_at + timedelta(minutes=60)
    due = await repository.list_due_refreshes(due_at, limit=500)
    scheduled = next(item for item in due if item.source.source_key == source_key)
    assert scheduled.last_succeeded_at == completed.completed_at
    assert scheduled.due_at == due_at
    assert scheduled.run_key() == f"cadence:{source_key}:{due_at.strftime('%Y%m%dT%H%M%S.%fZ')}"

    failed_claim = await repository.claim_refresh(
        source_key,
        scheduled.run_key(),
        lease_seconds=60,
    )
    assert failed_claim.lease_token is not None
    assert (
        await repository.fail_refresh(
            source_key,
            scheduled.run_key(),
            lease_token=failed_claim.lease_token,
            error="fixture transient failure",
        )
        is True
    )
    retried_due = await repository.list_due_refreshes(due_at, limit=500)
    retry = next(item for item in retried_due if item.source.source_key == source_key)
    assert retry.run_key() == scheduled.run_key()


async def test_catalog_refresh_complete_and_fail_require_live_leases_before_reclaim(
    db: None,
) -> None:
    """Expired generic catalog leases cannot advance cadence or record failure (NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    source_key = f"test-p25-refresh-{uuid4().hex}"
    source = CatalogSource(
        source_key=source_key,
        display_name="P25 catalog refresh lease fixture",
        publisher="Events Concierge tests",
        seed_url="https://p25-refresh.example.test/catalog",
        approved_origins=("https://p25-refresh.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=datetime.now(UTC) - timedelta(minutes=1),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1_500,
    )
    await _seed_owner_source(source)

    complete_run_key = f"manual:p25-complete-{uuid4().hex}"
    complete_claim = await repository.claim_refresh(source_key, complete_run_key, lease_seconds=60)
    assert complete_claim.acquired
    assert complete_claim.lease_token is not None
    await _expire_owner_refresh_lease(source_key, complete_run_key, complete_claim.lease_token)
    expired_complete = await _owner_refresh_state(source_key, complete_run_key)

    assert (
        await repository.complete_refresh(
            source_key,
            complete_run_key,
            lease_token=complete_claim.lease_token,
            candidate_count=3,
            canonical_count=2,
        )
        is False
    )
    assert await _owner_refresh_state(source_key, complete_run_key) == expired_complete

    fresh_complete = await repository.claim_refresh(source_key, complete_run_key, lease_seconds=60)
    assert fresh_complete.acquired
    assert fresh_complete.lease_token is not None
    assert fresh_complete.lease_token != complete_claim.lease_token
    assert (
        await repository.complete_refresh(
            source_key,
            complete_run_key,
            lease_token=fresh_complete.lease_token,
            candidate_count=3,
            canonical_count=2,
        )
        is True
    )
    completed_state = await _owner_refresh_state(source_key, complete_run_key)
    assert completed_state["status"] == CatalogRefreshRunStatus.SUCCEEDED.value

    fail_run_key = f"manual:p25-fail-{uuid4().hex}"
    fail_claim = await repository.claim_refresh(source_key, fail_run_key, lease_seconds=60)
    assert fail_claim.acquired
    assert fail_claim.lease_token is not None
    await _expire_owner_refresh_lease(source_key, fail_run_key, fail_claim.lease_token)
    expired_fail = await _owner_refresh_state(source_key, fail_run_key)

    assert (
        await repository.fail_refresh(
            source_key,
            fail_run_key,
            lease_token=fail_claim.lease_token,
            error="stale fixture failure",
        )
        is False
    )
    assert await _owner_refresh_state(source_key, fail_run_key) == expired_fail

    fresh_fail = await repository.claim_refresh(source_key, fail_run_key, lease_seconds=60)
    assert fresh_fail.acquired
    assert fresh_fail.lease_token is not None
    assert fresh_fail.lease_token != fail_claim.lease_token
    assert (
        await repository.fail_refresh(
            source_key,
            fail_run_key,
            lease_token=fresh_fail.lease_token,
            error="current fixture failure",
        )
        is True
    )
    failed_state = await _owner_refresh_state(source_key, fail_run_key)
    assert failed_state["status"] == CatalogRefreshRunStatus.FAILED.value


async def test_generic_catalog_fetch_lease_check_reads_the_database_clock_after_a_row_wait(
    db: None,
) -> None:
    """A post-Pacer generic fetch check cannot authorize a lease that expired while blocked (NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    source_key = f"test-p33-generic-fetch-{uuid4().hex}"
    source = CatalogSource(
        source_key=source_key,
        display_name="P33 generic fetch lease fixture",
        publisher="Events Concierge tests",
        seed_url="https://p33-generic.example.test/catalog",
        approved_origins=("https://p33-generic.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=datetime.now(UTC) - timedelta(minutes=1),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1_500,
    )
    await _seed_owner_source(source)
    run_key = f"manual:p33-generic-fetch-{uuid4().hex}"
    stale_claim = await repository.claim_refresh(source_key, run_key, lease_seconds=2)
    assert stale_claim.acquired and stale_claim.lease_token is not None

    async with _owner_paged_catalog_lock(source_key, run_key, lock_source=False) as owner:
        stale_check = asyncio.create_task(
            repository.has_live_refresh_lease(
                source_key,
                run_key,
                lease_token=stale_claim.lease_token,
            )
        )
        await asyncio.sleep(0.1)
        assert not stale_check.done()
        await _wait_for_catalog_refresh_lease_expiry(owner, source_key, run_key)

    assert await asyncio.wait_for(stale_check, timeout=5) is False
    stale_state = await _owner_refresh_state(source_key, run_key)
    assert (
        stale_state["status"] == CatalogRefreshRunStatus.RUNNING.value
        and stale_state["lease_token"] == stale_claim.lease_token
    )
    fresh_claim = await repository.claim_refresh(source_key, run_key, lease_seconds=120)
    assert fresh_claim.acquired and fresh_claim.lease_token is not None
    assert fresh_claim.lease_token != stale_claim.lease_token
    assert (
        await repository.has_live_refresh_lease(
            source_key,
            run_key,
            lease_token=stale_claim.lease_token,
        )
        is False
    )
    assert (
        await repository.has_live_refresh_lease(
            source_key,
            run_key,
            lease_token=fresh_claim.lease_token,
        )
        is True
    )
    assert (
        await repository.fail_refresh(
            source_key,
            run_key,
            lease_token=fresh_claim.lease_token,
            error="P33 fixture cleanup",
        )
        is True
    )


async def test_generic_catalog_commit_rolls_back_stale_publication_before_reclaim(
    db: None,
) -> None:
    """A blocked generic merge cannot publish catalog/provenance after its lease is reclaimed (NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    source_key = f"test-p31-generic-commit-{uuid4().hex}"
    source = CatalogSource(
        source_key=source_key,
        display_name="P31 generic atomic commit fixture",
        publisher="Events Concierge tests",
        seed_url="https://p31-generic.example.test/catalog",
        approved_origins=("https://p31-generic.example.test",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=datetime.now(UTC) - timedelta(minutes=1),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1_500,
    )
    await _seed_owner_source(source)
    container = build_container(get_settings())
    committer = PostgresCatalogRefreshCommitter(
        container.catalog,
        container.catalog_observation_repo,
    )
    start_at = datetime.now(UTC) + timedelta(days=37)
    seed = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"p31-seed-{uuid4().hex}",
        title=f"P31 generic atomic catalog fixture {uuid4().hex}",
        start_at=start_at,
        registration_url="https://p31-generic.example.test/seed",
        description="seed description",
    )
    canonical = (await container.catalog.upsert_candidates([seed]))[0]
    stale_candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"p31-stale-{uuid4().hex}",
        title=seed.title,
        start_at=start_at,
        registration_url="https://p31-generic.example.test/stale",
        description="stale description that must roll back after the lease is reclaimed",
    )
    run_key = f"manual:p31-generic-commit-{uuid4().hex}"
    stale_claim = await repository.claim_refresh(source_key, run_key, lease_seconds=2)
    assert stale_claim.acquired and stale_claim.lease_token is not None

    async with _owner_canonical_event_lock(canonical.canonical_event_id) as owner:
        stale_commit = asyncio.create_task(
            committer.commit_refresh(
                source_key,
                run_key,
                lease_token=stale_claim.lease_token,
                candidates=[stale_candidate],
            )
        )
        await asyncio.sleep(0.1)
        assert not stale_commit.done()
        await _wait_for_catalog_refresh_lease_expiry(owner, source_key, run_key)
        fresh_claim = await repository.claim_refresh(source_key, run_key, lease_seconds=120)
        assert fresh_claim.acquired and fresh_claim.lease_token is not None
        assert fresh_claim.lease_token != stale_claim.lease_token

    assert await asyncio.wait_for(stale_commit, timeout=5) is None
    stale_state = await _owner_refresh_state(source_key, run_key)
    assert (
        stale_state["status"] == CatalogRefreshRunStatus.RUNNING.value
        and stale_state["lease_token"] == fresh_claim.lease_token
        and stale_state["candidate_count"] is None
        and stale_state["canonical_count"] is None
    )
    canonical_after_stale = await container.catalog.get(canonical.canonical_event_id)
    assert canonical_after_stale is not None
    assert canonical_after_stale.description == seed.description
    assert {link.source_event_id for link in canonical_after_stale.source_links} == {
        seed.source_event_id
    }
    assert await PostgresCatalogObservationRepository().list_for_source(source_key) == []

    fresh_candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"p31-fresh-{uuid4().hex}",
        title=seed.title,
        start_at=start_at,
        registration_url="https://p31-generic.example.test/fresh",
        description="fresh description committed exactly once after reclaim",
    )
    committed = await committer.commit_refresh(
        source_key,
        run_key,
        lease_token=fresh_claim.lease_token,
        candidates=[fresh_candidate],
    )
    assert committed is not None
    assert (committed.candidate_count, committed.canonical_count) == (1, 1)
    completed = await repository.get_refresh_run(source_key, run_key)
    assert completed is not None
    assert completed.status is CatalogRefreshRunStatus.SUCCEEDED
    observations = await PostgresCatalogObservationRepository().list_for_source(source_key)
    assert [item.source_event_id for item in observations] == [fresh_candidate.source_event_id]

    replay_candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"p31-replay-{uuid4().hex}",
        title=seed.title,
        start_at=start_at,
        registration_url="https://p31-generic.example.test/replay",
        description="replayed description must not become a stale catalog publication",
    )
    assert (
        await committer.commit_refresh(
            source_key,
            run_key,
            lease_token=fresh_claim.lease_token,
            candidates=[replay_candidate],
        )
        is None
    )
    canonical_after_replay = await container.catalog.get(canonical.canonical_event_id)
    assert canonical_after_replay is not None
    assert canonical_after_replay.description == fresh_candidate.description
    assert {link.source_event_id for link in canonical_after_replay.source_links} == {
        seed.source_event_id,
        fresh_candidate.source_event_id,
    }
    observations_after_replay = await PostgresCatalogObservationRepository().list_for_source(
        source_key
    )
    assert [item.source_event_id for item in observations_after_replay] == [
        fresh_candidate.source_event_id
    ]
    replay = await repository.claim_refresh(source_key, run_key, lease_seconds=120)
    assert replay.outcome is CatalogRefreshClaimOutcome.SUCCEEDED


async def test_paged_catalog_abort_requires_a_live_lease_before_discarding_stage(
    db: None,
) -> None:
    """An expired P15 worker cannot erase its terminal normalized stage before reclaim (NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    source = await repository.get("san-jose-legistar-meetings")
    assert source is not None
    run_key = f"manual:p25-abort-{uuid4().hex}"
    claim = await repository.claim_refresh(source.source_key, run_key, lease_seconds=60)
    assert claim.acquired
    assert claim.lease_token is not None
    prepared = await repository.prepare_paged_refresh(
        source.source_key,
        run_key,
        lease_token=claim.lease_token,
        source_revision=source.source_revision,
    )
    assert prepared.status == "ready"
    assert (
        await repository.stage_paged_page(
            source.source_key,
            run_key,
            lease_token=claim.lease_token,
            source_revision=source.source_revision,
            page=CatalogSourcePage(0, 0, (), ()),
        )
    ).status == "terminal"

    await _expire_owner_refresh_lease(source.source_key, run_key, claim.lease_token)
    expired_state = await _owner_refresh_state(source.source_key, run_key)
    assert expired_state["status"] == CatalogRefreshRunStatus.RUNNING.value
    assert expired_state["progress_count"] == 1
    assert expired_state["stage_page_count"] == 1

    assert (
        await repository.abort_paged_refresh(
            source.source_key,
            run_key,
            lease_token=claim.lease_token,
            error="stale fixture abort",
        )
        is False
    )
    assert await _owner_refresh_state(source.source_key, run_key) == expired_state

    fresh_claim = await repository.claim_refresh(source.source_key, run_key, lease_seconds=60)
    assert fresh_claim.acquired
    assert fresh_claim.lease_token is not None
    assert fresh_claim.lease_token != claim.lease_token
    reclaimed = await repository.prepare_paged_refresh(
        source.source_key,
        run_key,
        lease_token=fresh_claim.lease_token,
        source_revision=source.source_revision,
    )
    assert reclaimed.status == "ready"
    assert reclaimed.progress is not None
    assert reclaimed.progress.is_terminal
    assert (
        await repository.abort_paged_refresh(
            source.source_key,
            run_key,
            lease_token=fresh_claim.lease_token,
            error="current fixture abort",
        )
        is True
    )
    cleared_state = await _owner_refresh_state(source.source_key, run_key)
    assert cleared_state["status"] == CatalogRefreshRunStatus.FAILED.value
    assert cleared_state["progress_count"] == 0
    assert cleared_state["stage_page_count"] == 0


async def test_paged_full_page_late_lease_rolls_back_stage_and_cursor_before_reclaim(
    db: None,
) -> None:
    """A P15 full-page lease loss after the source guard cannot advance a recoverable cursor (NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    source = await repository.get("san-jose-legistar-meetings")
    assert source is not None
    run_key = f"manual:p28-stage-lease-{uuid4().hex}"
    remote_id = str(7 * 10**12 + (uuid4().int % 10**11))
    candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"p28-stage:{remote_id}",
        title="P28 full-page lease fence fixture",
        start_at=datetime.now(UTC) + timedelta(days=35),
        registration_url=f"https://sanjose.legistar.com/MeetingDetail.aspx?LEGID={remote_id}",
        venue_name="City Hall",
    )
    page = CatalogSourcePage(0, 100, (candidate,), (remote_id,))
    claim = await repository.claim_refresh(source.source_key, run_key, lease_seconds=2)
    assert claim.acquired
    assert claim.lease_token is not None
    prepared = await repository.prepare_paged_refresh(
        source.source_key,
        run_key,
        lease_token=claim.lease_token,
        source_revision=source.source_revision,
    )
    assert prepared.status == "ready"
    assert prepared.progress is not None
    assert prepared.progress.next_page == 0

    async with _owner_paged_catalog_lock(source.source_key, run_key, lock_source=True) as owner:
        staging = asyncio.create_task(
            repository.stage_paged_page(
                source.source_key,
                run_key,
                lease_token=claim.lease_token,
                source_revision=source.source_revision,
                page=page,
            )
        )
        await asyncio.sleep(0.1)
        assert not staging.done()
        await _wait_for_paged_refresh_lease_expiry(owner, source.source_key, run_key)

    assert (await asyncio.wait_for(staging, timeout=5)).status == "lease_lost"
    stale = await _owner_refresh_state(source.source_key, run_key)
    assert stale["status"] == CatalogRefreshRunStatus.RUNNING.value
    assert stale["lease_token"] == claim.lease_token
    assert stale["progress_count"] == 1
    assert stale["progress_next_page"] == 0
    assert stale["progress_terminal_page"] is None
    assert stale["progress_staged_raw_count"] == 0
    assert stale["progress_staged_candidate_count"] == 0
    assert stale["stage_page_count"] == 0
    assert stale["stage_candidate_count"] == 0
    assert stale["stage_event_id_count"] == 0

    fresh = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert fresh.acquired
    assert fresh.lease_token is not None
    assert fresh.lease_token != claim.lease_token
    recovered = await repository.prepare_paged_refresh(
        source.source_key,
        run_key,
        lease_token=fresh.lease_token,
        source_revision=source.source_revision,
    )
    assert recovered.status == "ready"
    assert recovered.progress is not None
    assert recovered.progress.next_page == 0
    assert (
        await repository.stage_paged_page(
            source.source_key,
            run_key,
            lease_token=fresh.lease_token,
            source_revision=source.source_revision,
            page=page,
        )
    ).status == "more"

    cleanup = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert cleanup.acquired
    assert cleanup.lease_token is not None
    assert (
        await repository.abort_paged_refresh(
            source.source_key,
            run_key,
            lease_token=cleanup.lease_token,
            error="P28 full-page lease fence fixture cleanup",
        )
        is True
    )


async def test_paged_terminal_stage_after_late_lease_remains_recoverable_and_promotes_once(
    db: None,
) -> None:
    """A P15 short page staged after expiry remains a recoverable, exactly-once terminal input (NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    source = await repository.get("san-jose-legistar-meetings")
    assert source is not None
    run_key = f"manual:p29-terminal-stage-lease-{uuid4().hex}"
    remote_id = str(8 * 10**12 + (uuid4().int % 10**11))
    candidate = CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=f"p29-terminal-stage:{remote_id}",
        title="P29 recoverable terminal stage fixture",
        start_at=datetime.now(UTC) + timedelta(days=36),
        registration_url=f"https://sanjose.legistar.com/MeetingDetail.aspx?LEGID={remote_id}",
        venue_name="City Hall",
    )
    page = CatalogSourcePage(0, 1, (candidate,), (remote_id,))
    claim = await repository.claim_refresh(source.source_key, run_key, lease_seconds=2)
    assert claim.acquired and claim.lease_token is not None
    assert (
        await repository.prepare_paged_refresh(
            source.source_key,
            run_key,
            lease_token=claim.lease_token,
            source_revision=source.source_revision,
        )
    ).status == "ready"

    # P28 intentionally permits this terminal stage to remain after expiry: it is not a
    # publish effect, and a fresh lease must later promote the exact durable input once.
    async with _owner_paged_catalog_lock(source.source_key, run_key, lock_source=True) as owner:
        staging = asyncio.create_task(
            repository.stage_paged_page(
                source.source_key,
                run_key,
                lease_token=claim.lease_token,
                source_revision=source.source_revision,
                page=page,
            )
        )
        await asyncio.sleep(0.1)
        assert not staging.done()
        await _wait_for_paged_refresh_lease_expiry(owner, source.source_key, run_key)

    assert (await asyncio.wait_for(staging, timeout=5)).status == "terminal"
    stale = await _owner_refresh_state(source.source_key, run_key)
    assert stale["status"] == CatalogRefreshRunStatus.RUNNING.value
    assert stale["lease_token"] == claim.lease_token
    assert stale["progress_count"] == 1
    assert stale["progress_next_page"] == 1
    assert stale["progress_terminal_page"] == 0
    assert stale["progress_staged_raw_count"] == 1
    assert stale["progress_staged_candidate_count"] == 1
    assert stale["stage_page_count"] == 1
    assert stale["stage_candidate_count"] == 1
    assert stale["stage_event_id_count"] == 1

    fresh = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert fresh.acquired
    assert fresh.lease_token is not None
    assert fresh.lease_token != claim.lease_token
    recovered = await repository.prepare_paged_refresh(
        source.source_key,
        run_key,
        lease_token=fresh.lease_token,
        source_revision=source.source_revision,
    )
    assert recovered.status == "ready"
    assert recovered.progress is not None
    assert recovered.progress.is_terminal

    container = build_container(get_settings())
    promoter = PostgresCatalogPagedRefreshPromoter(
        container.catalog,
        container.catalog_observation_repo,
    )
    promoted = await promoter.promote_paged_refresh(
        source.source_key,
        run_key,
        lease_token=fresh.lease_token,
        source_revision=source.source_revision,
    )
    assert (promoted.candidate_count, promoted.canonical_count) == (1, 1)
    completed = await repository.get_refresh_run(source.source_key, run_key)
    assert completed is not None
    assert completed.status is CatalogRefreshRunStatus.SUCCEEDED
    assert (completed.candidate_count, completed.canonical_count) == (1, 1)
    observations = await PostgresCatalogObservationRepository().list_for_source(source.source_key)
    assert [item.source_event_id for item in observations].count(candidate.source_event_id) == 1

    replay = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert replay.outcome is CatalogRefreshClaimOutcome.SUCCEEDED
    observations_after_replay = await PostgresCatalogObservationRepository().list_for_source(
        source.source_key
    )
    assert [item.source_event_id for item in observations_after_replay].count(
        candidate.source_event_id
    ) == 1


async def test_paged_prepare_late_run_lock_lease_loss_leaves_no_cursor_before_reclaim(
    db: None,
) -> None:
    """A P15 prepare waiting on its run cannot create a cursor after lease expiry (NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    source = await repository.get("san-jose-legistar-meetings")
    assert source is not None
    run_key = f"manual:p30-prepare-run-lock-{uuid4().hex}"
    claim = await repository.claim_refresh(source.source_key, run_key, lease_seconds=2)
    assert claim.acquired and claim.lease_token is not None

    async with _owner_paged_catalog_lock(source.source_key, run_key, lock_source=False) as owner:
        preparation = asyncio.create_task(
            repository.prepare_paged_refresh(
                source.source_key,
                run_key,
                lease_token=claim.lease_token,
                source_revision=source.source_revision,
            )
        )
        await asyncio.sleep(0.1)
        assert not preparation.done()
        await _wait_for_paged_refresh_lease_expiry(owner, source.source_key, run_key)

    assert (await asyncio.wait_for(preparation, timeout=5)).status == "lease_lost"
    stale = await _owner_refresh_state(source.source_key, run_key)
    assert stale["status"] == CatalogRefreshRunStatus.RUNNING.value
    assert stale["lease_token"] == claim.lease_token
    assert stale["progress_count"] == 0
    assert stale["stage_page_count"] == 0

    fresh = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert fresh.acquired and fresh.lease_token is not None
    assert fresh.lease_token != claim.lease_token
    recovered = await repository.prepare_paged_refresh(
        source.source_key,
        run_key,
        lease_token=fresh.lease_token,
        source_revision=source.source_revision,
    )
    assert recovered.status == "ready"
    assert recovered.progress is not None
    assert recovered.progress.next_page == 0
    assert (
        await repository.abort_paged_refresh(
            source.source_key,
            run_key,
            lease_token=fresh.lease_token,
            error="P30 run-lock prepare fence fixture cleanup",
        )
        is True
    )


async def test_paged_prepare_late_cursor_lock_lease_loss_preserves_existing_cursor(
    db: None,
) -> None:
    """A P15 prepare waiting on its cursor cannot reset or return it after expiry (NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    source = await repository.get("san-jose-legistar-meetings")
    assert source is not None
    run_key = f"manual:p30-prepare-cursor-lock-{uuid4().hex}"
    seed = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert seed.acquired and seed.lease_token is not None
    assert (
        await repository.prepare_paged_refresh(
            source.source_key,
            run_key,
            lease_token=seed.lease_token,
            source_revision=source.source_revision,
        )
    ).status == "ready"
    assert (
        await repository.pause_paged_refresh(
            source.source_key,
            run_key,
            lease_token=seed.lease_token,
            error="P30 cursor-lock prepare fixture seed",
        )
        is True
    )
    claim = await repository.claim_refresh(source.source_key, run_key, lease_seconds=2)
    assert claim.acquired and claim.lease_token is not None

    async with _owner_paged_catalog_lock(
        source.source_key, run_key, lock_source=False, lock_progress=True
    ) as owner:
        preparation = asyncio.create_task(
            repository.prepare_paged_refresh(
                source.source_key,
                run_key,
                lease_token=claim.lease_token,
                source_revision=source.source_revision,
            )
        )
        await asyncio.sleep(0.1)
        assert not preparation.done()
        await _wait_for_paged_refresh_lease_expiry(owner, source.source_key, run_key)

    assert (await asyncio.wait_for(preparation, timeout=5)).status == "lease_lost"
    stale = await _owner_refresh_state(source.source_key, run_key)
    assert stale["status"] == CatalogRefreshRunStatus.RUNNING.value
    assert stale["lease_token"] == claim.lease_token
    assert stale["progress_count"] == 1
    assert stale["progress_next_page"] == 0
    assert stale["progress_terminal_page"] is None
    assert stale["stage_page_count"] == 0

    fresh = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert fresh.acquired and fresh.lease_token is not None
    assert fresh.lease_token != claim.lease_token
    recovered = await repository.prepare_paged_refresh(
        source.source_key,
        run_key,
        lease_token=fresh.lease_token,
        source_revision=source.source_revision,
    )
    assert recovered.status == "ready"
    assert recovered.progress is not None
    assert recovered.progress.next_page == 0
    assert (
        await repository.abort_paged_refresh(
            source.source_key,
            run_key,
            lease_token=fresh.lease_token,
            error="P30 cursor-lock prepare fence fixture cleanup",
        )
        is True
    )


@pytest.mark.parametrize("lock_source", (False, True), ids=("run-row", "source-contract"))
async def test_paged_promotion_late_lease_leaves_terminal_stage_recoverable_before_reclaim(
    db: None,
    lock_source: bool,
) -> None:
    """A P15 success transition losing either final database wait cannot consume its terminal stage (NFR-8)."""
    repository = PostgresCatalogSourceRepository()
    source = await repository.get("san-jose-legistar-meetings")
    assert source is not None
    run_key = f"manual:p28-promote-lease-{uuid4().hex}"
    claim = await repository.claim_refresh(source.source_key, run_key, lease_seconds=2)
    assert claim.acquired
    assert claim.lease_token is not None
    assert (
        await repository.prepare_paged_refresh(
            source.source_key,
            run_key,
            lease_token=claim.lease_token,
            source_revision=source.source_revision,
        )
    ).status == "ready"
    assert (
        await repository.stage_paged_page(
            source.source_key,
            run_key,
            lease_token=claim.lease_token,
            source_revision=source.source_revision,
            page=CatalogSourcePage(0, 0, (), ()),
        )
    ).status == "terminal"

    async with _owner_paged_catalog_lock(
        source.source_key, run_key, lock_source=lock_source
    ) as owner:
        promotion = asyncio.create_task(
            _direct_paged_refresh_promotion(
                source.source_key,
                run_key,
                lease_token=claim.lease_token,
                source_revision=source.source_revision,
            )
        )
        await asyncio.sleep(0.1)
        assert not promotion.done()
        await _wait_for_paged_refresh_lease_expiry(owner, source.source_key, run_key)

    assert await asyncio.wait_for(promotion, timeout=5) is False
    stale = await _owner_refresh_state(source.source_key, run_key)
    assert stale["status"] == CatalogRefreshRunStatus.RUNNING.value
    assert stale["lease_token"] == claim.lease_token
    assert stale["progress_count"] == 1
    assert stale["progress_next_page"] == 1
    assert stale["progress_terminal_page"] == 0
    assert stale["stage_page_count"] == 1
    assert stale["stage_candidate_count"] == 0
    assert stale["stage_event_id_count"] == 0

    fresh = await repository.claim_refresh(source.source_key, run_key, lease_seconds=120)
    assert fresh.acquired
    assert fresh.lease_token is not None
    assert fresh.lease_token != claim.lease_token
    recovered = await repository.prepare_paged_refresh(
        source.source_key,
        run_key,
        lease_token=fresh.lease_token,
        source_revision=source.source_revision,
    )
    assert recovered.status == "ready"
    assert recovered.progress is not None
    assert recovered.progress.is_terminal
    container = build_container(get_settings())
    promoter = PostgresCatalogPagedRefreshPromoter(
        container.catalog,
        container.catalog_observation_repo,
    )
    promoted = await promoter.promote_paged_refresh(
        source.source_key,
        run_key,
        lease_token=fresh.lease_token,
        source_revision=source.source_revision,
    )
    assert (promoted.candidate_count, promoted.canonical_count) == (0, 0)
    completed = await repository.get_refresh_run(source.source_key, run_key)
    assert completed is not None
    assert completed.status is CatalogRefreshRunStatus.SUCCEEDED


async def _seed_owner_source(source: CatalogSource) -> None:
    """Seed a reviewed source through the migration-owner control plane only (FR-10.3)."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    INSERT INTO public.catalog_sources
                        (source_key, display_name, publisher, seed_url, approved_origins, region,
                         mode, handoff_only, enabled, reviewed_at, review_expires_at,
                         refresh_interval_minutes, min_interval_ms, page_limit)
                    VALUES
                        (:source_key, :display_name, :publisher, :seed_url, :approved_origins, :region,
                         :mode, :handoff_only, :enabled, :reviewed_at, :review_expires_at,
                         :refresh_interval_minutes, :min_interval_ms, :page_limit)
                    ON CONFLICT (source_key) DO UPDATE SET
                        display_name = EXCLUDED.display_name,
                        publisher = EXCLUDED.publisher,
                        seed_url = EXCLUDED.seed_url,
                        approved_origins = EXCLUDED.approved_origins,
                        region = EXCLUDED.region,
                        mode = EXCLUDED.mode,
                        handoff_only = EXCLUDED.handoff_only,
                        enabled = EXCLUDED.enabled,
                        reviewed_at = EXCLUDED.reviewed_at,
                        review_expires_at = EXCLUDED.review_expires_at,
                        refresh_interval_minutes = EXCLUDED.refresh_interval_minutes,
                        min_interval_ms = EXCLUDED.min_interval_ms,
                        page_limit = EXCLUDED.page_limit,
                        updated_at = clock_timestamp()
                    """
                ),
                {
                    "source_key": source.source_key,
                    "display_name": source.display_name,
                    "publisher": source.publisher,
                    "seed_url": source.seed_url,
                    "approved_origins": list(source.approved_origins),
                    "region": source.region,
                    "mode": source.mode.value,
                    "handoff_only": source.handoff_only,
                    "enabled": source.enabled,
                    "reviewed_at": source.reviewed_at,
                    "review_expires_at": source.review_expires_at,
                    "refresh_interval_minutes": source.refresh_interval_minutes,
                    "min_interval_ms": source.min_interval_ms,
                    "page_limit": source.page_limit,
                },
            )
    finally:
        await owner.dispose()


async def _direct_paged_refresh_promotion(
    source_key: str,
    run_key: str,
    *,
    lease_token: UUID,
    source_revision: int,
) -> bool:
    """Invoke the app-role P15 terminal capability without catalog writes to isolate its final fence."""
    async with system_session_scope() as session:
        promoted = (
            await session.execute(
                text(
                    """
                    SELECT public.fn_promote_paged_catalog_refresh(
                        :source_key,
                        :run_key,
                        :lease_token,
                        :source_revision,
                        0,
                        0
                    ) AS promoted
                    """
                ),
                {
                    "source_key": source_key,
                    "run_key": run_key,
                    "lease_token": lease_token,
                    "source_revision": source_revision,
                },
            )
        ).scalar_one()
    return bool(promoted)


@asynccontextmanager
async def _owner_canonical_event_lock(canonical_event_id: UUID) -> AsyncIterator[AsyncConnection]:
    """Hold one canonical row so a generic app-role merge reaches its final lease fence late (NFR-8)."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.connect() as connection, connection.begin():
            row = (
                await connection.execute(
                    text(
                        """
                        SELECT canonical_event_id
                        FROM public.canonical_events
                        WHERE canonical_event_id = :canonical_event_id
                        FOR NO KEY UPDATE
                        """
                    ),
                    {"canonical_event_id": canonical_event_id},
                )
            ).scalar_one_or_none()
            if row is None:
                raise RuntimeError("P31 fixture owner lock did not find its canonical event")
            yield connection
    finally:
        await owner.dispose()


@asynccontextmanager
async def _owner_paged_catalog_lock(
    source_key: str,
    run_key: str,
    *,
    lock_source: bool,
    lock_progress: bool = False,
) -> AsyncIterator[AsyncConnection]:
    """Hold one exact owner row lock until a P15 app call reaches its final wait (NFR-8)."""
    if lock_source and lock_progress:
        raise ValueError("P15 fixture can lock either a source or cursor row, not both")
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.connect() as connection, connection.begin():
            if lock_source:
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT source_key
                            FROM public.catalog_sources
                            WHERE source_key = :source_key
                            FOR NO KEY UPDATE
                            """
                        ),
                        {"source_key": source_key},
                    )
                ).scalar_one_or_none()
            elif lock_progress:
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT run_key
                            FROM public.catalog_refresh_progress
                            WHERE source_key = :source_key
                              AND run_key = :run_key
                            FOR NO KEY UPDATE
                            """
                        ),
                        {"source_key": source_key, "run_key": run_key},
                    )
                ).scalar_one_or_none()
            else:
                row = (
                    await connection.execute(
                        text(
                            """
                            SELECT run_key
                            FROM public.catalog_refresh_runs
                            WHERE source_key = :source_key
                              AND run_key = :run_key
                            FOR NO KEY UPDATE
                            """
                        ),
                        {"source_key": source_key, "run_key": run_key},
                    )
                ).scalar_one_or_none()
            if row is None:
                raise RuntimeError("P15 fixture owner lock did not find its target row")
            yield connection
    finally:
        await owner.dispose()


async def _wait_for_catalog_refresh_lease_expiry(
    connection: AsyncConnection,
    source_key: str,
    run_key: str,
) -> None:
    """Wait on PostgreSQL's clock, never the test-process clock, for the current lease to expire."""
    for _ in range(240):
        expired = (
            await connection.execute(
                text(
                    """
                    SELECT refresh.lease_expires_at <= pg_catalog.clock_timestamp()
                    FROM public.catalog_refresh_runs AS refresh
                    WHERE refresh.source_key = :source_key
                      AND refresh.run_key = :run_key
                    """
                ),
                {"source_key": source_key, "run_key": run_key},
            )
        ).scalar_one()
        if bool(expired):
            return
        await asyncio.sleep(0.025)
    raise RuntimeError(
        "catalog refresh fixture lease did not expire while the app call was blocked"
    )


async def _wait_for_paged_refresh_lease_expiry(
    connection: AsyncConnection,
    source_key: str,
    run_key: str,
) -> None:
    """Retain the P15 fixture helper name while reusing the generic database-clock wait."""
    await _wait_for_catalog_refresh_lease_expiry(connection, source_key, run_key)


async def _expire_owner_refresh_lease(source_key: str, run_key: str, lease_token: UUID) -> None:
    """Expire one exact lease as the migration owner without granting app-row access (NFR-8)."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.begin() as connection:
            result = await connection.execute(
                text(
                    """
                    UPDATE public.catalog_refresh_runs
                    SET lease_expires_at = pg_catalog.clock_timestamp() - INTERVAL '1 second'
                    WHERE source_key = :source_key
                      AND run_key = :run_key
                      AND lease_token = :lease_token
                    """
                ),
                {
                    "source_key": source_key,
                    "run_key": run_key,
                    "lease_token": lease_token,
                },
            )
            if result.rowcount != 1:
                raise RuntimeError("catalog refresh fixture lease did not expire exactly once")
    finally:
        await owner.dispose()


async def _owner_refresh_state(source_key: str, run_key: str) -> dict[str, object]:
    """Read full fixture state as owner to prove an app-role stale call was a true no-op (NFR-8)."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.connect() as connection:
            row = (
                (
                    await connection.execute(
                        text(
                            """
                        SELECT refresh.status,
                               refresh.lease_token,
                               refresh.lease_expires_at,
                               refresh.started_at,
                               refresh.completed_at,
                               refresh.candidate_count,
                               refresh.canonical_count,
                               refresh.error,
                               refresh.attempt_count,
                               (
                                   SELECT count(*)
                                   FROM public.catalog_refresh_progress AS progress
                                   WHERE progress.source_key = refresh.source_key
                                     AND progress.run_key = refresh.run_key
                               ) AS progress_count,
                               (
                                   SELECT progress.next_page
                                   FROM public.catalog_refresh_progress AS progress
                                   WHERE progress.source_key = refresh.source_key
                                     AND progress.run_key = refresh.run_key
                               ) AS progress_next_page,
                               (
                                   SELECT progress.terminal_page
                                   FROM public.catalog_refresh_progress AS progress
                                   WHERE progress.source_key = refresh.source_key
                                     AND progress.run_key = refresh.run_key
                               ) AS progress_terminal_page,
                               (
                                   SELECT progress.staged_raw_count
                                   FROM public.catalog_refresh_progress AS progress
                                   WHERE progress.source_key = refresh.source_key
                                     AND progress.run_key = refresh.run_key
                               ) AS progress_staged_raw_count,
                               (
                                   SELECT progress.staged_candidate_count
                                   FROM public.catalog_refresh_progress AS progress
                                   WHERE progress.source_key = refresh.source_key
                                     AND progress.run_key = refresh.run_key
                               ) AS progress_staged_candidate_count,
                               (
                                   SELECT count(*)
                                   FROM public.catalog_refresh_stage_pages AS page
                                   WHERE page.source_key = refresh.source_key
                                     AND page.run_key = refresh.run_key
                               ) AS stage_page_count,
                               (
                                   SELECT count(*)
                                   FROM public.catalog_refresh_stage_candidates AS candidate
                                   WHERE candidate.source_key = refresh.source_key
                                     AND candidate.run_key = refresh.run_key
                               ) AS stage_candidate_count,
                               (
                                   SELECT count(*)
                                   FROM public.catalog_refresh_stage_event_ids AS event_id
                                   WHERE event_id.source_key = refresh.source_key
                                     AND event_id.run_key = refresh.run_key
                               ) AS stage_event_id_count
                        FROM public.catalog_refresh_runs AS refresh
                        WHERE refresh.source_key = :source_key
                          AND refresh.run_key = :run_key
                        """
                        ),
                        {"source_key": source_key, "run_key": run_key},
                    )
                )
                .mappings()
                .one()
            )
        return {str(key): value for key, value in row.items()}
    finally:
        await owner.dispose()


async def _set_owner_source_min_interval(source_key: str, min_interval_ms: int) -> None:
    """Apply one temporary owner-only request-contract edit for P15b revision coverage."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.catalog_sources
                    SET min_interval_ms = :min_interval_ms,
                        updated_at = clock_timestamp()
                    WHERE source_key = :source_key
                    """
                ),
                {"source_key": source_key, "min_interval_ms": min_interval_ms},
            )
    finally:
        await owner.dispose()


async def _set_owner_source_transport_contract(
    source_key: str,
    *,
    seed_url: str,
    approved_origins: tuple[str, ...],
) -> None:
    """Apply one temporary owner-only endpoint/origin edit for exact-profile guard coverage."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE public.catalog_sources
                    SET seed_url = :seed_url,
                        approved_origins = :approved_origins,
                        updated_at = clock_timestamp()
                    WHERE source_key = :source_key
                    """
                ),
                {
                    "source_key": source_key,
                    "seed_url": seed_url,
                    "approved_origins": list(approved_origins),
                },
            )
    finally:
        await owner.dispose()
