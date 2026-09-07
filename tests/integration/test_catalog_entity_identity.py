"""Corroborated source-scoped entity identity (migration 0171).

A display name is not a cross-source identity, so an entity with no verified profile URL stays
scoped to the source that asserted it.  These tests pin the one case that overrides that: when two
sources assert the same role, for the same name, on the *same canonical event*, the shared event
has done the identifying and the two assertions are one entity.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from events_concierge.adapters.postgres.catalog_refresh_commit import (
    PostgresCatalogRefreshCommitter,
)
from events_concierge.adapters.postgres.catalog_sources import PostgresCatalogSourceRepository
from events_concierge.composition import build_container
from events_concierge.config import get_settings
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import CatalogSourceMode, Source
from events_concierge.domain.events import CandidateEvent

pytestmark = pytest.mark.integration


def _source(source_key: str) -> CatalogSource:
    return CatalogSource(
        source_key=source_key,
        display_name=f"Identity fixture {source_key}",
        publisher="Identity fixture publisher",
        # Deliberately NOT an example.test host: fn_ingestion_admin_source_is_fixture treats
        # those as the test suite's own leftovers and the browse projection drops them, so a
        # fixture-shaped source would silently project no entities at all.
        seed_url=f"https://{source_key}.identity-fixture.invalid/catalog",
        approved_origins=(f"https://{source_key}.identity-fixture.invalid",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=datetime.now(UTC) - timedelta(minutes=1),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1_500,
    )


async def _seed_owner_source(source: CatalogSource) -> None:
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
                        (:source_key, :display_name, :publisher, :seed_url, :approved_origins,
                         :region, :mode, true, true, :reviewed_at, NULL, 60, 1500, 1)
                    ON CONFLICT (source_key) DO NOTHING
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
                    "reviewed_at": source.reviewed_at,
                },
            )
    finally:
        await owner.dispose()


def _candidate(
    *,
    registration_url: str,
    title: str,
    host: str,
    start_at: datetime,
) -> CandidateEvent:
    """One listing of one event.

    ``start_at`` is explicit because the catalog deduplicates on title and start time, not on the
    registration URL: two listings that are meant to be the same canonical event must carry the
    identical instant, and two that are meant to be different must not.
    """
    return CandidateEvent(
        source=Source.PUBLIC_JSONLD,
        source_event_id=registration_url,
        title=title,
        start_at=start_at,
        registration_url=registration_url,
        host_names=(host,),
    )


async def _publish(source_key: str, candidate: CandidateEvent) -> None:
    """Run one whole reviewed refresh so the entity projection runs exactly as it does live."""
    await _seed_owner_source(_source(source_key))
    container = build_container(get_settings())
    repository = PostgresCatalogSourceRepository()
    committer = PostgresCatalogRefreshCommitter(
        container.catalog,
        container.catalog_observation_repo,
    )
    run_key = f"manual:identity-{uuid4().hex}"
    claim = await repository.claim_refresh(source_key, run_key, lease_seconds=120)
    assert claim.acquired and claim.lease_token is not None
    commit = await committer.commit_refresh(
        source_key,
        run_key,
        lease_token=claim.lease_token,
        candidates=[candidate],
    )
    assert commit is not None


async def _entities_named(name: str) -> list[tuple[str, set[str], int]]:
    """Read the projection as the migration owner; ``ec_app`` has no direct grant on these tables."""
    migration_url = os.environ.get("EC_MIGRATION_URL")
    if migration_url is None:
        pytest.skip("EC_MIGRATION_URL not set; run the integration target")
    owner = create_async_engine(migration_url, pool_pre_ping=True)
    try:
        async with owner.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        """
                        SELECT entity.identity_key,
                               array_agg(DISTINCT mention.source_key) AS source_keys,
                               count(DISTINCT mention.canonical_event_id)::integer AS events
                        FROM public.catalog_entities AS entity
                        JOIN public.catalog_entity_event_mentions AS mention
                          ON mention.entity_id = entity.entity_id
                        WHERE entity.display_name = :name
                        GROUP BY entity.identity_key
                        ORDER BY entity.identity_key
                        """
                    ),
                    {"name": name},
                )
            ).all()
    finally:
        await owner.dispose()
    return [(row.identity_key, set(row.source_keys), row.events) for row in rows]


async def test_two_sources_naming_one_host_on_one_event_are_one_entity(db: None) -> None:
    """The shared canonical event is the identifying evidence, not the matching name."""
    host = f"Corroborated Host {uuid4().hex[:8]}"
    url = f"https://identity-fixture.invalid/{uuid4().hex[:10]}"
    start_at = datetime.now(UTC) + timedelta(days=21)
    shelf, calendar = f"idfix-shelf-{uuid4().hex[:8]}", f"idfix-cal-{uuid4().hex[:8]}"
    listing = _candidate(registration_url=url, title=host, host=host, start_at=start_at)

    await _publish(shelf, listing)
    await _publish(calendar, listing)

    entities = await _entities_named(host)

    assert len(entities) == 1, entities
    _, source_keys, events = entities[0]
    assert source_keys == {shelf, calendar}
    assert events == 1


async def test_the_merged_identity_key_does_not_depend_on_refresh_order(db: None) -> None:
    """Every member of a component must derive the same key, or hub and leaf never converge."""
    host = f"Ordered Host {uuid4().hex[:8]}"
    url = f"https://identity-fixture.invalid/{uuid4().hex[:10]}"
    start_at = datetime.now(UTC) + timedelta(days=21)
    first, second = f"idfix-a-{uuid4().hex[:8]}", f"idfix-b-{uuid4().hex[:8]}"
    listing = _candidate(registration_url=url, title=host, host=host, start_at=start_at)

    await _publish(first, listing)
    await _publish(second, listing)
    forward = await _entities_named(host)
    # Refreshing the first source again must not move the identity back to its own key.
    await _publish(first, listing)
    reversed_order = await _entities_named(host)

    assert len(forward) == 1
    assert forward == reversed_order


async def test_the_same_name_on_different_events_stays_two_entities(db: None) -> None:
    """Without a shared event there is no corroboration, and a name alone is not an identity."""
    host = f"Ambiguous Host {uuid4().hex[:8]}"
    left, right = f"idfix-l-{uuid4().hex[:8]}", f"idfix-r-{uuid4().hex[:8]}"

    await _publish(
        left,
        _candidate(
            registration_url=f"https://identity-fixture.invalid/{uuid4().hex[:10]}",
            title=f"{host} on the left",
            host=host,
            start_at=datetime.now(UTC) + timedelta(days=21),
        ),
    )
    await _publish(
        right,
        _candidate(
            registration_url=f"https://identity-fixture.invalid/{uuid4().hex[:10]}",
            title=f"{host} somewhere else entirely",
            host=host,
            start_at=datetime.now(UTC) + timedelta(days=44),
        ),
    )

    entities = await _entities_named(host)

    assert len(entities) == 2, entities
    assert [source_keys for _, source_keys, _ in entities] != [{left, right}]


async def _rebuild_all() -> None:
    """Run the full projection, which is the answer a per-source refresh must agree with."""
    owner = create_async_engine(os.environ["EC_MIGRATION_URL"], pool_pre_ping=True)
    try:
        async with owner.begin() as connection:
            await connection.execute(
                text("SELECT public.fn_refresh_catalog_entity_index_v3(NULL)")
            )
    finally:
        await owner.dispose()


async def test_an_entity_survives_the_source_that_named_it_going_quiet(db: None) -> None:
    """A member that stops asserting must not leave the entity keyed to it.

    That source appears in neither branch of the component computation, so a repair driven by the
    component map never selects its entity — and the prune spares it, because it is still holding
    the other member's mentions. The row would then stand keyed to a source with no assertion
    behind it, and a per-source refresh and a full rebuild would disagree about the same evidence.
    """
    host = f"Outliving Host {uuid4().hex[:8]}"
    url = f"https://identity-fixture.invalid/{uuid4().hex[:10]}"
    start_at = datetime.now(UTC) + timedelta(days=21)
    # 'aaa' sorts first, so it becomes the component representative and the entity's key.
    first, second = f"idfix-aaa-{uuid4().hex[:8]}", f"idfix-zzz-{uuid4().hex[:8]}"
    shared = _candidate(registration_url=url, title=host, host=host, start_at=start_at)

    await _publish(first, shared)
    await _publish(second, shared)
    assert len(await _entities_named(host)) == 1

    # The first source's feed drops the event: its next refresh carries something else entirely.
    await _publish(
        first,
        _candidate(
            registration_url=f"https://identity-fixture.invalid/{uuid4().hex[:10]}",
            title=f"Unrelated {uuid4().hex[:8]}",
            host=f"Unrelated Host {uuid4().hex[:8]}",
            start_at=datetime.now(UTC) + timedelta(days=30),
        ),
    )

    after_drop = await _entities_named(host)
    assert len(after_drop) == 1, after_drop
    _, source_keys, _ = after_drop[0]
    assert source_keys == {second}, "the departed source must not still be an asserter"

    # The answer must be the one a full rebuild produces from the same evidence.
    await _rebuild_all()
    assert await _entities_named(host) == after_drop


async def test_a_third_source_still_merges_after_the_first_one_goes_quiet(db: None) -> None:
    """The failure this guards is not staleness — it is the split coming back.

    With the naming source gone quiet, a later corroborating source computes its representative
    over the remaining members and would key its facts to a *different* entity, producing two rows
    for one name.
    """
    host = f"Rejoining Host {uuid4().hex[:8]}"
    start_at = datetime.now(UTC) + timedelta(days=21)
    url = f"https://identity-fixture.invalid/{uuid4().hex[:10]}"
    first = f"idfix-aaa-{uuid4().hex[:8]}"
    middle = f"idfix-mmm-{uuid4().hex[:8]}"
    third = f"idfix-zzz-{uuid4().hex[:8]}"
    shared = _candidate(registration_url=url, title=host, host=host, start_at=start_at)

    await _publish(first, shared)
    await _publish(middle, shared)
    await _publish(
        first,
        _candidate(
            registration_url=f"https://identity-fixture.invalid/{uuid4().hex[:10]}",
            title=f"Unrelated {uuid4().hex[:8]}",
            host=f"Unrelated Host {uuid4().hex[:8]}",
            start_at=datetime.now(UTC) + timedelta(days=30),
        ),
    )
    await _publish(third, shared)

    entities = await _entities_named(host)

    assert len(entities) == 1, entities
    assert entities[0][1] == {middle, third}

