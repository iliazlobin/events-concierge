"""New vendor profiles require explicit source review and stay within refresh lease limits."""

import pytest

from events_concierge.adapters.postgres.catalog_sources import PostgresCatalogSourceRepository
from events_concierge.application.catalog_refresh import catalog_refresh_lease_seconds
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
