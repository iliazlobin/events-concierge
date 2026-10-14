"""New vendor profiles require explicit source review and stay within refresh lease limits."""

import os
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

from events_concierge.adapters.postgres.catalog_sources import PostgresCatalogSourceRepository
from events_concierge.application.catalog_refresh import catalog_refresh_lease_seconds
from events_concierge.deployment.catalog_source_seeds import remove_unused_seed
from events_concierge.domain.enums import CatalogSourceMode

pytestmark = pytest.mark.integration


async def test_vendor_profiles_are_prepared_without_policy_or_cadence_activation(db: None) -> None:
    repo = PostgresCatalogSourceRepository()
    for key in [
        "langchain-events",
        "luma-langchain",
        "luma-semianalysis",
        "luma-claude-startups",
        "luma-tavily",
        "luma-silicon-after-dark-2026",
    ]:
        source = await repo.get(key)
        assert source is not None
        assert source.enabled is False and source.reviewed_at is None
        assert source.handoff_only is True
        assert source.region == ("bayarea" if key == "luma-silicon-after-dark-2026" else "global")
        assert source.collection_horizon_days == 90
        assert catalog_refresh_lease_seconds(source, floor_seconds=60) is not None
        if key == "langchain-events":
            assert source.mode is CatalogSourceMode.LANGCHAIN_HTML
            assert source.page_limit == 1
            assert source.approved_origins == (
                "https://www.langchain.com",
                "https://events.langchain.com",
                "https://luma.com",
            )
        elif key == "luma-silicon-after-dark-2026":
            assert source.mode is CatalogSourceMode.LUMA_EVENT_HTML
            assert source.page_limit == 1
            assert source.approved_origins == ("https://luma.com",)
        else:
            assert source.mode is CatalogSourceMode.LUMA_CALENDAR_JSON
            assert source.page_limit == 30
            assert source.approved_origins == ("https://api.luma.com", "https://api2.luma.com")


def test_seed_cleanup_preserves_reviewed_or_activated_configuration(db: None) -> None:
    engine = create_engine(os.environ["EC_MIGRATION_URL"])
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(
                text("""
                UPDATE catalog_sources SET enabled=true WHERE source_key='langchain-events'
            """)
            )
            with (
                Operations.context(MigrationContext.configure(connection)),
                pytest.raises(RuntimeError, match="activated or modified"),
            ):
                remove_unused_seed("langchain-events", {"mode": "langchain_html"})
            assert (
                connection.execute(
                    text("""
                SELECT enabled FROM catalog_sources WHERE source_key='langchain-events'
            """)
                ).scalar_one()
                is True
            )
            transaction.rollback()
    finally:
        engine.dispose()


def test_seed_cleanup_refuses_collection_history_even_when_profile_is_still_pristine(
    db: None,
) -> None:
    engine = create_engine(os.environ["EC_MIGRATION_URL"])
    try:
        with engine.connect() as connection:
            transaction = connection.begin()
            connection.execute(
                text("""
                INSERT INTO catalog_refresh_runs(source_key,run_key,status,started_at,completed_at)
                VALUES ('langchain-events',:run,'failed',now(),now())
            """),
                {"run": "manual:retention-test-" + uuid4().hex},
            )
            with (
                Operations.context(MigrationContext.configure(connection)),
                pytest.raises(RuntimeError, match="retain vendor source history"),
            ):
                remove_unused_seed("langchain-events", {"mode": "langchain_html"})
            assert (
                connection.execute(
                    text("""
                SELECT count(*) FROM catalog_refresh_runs WHERE source_key='langchain-events'
            """)
                ).scalar_one()
                == 1
            )
            transaction.rollback()
    finally:
        engine.dispose()
