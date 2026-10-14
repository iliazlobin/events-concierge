"""Provider visibility survives source publication and uses the same public admission gate."""

import os
from dataclasses import replace
from importlib import import_module
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.test_catalog_browse import _source_with_current_events
from tests.integration.test_catalog_name_suggestions import _publish_names

from events_concierge.api.app import _catalog_browse_item_out, _database_is_ready, create_app
from events_concierge.api.catalog_reads import CatalogReadCoordinator
from events_concierge.infra.db import system_session_scope

pytestmark = pytest.mark.integration


async def test_vendor_source_counts_respect_place_and_registration_filters(db) -> None:
    source, _, _, catalog = await _source_with_current_events(event_count=2)
    _, providers = await catalog.browse_current(
        source_keys=(source,), cities=("Oakland",), after=None, limit=10
    )
    assert (
        next(provider for provider in providers if provider.source_key == source).event_count == 0
    )
    _, providers = await catalog.browse_current(
        source_keys=(source,), cities=("San Francisco",), after=None, limit=10
    )
    assert (
        next(provider for provider in providers if provider.source_key == source).event_count == 2
    )
    _, providers = await catalog.browse_current(
        source_keys=(source,), availability="sold_out", after=None, limit=10
    )
    assert (
        next(provider for provider in providers if provider.source_key == source).event_count == 0
    )


async def test_private_card_visibility_is_admitted_source_metadata_not_a_global_guess(db) -> None:
    source, events, _, catalog = await _source_with_current_events(event_count=1)
    [candidate] = events
    candidate = replace(
        candidate,
        visibility="private",
        location_withheld=True,
        geo=None,
        venue_name=None,
        organizer_name="SemiAnalysis",
        entity_profiles=(),
        title="Silicon After Dark " + uuid4().hex,
    )
    await _publish_names(source, [candidate], catalog)
    [item], _ = await catalog.browse_current(source_keys=(source,), after=None, limit=10)
    assert item.sources[0].visibility == "private"
    output = _catalog_browse_item_out(item)
    assert output.visibility == "private" and output.sources[0].visibility == "private"
    assert output.latitude is None and output.longitude is None and output.venue_name is None
    detail = await catalog.get_browse_event(item.canonical_event.canonical_event_id)
    assert detail is not None and detail.sources[0].visibility == "private"
    async with system_session_scope() as session:
        denied = await session.execute(
            text("""
            SELECT public.fn_catalog_observation_card_v1(
                :source, 'public_jsonld', 'undisclosed-id', :event_id)
        """),
            {"source": source, "event_id": item.canonical_event.canonical_event_id},
        )
        assert denied.scalar_one() is None


async def test_unadmitted_fixture_visibility_cannot_be_queried_by_id(db) -> None:
    source, events, _, catalog = await _source_with_current_events(fixture=True, event_count=1)
    await _publish_names(source, [replace(events[0], visibility="private")], catalog)
    async with system_session_scope() as session:
        row = (
            await session.execute(
                text("""
            SELECT canonical_event_id, source, source_event_id
            FROM catalog_event_observations WHERE source_key=:source AND source_event_id=:identity
        """),
                {"source": source, "identity": events[0].source_event_id},
            )
        ).one()
        label = (
            await session.execute(
                text("""
            SELECT public.fn_catalog_observation_card_v1(:source,:kind,:identity,:cid)
        """),
                {
                    "source": source,
                    "kind": row.source,
                    "identity": row.source_event_id,
                    "cid": row.canonical_event_id,
                },
            )
        ).scalar_one()
        assert label is None


async def test_cached_public_cards_invalidate_on_publication_and_source_revocation(db) -> None:
    source, candidates, _, catalog = await _source_with_current_events(event_count=1)
    app = create_app()
    app.state.container = SimpleNamespace(catalog=catalog)
    app.state.catalog_reads = CatalogReadCoordinator(2)
    params = {"source_key": source}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        first = await client.get("/v1/catalog/events", params=params)
        hit = await client.get("/v1/catalog/events", params=params)
        assert first.status_code == hit.status_code == 200
        assert hit.headers["x-ec-catalog-cache"] == "hit"
        assert hit.json() == first.json()
        assert hit.headers["cache-control"] == "no-store"
        updated = replace(candidates[0], title="Published title " + uuid4().hex,
                          visibility="private", location_withheld=True, geo=None, venue_name=None)
        await _publish_names(source, [updated], catalog)
        refreshed = await client.get("/v1/catalog/events", params=params)
        assert refreshed.headers["x-ec-catalog-cache"] == "miss"
        [card] = refreshed.json()["items"]
        assert card["title"] == updated.title and card["visibility"] == "private"
        assert card["venue_name"] is None and card["latitude"] is None
        owner = create_async_engine(os.environ["EC_MIGRATION_URL"])
        try:
            async with owner.begin() as connection:
                await connection.execute(text(
                    "UPDATE catalog_sources SET enabled=false WHERE source_key=:source"
                ), {"source": source})
            revoked = await client.get("/v1/catalog/events", params=params)
            assert revoked.headers["x-ec-catalog-cache"] == "miss"
            assert revoked.json()["items"] == []
        finally:
            await owner.dispose()


async def test_read_capability_rollback_preserves_visibility_data_and_private_grants(db) -> None:
    source, candidates, _, catalog = await _source_with_current_events(event_count=1)
    await _publish_names(source, [replace(
        candidates[0], visibility="private", location_withheld=True, geo=None, venue_name=None,
    )], catalog)
    [item], _ = await catalog.browse_current(source_keys=(source,), after=None, limit=10)
    migration = import_module("migrations.versions.0220_catalog_read_performance")
    owner = create_async_engine(os.environ["EC_MIGRATION_URL"])
    try:
        async with owner.begin() as connection:
            before = (await connection.execute(text(
                "SELECT count(*) FROM catalog_event_observations WHERE visibility='private'"
            ))).scalar_one()

            def downgrade(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    migration.downgrade()

            await connection.run_sync(downgrade)
        assert await _database_is_ready() is False
        async with owner.begin() as connection:

            def upgrade(sync):
                with Operations.context(MigrationContext.configure(sync)):
                    migration.upgrade()

            await connection.run_sync(upgrade)
            after = (await connection.execute(text(
                "SELECT count(*) FROM catalog_event_observations WHERE visibility='private'"
            ))).scalar_one()
            assert before == after
            for signature in (
                "public.fn_catalog_public_read_token_v1()",
                "public.fn_catalog_observation_card_v1(text,text,text,uuid)",
            ):
                assert (await connection.execute(text(
                    "SELECT has_function_privilege('ec_app',:signature,'EXECUTE')"
                ), {"signature": signature})).scalar_one()
                assert not (await connection.execute(text("""
                    SELECT EXISTS(SELECT 1 FROM pg_proc AS proc,
                      aclexplode(coalesce(proc.proacl,acldefault('f',proc.proowner))) AS acl
                      WHERE proc.oid=CAST(:signature AS regprocedure)
                        AND acl.grantee=0 AND acl.privilege_type='EXECUTE')
                """), {"signature": signature})).scalar_one()
        assert await _database_is_ready() is True
        detail = await catalog.get_browse_event(item.canonical_event.canonical_event_id)
        assert detail is not None and detail.sources[0].visibility == "private"
    finally:
        await owner.dispose()
