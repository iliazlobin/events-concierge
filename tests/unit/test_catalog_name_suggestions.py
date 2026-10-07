"""Public autocomplete filters, validation and repository bounds."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from events_concierge.adapters.postgres.catalog import PostgresCatalogRepository
from events_concierge.adapters.ranking.embedding import DeterministicEmbedding
from events_concierge.api import app as app_module
from events_concierge.config import Settings
from events_concierge.domain.catalog_browse import CatalogNameSuggestion


@pytest.fixture
def autocomplete_app(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(app_module, "get_settings", lambda: Settings(
        release_profile="discovery", mock_cloud=False, catalog_name_suggestions_enabled=True,
    ))
    app = app_module.create_app()
    suggest = AsyncMock(return_value=[CatalogNameSuggestion(
        name="Nebius", kinds=("host", "organization"), event_count=3,
    )])
    app.state.container = SimpleNamespace(catalog=SimpleNamespace(suggest_names=suggest))
    return app, suggest


@pytest.mark.parametrize("release_profile", ["discovery", "full"])
async def test_disabled_catalog_names_never_require_a_database(
    monkeypatch: pytest.MonkeyPatch, release_profile: str,
) -> None:
    settings = Settings(_env_file=None, release_profile=release_profile)
    assert settings.catalog_name_suggestions_enabled is False
    monkeypatch.setattr(app_module, "get_settings", lambda: settings)
    app = app_module.create_app()
    # No container is installed: a disabled route must reject before reaching storage.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/catalog/name-suggestions", params={"q": "nebius"})
        config = await client.get("/v1/ui-config")
    assert response.status_code == 404
    assert config.json()["catalog_name_suggestions_enabled"] is False
    assert "/v1/catalog/name-suggestions" not in app.openapi()["paths"]
    assert "/v1/catalog/events" in app.openapi()["paths"]


async def test_guest_autocomplete_preserves_all_catalog_filters(autocomplete_app) -> None:
    app, suggest = autocomplete_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/catalog/name-suggestions", params=[
            ("q", "  nebius  "), ("source_key", "tech-week-sf-2026"),
            ("city", "San Francisco"), ("topic", "ai"), ("availability", "available"),
            ("price", "paid"), ("price_min_cents", "100"), ("price_max_cents", "5000"),
            ("date_range", "2026-10-01..2026-10-31"), ("limit", "6"),
        ])
        config = await client.get("/v1/ui-config")
    assert response.status_code == 200
    assert config.json()["catalog_name_suggestions_enabled"] is True
    assert response.json() == [{"name": "Nebius", "kinds": ["host", "organization"],
                                "event_count": 3}]
    call = suggest.call_args.kwargs
    assert call["query"] == "nebius"
    assert call["source_keys"] == ("tech-week-sf-2026",)
    assert call["cities"] == ("San Francisco",)
    assert call["topics"] == ("ai",)
    assert call["availability"] == "available"
    assert (call["price_min_cents"], call["price_max_cents"]) == (100, 5000)
    assert len(call["date_ranges"]) == 1
    assert call["limit"] == 6
    assert set(app.openapi()["paths"]["/v1/catalog/name-suggestions"]) == {"get"}


@pytest.mark.parametrize("params", [
    {}, {"q": "n"}, {"q": "  "}, {"q": "n" * 161}, {"q": "ne\x01"},
    {"q": "ne", "limit": 21}, {"q": "ne", "limit": 0},
    {"q": "ne", "availability": "waitlist"},
    {"q": "ne", "source_key": "bad/source"},
    {"q": "ne", "date_range": "broken"},
    {"q": "ne", "starts_after": "2026-10-01T00:00:00Z"},
    {"q": "ne", "starts_after": "2026-10-01", "starts_before": "2026-10-31"},
    {"q": "ne", "price_min_cents": 100, "price_max_cents": 50},
])
async def test_bad_autocomplete_inputs_never_reach_database(autocomplete_app, params) -> None:
    app, suggest = autocomplete_app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/v1/catalog/name-suggestions", params=params)
    assert response.status_code == 422
    suggest.assert_not_called()


@pytest.mark.parametrize(("query", "limit"), [("n", 8), ("  ", 8), ("ne", 21), ("ne\x7f", 8)])
async def test_repository_rejects_unsafe_or_unbounded_search(query: str, limit: int) -> None:
    catalog = PostgresCatalogRepository(DeterministicEmbedding())
    with pytest.raises(ValueError):
        await catalog.suggest_names(query=query, limit=limit)
