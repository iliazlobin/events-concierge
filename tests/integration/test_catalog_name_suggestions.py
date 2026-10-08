"""Autocomplete searches all admitted names, not the first chronological event page."""

import os
from dataclasses import replace
from datetime import timedelta
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from tests.integration.test_catalog_browse import _source_with_current_events

from events_concierge.adapters.postgres.catalog_observations import (
    PostgresCatalogObservationRepository,
)
from events_concierge.adapters.postgres.catalog_refresh_commit import (
    PostgresCatalogRefreshCommitter,
)
from events_concierge.adapters.postgres.catalog_sources import PostgresCatalogSourceRepository
from events_concierge.domain.enums import RegistrationStatus
from events_concierge.domain.events import CandidateEvent, EventEntityProfile
from events_concierge.infra.db import system_session_scope

pytestmark = pytest.mark.integration


async def _publish_names(source: str, candidates: list[CandidateEvent], catalog) -> None:
    runs = PostgresCatalogSourceRepository()
    run_key = f"manual:company-names-{uuid4().hex}"
    claim = await runs.claim_refresh(source, run_key, lease_seconds=300)
    committer = PostgresCatalogRefreshCommitter(catalog, PostgresCatalogObservationRepository())
    assert await committer.commit_refresh(
        source, run_key, lease_token=claim.lease_token, candidates=candidates,
    ) is not None


async def test_names_beyond_first_page_with_role_counts_and_literal_punctuation(db: None) -> None:
    source, current, _, catalog = await _source_with_current_events(event_count=76)
    # Earlier events mention Nebius only in prose. They must neither use up the name limit
    # nor crowd out a structured company name on the last chronological event.
    candidates = [replace(event, description="Nebius appears only in the description.")
                  for event in current]
    candidates[-1] = replace(
        candidates[-1], title="Nebius Builders Night", venue_name="Nebius Studio",
        organizer_name="A%_B Collective", host_names=("Nebius",),
        speaker_names=("Mira Developer",), partner_names=("Cloud Partner",),
        registration_status=RegistrationStatus.OPEN,
        entity_profiles=(EventEntityProfile(
            name="Nebius", role="host", kind="organization",
            profile_url="https://www.linkedin.com/company/nebius",
        ),),
    )
    candidates[-2] = replace(candidates[-2], host_names=("NEBIUS",), entity_profiles=())
    runs = PostgresCatalogSourceRepository()
    committer = PostgresCatalogRefreshCommitter(catalog, PostgresCatalogObservationRepository())
    run_key = f"manual:autocomplete-{uuid4().hex}"
    claim = await runs.claim_refresh(source, run_key, lease_seconds=300)
    assert await committer.commit_refresh(source, run_key, lease_token=claim.lease_token,
                                          candidates=candidates) is not None
    first, _ = await catalog.browse_current(source_keys=(source,), after=None, limit=72)
    assert not any(name.lower() == "nebius"
                   for item in first for name in item.canonical_event.host_names)
    matches = await catalog.suggest_names(query="nebius", source_keys=(source,), limit=1)
    assert len(matches) == 1
    assert matches[0].name.lower() == "nebius"
    assert matches[0].kinds == ("host", "organization")
    assert matches[0].event_count == 2
    all_matches = await catalog.suggest_names(query="nebius", source_keys=(source,))
    assert {item.name.lower() for item in all_matches} == {
        "nebius", "nebius builders night", "nebius studio",
    }
    open_matches = await catalog.suggest_names(
        query="nebius", source_keys=(source,), availability="available",
    )
    assert next(item for item in open_matches if item.name.lower() == "nebius").event_count == 1
    for query, name, kinds in [
        ("%_", "A%_B Collective", ("organization", "organizer")),
        ("mira", "Mira Developer", ("speaker",)),
        ("cloud", "Cloud Partner", ("organization", "partner")),
    ]:
        result = await catalog.suggest_names(query=query, source_keys=(source,))
        assert [(item.name, item.kinds) for item in result] == [(name, kinds)]
        selected, _ = await catalog.browse_current(
            source_keys=(source,), after=None, limit=5, query=name,
        )
        assert [item.canonical_event.title for item in selected] == [candidates[-1].title]

    # An event crossing two disjoint windows and multiple mentions still counts only once.
    start = candidates[-1].start_at
    windows = ((start, start + timedelta(minutes=20)),
               (start + timedelta(minutes=40), start + timedelta(hours=1)))
    ranged = await catalog.suggest_names(query="nebius", source_keys=(source,), date_ranges=windows)
    assert all(item.event_count == 1 for item in ranged)
    assert await catalog.suggest_names(query="nebius", source_keys=(source,), cities=("Oakland",)) == []


