"""Real PostgreSQL fencing, failure preservation and shared budget contracts."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from events_concierge.adapters.entity_intelligence.public_sources import CollectedPublicSource
from events_concierge.adapters.postgres.catalog_entities import PostgresCatalogEntityRepository
from events_concierge.adapters.postgres.social_profile_refresh import (
    PostgresSocialProfileRefreshRepository,
)
from events_concierge.application.social_profile_enrichment import SocialProfileClaim
from events_concierge.domain.catalog_entities import (
    CatalogEntityExternalFactDraft,
    CatalogEntityExternalSourceSnapshot,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def social_fixture(db):
    owner = create_async_engine(os.environ["EC_MIGRATION_URL"])
    entity = uuid4()
    url = "https://www.linkedin.com/in/social-fixture-" + entity.hex
    social_url = "https://x.com/social_builder"
    async with owner.begin() as conn:
        await conn.execute(
            text("""
            INSERT INTO public.catalog_entities(entity_id,identity_key,identity_status,kind,display_name,
                normalized_name,canonical_profile_url,profile_key,first_seen_at,last_seen_at)
            VALUES(:id,'profile:'||md5(public.fn_normalize_profile_url_v1(:url)),
                'profile_verified','person','Social fixture','social fixture',
                :url,public.fn_normalize_profile_url_v1(:url),clock_timestamp(),clock_timestamp())
        """),
            {"id": entity, "url": url},
        )
        await conn.execute(
            text("""
            INSERT INTO public.catalog_social_profile_refreshes(entity_id,provider_key,source_url,next_refresh_at)
            VALUES(:id,'x_public_api',:url,'2000-01-01')
        """),
            {"id": entity, "url": social_url},
        )
    repo = PostgresCatalogEntityRepository()

    async def import_link():
        await repo.replace_external_source(
            CatalogEntityExternalSourceSnapshot(
                entity,
                "x_profile",
                "x:social_builder",
                social_url,
                "X",
                "linked",
                None,
                datetime.now(UTC) + timedelta(days=30),
                None,
            )
        )

    await import_link()

    async def due():
        async with owner.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE public.catalog_social_profile_refreshes SET next_refresh_at='2000-01-01' WHERE entity_id=:id"
                ),
                {"id": entity},
            )

    yield entity, owner, import_link, due
    async with owner.begin() as conn:
        await conn.execute(
            text("DELETE FROM public.catalog_entities WHERE entity_id=:id"), {"id": entity}
        )
    await owner.dispose()


async def test_snapshot_survives_ingestion_and_provider_failure(social_fixture):
    entity, _owner, import_link, due = social_fixture
    refresh = PostgresSocialProfileRefreshRepository()
    claim = await refresh.claim(("x_public_api",), 10000)
    assert claim is not None and claim.entity_id == entity and claim.previous_id is None
    collection = CollectedPublicSource(
        "x_public_api",
        "12345",
        claim.source_url,
        "X API",
        (
            CatalogEntityExternalFactDraft("description", "Public bio"),
            CatalogEntityExternalFactDraft("followers", "6412"),
            CatalogEntityExternalFactDraft(
                "avatar", "Profile image", "https://pbs.twimg.com/a.png"
            ),
        ),
    )
    assert await refresh.finish(claim, collection, error_code=None, refresh_seconds=86400)
    await import_link()
    detail = await PostgresCatalogEntityRepository().get(entity)
    source = next(s for s in detail.external_sources if s.provider_key == "x_public_api")
    saved_time = source.fetched_at
    assert source.status == "fresh"
    assert len(detail.external_facts) == 3
    await due()
    claim = await refresh.claim(("x_public_api",), 10000)
    assert claim.previous_id == "12345"
    assert await refresh.finish(
        claim, None, error_code="credentials_rejected", refresh_seconds=86400
    )
    detail = await PostgresCatalogEntityRepository().get(entity)
    source = next(s for s in detail.external_sources if s.provider_key == "x_public_api")
    assert source.status == "failed" and source.fetched_at == saved_time
    assert len(detail.external_facts) == 3
    assert all(f.observed_at == saved_time for f in detail.external_facts)


async def test_lease_fence_budget_and_changed_link(social_fixture):
    entity, owner, _, _due = social_fixture
    refresh = PostgresSocialProfileRefreshRepository()
    async with owner.connect() as conn:
        used = (
            await conn.execute(
                text(
                    "SELECT coalesce(sum(reserved_calls),0) FROM public.catalog_social_profile_daily_budget WHERE provider_key='x_public_api' AND budget_date=(clock_timestamp() AT TIME ZONE 'UTC')::date"
                )
            )
        ).scalar_one()
    cap = int(used) + 1
    claim = await refresh.claim(("x_public_api",), cap)
    assert claim and claim.entity_id == entity
    assert await refresh.claim(("x_public_api",), cap) is None
    fake = SocialProfileClaim(entity, "x_public_api", claim.source_url, None, uuid4())
    assert not await refresh.finish(fake, None, error_code="unavailable", refresh_seconds=86400)
    async with owner.begin() as conn:
        await conn.execute(
            text(
                "UPDATE public.catalog_social_profile_refreshes SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE entity_id=:id"
            ),
            {"id": entity},
        )
    assert not await refresh.finish(claim, None, error_code="unavailable", refresh_seconds=86400)
    replacement = await refresh.claim(("x_public_api",), cap + 1)
    assert replacement and replacement.lease_token != claim.lease_token
    async with owner.begin() as conn:
        await conn.execute(
            text(
                "UPDATE public.catalog_entity_external_sources SET source_url='https://x.com/changed_builder' WHERE entity_id=:id AND provider_key='x_profile'"
            ),
            {"id": entity},
        )
    assert not await refresh.finish(
        replacement,
        CollectedPublicSource("x_public_api", "12345", replacement.source_url, "X API", ()),
        error_code=None,
        refresh_seconds=86400,
    )
    detail = await PostgresCatalogEntityRepository().get(entity)
    assert not any(s.provider_key == "x_public_api" for s in detail.external_sources)


async def test_app_cannot_bypass_fenced_writer(social_fixture):
    entity, _, _, _ = social_fixture
    with pytest.raises(Exception, match="invalid"):
        await PostgresCatalogEntityRepository().replace_external_source(
            CatalogEntityExternalSourceSnapshot(
                entity,
                "x_public_api",
                "12345",
                "https://x.com/social_builder",
                "X API",
                "fresh",
                datetime.now(UTC),
                datetime.now(UTC) + timedelta(days=1),
                None,
            )
        )
