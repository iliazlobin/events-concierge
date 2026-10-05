"""Real PostgreSQL edition admission, frozen dates and distinct public event identities."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from tests.integration.test_operator_management import _denied, _owner_transaction, _role
from tests.integration.test_source_collection_windows import _claim, _prepare
from tests.unit.test_tech_week_source import _event

from events_concierge.adapters.luma_calendar.source import reviewed_calendar_api_id
from events_concierge.adapters.postgres.catalog import PostgresCatalogRepository
from events_concierge.adapters.ranking.embedding import DeterministicEmbedding
from events_concierge.adapters.tech_week.source import _PROFILES, _candidate
from events_concierge.domain.enums import PriceStatus, Source

pytestmark = pytest.mark.integration


async def _activate(connection, city):
    await _role(connection, "ec_operator_controller")
    row = (await connection.execute(text("""
        SELECT * FROM public.fn_update_ingestion_admin_source_configuration_v3(
          :source,1,:url,ARRAY['https://www.tech-week.com'],'tech_week_mcp',true,true,NULL,
          180,1500,40,'tech-week-test',15
        )
    """), {"source": f"tech-week-{city}-2026", "url": f"https://www.tech-week.com/calendar/{city}"})).mappings().one()
    assert row["outcome"] == "updated" and row["source_revision"] == 2


async def test_migration_registers_only_disabled_unreviewed_handoff_sources_and_preserves_authority():
    async with _owner_transaction() as connection:
        rows = (await connection.execute(text("""
            SELECT source_key,mode,enabled,reviewed_at,page_limit,min_interval_ms,
                   collection_horizon_days,refresh_interval_minutes,handoff_only
            FROM public.catalog_sources WHERE mode='tech_week_mcp' ORDER BY source_key
        """))).mappings().all()
        assert [row["source_key"] for row in rows] == ["tech-week-la-2026", "tech-week-sf-2026"]
        assert all(not row["enabled"] and row["reviewed_at"] is None and row["handoff_only"]
                   and row["page_limit"] == 40 and row["min_interval_ms"] == 1500
                   and row["collection_horizon_days"] == 15 and row["refresh_interval_minutes"] == 180 for row in rows)
        community = (await connection.execute(text("""
            SELECT source_key,seed_url,approved_origins,enabled,reviewed_at,page_limit
            FROM public.catalog_sources WHERE source_key IN ('luma-sf-tech-week-2026','luma-la-tech-week-2026')
            ORDER BY source_key
        """))).mappings().all()
        assert len(community) == 2 and all(not row["enabled"] and row["reviewed_at"] is None
                                          and row["page_limit"] == 30 for row in community)
        assert [reviewed_calendar_api_id(row["seed_url"]) for row in community] == [
            "cal-BVYkgFSqxs8DEOL", "cal-bR2dxhC1V6wCtK8",
        ]
        assert all(set(row["approved_origins"]) == {"https://api.luma.com", "https://api2.luma.com"} for row in community)
        function = (await connection.execute(text("""
            SELECT p.prosecdef,p.proconfig,r.rolname AS owner FROM pg_proc p
            JOIN pg_roles r ON r.oid=p.proowner
            WHERE p.oid='public.fn_prepare_catalog_collection_window_v1(text,text,uuid,integer)'::regprocedure
        """))).mappings().one()
        owner = (await connection.execute(text("""
            SELECT r.rolname FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
            WHERE p.oid='public.fn_claim_catalog_refresh(text,text,integer,uuid)'::regprocedure
        """))).scalar_one()
        assert function["prosecdef"] and function["owner"] == owner
        assert function["proconfig"] == ["search_path=pg_catalog, public"]
        for role in ("ec_app", "ec_operator_controller", "ec_ingestion_executor"):
            await _role(connection, role)
            await _denied(connection, "INSERT INTO public.catalog_sources(source_key) VALUES ('unreviewed-new-source')")
        await _role(connection, "ec_operator_viewer")
        await _denied(connection, "SELECT public.fn_prepare_catalog_collection_window_v1('tech-week-sf-2026','blocked',NULL,1)")


@pytest.mark.parametrize("city", ["sf", "la"])
async def test_reviewed_editions_freeze_the_full_requested_dates_with_lease_and_revision_fences(city):
    async with _owner_transaction() as connection:
        source = f"tech-week-{city}-2026"
        run = "tech-week-test-" + uuid4().hex
        await _activate(connection, city)
        token = await _claim(connection, source, run)
        assert await _prepare(connection, source, run, token, 1) is None
        assert await _prepare(connection, source, run, uuid4(), 2) is None
        result = await _prepare(connection, source, run, token, 2)
        assert result is not None
        await _role(connection, None)
        frozen = (await connection.execute(text("""
            SELECT start_at,end_at,horizon_days FROM public.catalog_refresh_collection_windows
            WHERE source_key=:source AND run_key=:run
        """), {"source": source, "run": run})).mappings().one()
        assert frozen["start_at"] == datetime(2026, 10, 5, 7, tzinfo=UTC)
        assert frozen["end_at"] == datetime(2026, 10, 20, 7, tzinfo=UTC)
        assert frozen["horizon_days"] == 15
        # After the edition closes the same immutable window is expired, never rolled forward.
        assert await _prepare(connection, source, run, token, 2) == result


async def test_distinct_official_ids_are_not_lost_to_fuzzy_titles_and_exact_replays_are_stable():
    async with _owner_transaction() as connection:
        @asynccontextmanager
        async def scope():
            async with AsyncSession(bind=connection, expire_on_commit=False) as session:
                yield session

        catalog = PostgresCatalogRepository(DeterministicEmbedding(), session_scope=scope)
        identity = uuid4().int
        first = _candidate(_event(identity), _PROFILES["tech-week-sf-2026"])
        # Other publishers may deduplicate only when title, start and venue agree exactly.
        other = replace(first, source=Source.LUMA, source_event_id="cross-publisher-" + uuid4().hex)
        original = (await catalog.upsert_candidates([other]))[0]
        first = replace(first, description="Free snacks and drinks at this networking event")
        second = _candidate(_event(identity + 1), _PROFILES["tech-week-sf-2026"])
        third = replace(_candidate(_event(identity + 2), _PROFILES["tech-week-sf-2026"]), venue_name=None)
        events = await catalog.upsert_candidates([first, second, third])
        assert events[0].canonical_event_id == original.canonical_event_id
        assert len({event.canonical_event_id for event in events}) == 3
        repeated = await catalog.upsert_candidates([first, second, third])
        assert [event.canonical_event_id for event in repeated] == [event.canonical_event_id for event in events]
        assert all(event.price_status is PriceStatus.UNKNOWN for event in repeated)
        assert all("ai" in event.topics for event in repeated)
        assert {link.source_event_id for event in repeated for link in event.source_links
                if link.source_event_id.startswith("tech-week:")} == {first.source_event_id, second.source_event_id, third.source_event_id}
        later_luma = replace(second, source=Source.LUMA, source_event_id="other-" + uuid4().hex,
                             start_at=second.start_at + timedelta(hours=1))
        later = (await catalog.upsert_candidates([later_luma]))[0]
        assert later.canonical_event_id not in {event.canonical_event_id for event in repeated}