async def test_fixture_names_and_noncurrent_observations_are_not_public(db: None) -> None:
    fixture_source, _, _, catalog = await _source_with_current_events(fixture=True)
    assert await catalog.suggest_names(query="Luma", source_keys=(fixture_source,)) == []
    source, _, _, catalog = await _source_with_current_events()
    assert await catalog.suggest_names(query="Stale", source_keys=(source,)) == []
    # The app role can execute only the bounded public function, never its unbounded primitive.
    async with system_session_scope() as session:
        grants = (await session.execute(text("""
            SELECT has_function_privilege(current_user,
                'public.fn_browse_filtered_current_catalog_events_unbounded_v1(text[],timestamptz,timestamptz,text,text[],text[],text,integer,integer,text[],text,timestamptz,uuid,integer)',
                'EXECUTE')
        """))).scalar_one()
        assert grants is False


async def test_every_indexed_company_and_recorded_variant_reaches_its_events(db: None) -> None:
    first_source, first, _, catalog = await _source_with_current_events(event_count=42)
    second_source, second, _, _ = await _source_with_current_events(event_count=42)
    tag = uuid4().hex
    labels = ["A%_B Collective", "C++ Labs", "R&D, Inc.", "東京 Studio", "Εταιρεία"]
    names = [f"{labels[index] if index < len(labels) else 'Company'} {tag} {index:02}"
             for index in range(42)]
    old_names = [f"Recorded Team {tag} {index:02}" for index in range(42)]

    def companies(events: list[CandidateEvent], variants: list[str]) -> list[CandidateEvent]:
        return [replace(
            event, title=f"Unrelated workshop {uuid4().hex}", organizer_name=None,
            host_names=(name,), partner_names=(),
            entity_profiles=(EventEntityProfile(
                name=name, role="host", kind="organization",
                profile_url=f"https://www.linkedin.com/company/company-{tag}-{index}",
            ),), registration_status=RegistrationStatus.OPEN,
        ) for index, (event, name) in enumerate(zip(events, variants, strict=True))]

    first = companies(first, old_names)
    second = companies(second, names)
    await _publish_names(first_source, first, catalog)
    await _publish_names(second_source, second, catalog)
    # Cross-source identity comes from the same exact direct company URL, never from the name.
    # The second publication updates the indexed display name, without rewriting older cards.
    for company, recorded, event in zip(names, old_names, first, strict=True):
        suggestions = await catalog.suggest_names(query=company, source_keys=(first_source,))
        exact = next(item for item in suggestions if item.name == company)
        assert exact.kinds == ("organization",)
        assert exact.event_count == 1
        selected, _ = await catalog.browse_current(
            query=company, source_keys=(first_source,), after=None, limit=5,
        )
        assert [item.canonical_event.title for item in selected] == [event.title]
        original = await catalog.suggest_names(query=recorded, source_keys=(first_source,))
        assert next(item for item in original if item.name == recorded).event_count == 1
        both = await catalog.suggest_names(query=company, source_keys=(first_source, second_source))
        assert next(item for item in both if item.name == company).event_count == 2

    selected_company = names[-1]
    assert await catalog.suggest_names(
        query=selected_company, source_keys=(first_source,), cities=("Oakland",),
    ) == []
    assert await catalog.suggest_names(
        query=selected_company, source_keys=(first_source,), availability="sold_out",
    ) == []
    start = first[-1].start_at
    filtered = await catalog.suggest_names(
        query=selected_company, source_keys=(first_source,), availability="available",
        date_ranges=((start, start + timedelta(hours=1)),), cities=("San Francisco",),
    )
    assert [(item.name, item.event_count) for item in filtered] == [(selected_company, 1)]
    facets = await catalog.list_day_facets(
        source_keys=(first_source,), starts_after=start, starts_before=start + timedelta(hours=1),
        query=selected_company, cities=(), location_scopes=(), price=None,
        price_max_cents=None, topics=(), time_zone="UTC",
    )
    assert sum(day.event_count for day in facets) == 1
    # Refresh removes old-run company mentions immediately, without another name-index job.
    await _publish_names(first_source, first[:-1], catalog)
    assert await catalog.suggest_names(query=selected_company, source_keys=(first_source,)) == []
    selected, _ = await catalog.browse_current(
        query=selected_company, source_keys=(first_source,), after=None, limit=5,
    )
    assert selected == []


async def test_company_index_never_exposes_fixture_or_stale_source_names(db: None) -> None:
    source, current, _, catalog = await _source_with_current_events()
    fixture, fixture_events, _, _ = await _source_with_current_events(fixture=True)
    tag = uuid4().hex
    name = f"Literal %_ Company {tag}"
    profile = f"https://www.linkedin.com/company/literal-{tag}"
    candidate = replace(current[0], organizer_name=name, host_names=(), entity_profiles=(
        EventEntityProfile(name=name, role="organizer", kind="organization", profile_url=profile),
    ))
    await _publish_names(source, [candidate], catalog)
    # A newer fixture asserting an alias of this company cannot provide public event counts.
    alias = f"FixtureCompany{tag}"
    await _publish_names(fixture, [replace(
        fixture_events[0], organizer_name=alias, host_names=(), entity_profiles=(
            EventEntityProfile(name=alias, role="organizer", kind="organization", profile_url=profile),
        ),
    )], catalog)
    assert await catalog.suggest_names(query=alias, source_keys=(fixture,)) == []
    literal = await catalog.suggest_names(query="%_", source_keys=(source,))
    assert any(item.name == name for item in literal)
    assert all("%_" in item.name for item in literal)

    owner = create_async_engine(os.environ["EC_MIGRATION_URL"])
    try:
        async with owner.begin() as connection:
            await connection.execute(text(
                "UPDATE public.catalog_sources SET enabled=false WHERE source_key=:source"
            ), {"source": source})
        assert await catalog.suggest_names(query=name, source_keys=(source,)) == []
        selected, _ = await catalog.browse_current(query=name, source_keys=(source,), after=None, limit=5)
        assert selected == []
    finally:
        await owner.dispose()


async def test_company_name_upgrade_and_downgrade_preserve_security_and_data(db: None) -> None:
    owner = create_async_engine(os.environ["EC_MIGRATION_URL"])
    migration_file = Path(__file__).parents[2] / "migrations/versions/0213_indexed_company_names.py"
    spec = spec_from_file_location("company_name_migration", migration_file)
    assert spec is not None and spec.loader is not None
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    try:
        async with owner.begin() as connection:
            snapshot_query = text("""
                SELECT oid::regprocedure::text, proowner, prosecdef, provolatile,
                       proconfig, proacl, pg_get_functiondef(oid)
                FROM pg_proc
                WHERE oid IN (CAST(:browse AS regprocedure), CAST(:names AS regprocedure))
                ORDER BY oid
            """)
            signatures = {"browse": migration._BROWSE, "names": migration._NAMES}
            before = (await connection.execute(snapshot_query, signatures)).all()
            counts_query = text(
                "SELECT (SELECT count(*) FROM canonical_events), "
                "(SELECT count(*) FROM catalog_entities), "
                "(SELECT count(*) FROM catalog_entity_event_mentions)"
            )
            counts = (await connection.execute(counts_query)).one()

            def roundtrip(sync_connection):
                with Operations.context(MigrationContext.configure(sync_connection)):
                    migration.downgrade()
                    migration.upgrade()

            await connection.run_sync(roundtrip)
            assert (await connection.execute(snapshot_query, signatures)).all() == before
            assert (await connection.execute(counts_query)).one() == counts
            for signature in signatures.values():
                assert (await connection.execute(text(
                    "SELECT has_function_privilege('ec_app', :signature, 'EXECUTE')"
                ), {"signature": signature})).scalar_one() is (signature == migration._NAMES)
                assert (await connection.execute(text("""
                    SELECT EXISTS(SELECT 1 FROM pg_proc, LATERAL aclexplode(proacl) AS acl
                        WHERE oid=CAST(:signature AS regprocedure) AND acl.grantee=0)
                """), {"signature": signature})).scalar_one() is False
    finally:
        await owner.dispose()
