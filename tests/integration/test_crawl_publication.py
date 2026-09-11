"""Reviewed JSON-LD failures retain the last publication in isolated PostgreSQL."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from tests.integration.test_catalog_sources import _seed_owner_source

from events_concierge.adapters.crawl.source import PublicJsonLdSource
from events_concierge.adapters.mock.discovery_policy import MockDiscoveryPolicyReader
from events_concierge.adapters.policy.discovery import StoreBackedDiscoveryPolicyGate
from events_concierge.adapters.policy.pacer import InMemoryPacer
from events_concierge.adapters.postgres.catalog import PostgresCatalogRepository
from events_concierge.adapters.postgres.catalog_observations import (
    PostgresCatalogObservationRepository,
)
from events_concierge.adapters.postgres.catalog_refresh_commit import (
    PostgresCatalogRefreshCommitter,
)
from events_concierge.adapters.postgres.catalog_sources import PostgresCatalogSourceRepository
from events_concierge.adapters.ranking.embedding import DeterministicEmbedding
from events_concierge.application.catalog_refresh import (
    CatalogRefreshOutcome,
    CatalogRefreshService,
)
from events_concierge.domain.catalog_sources import CatalogSource
from events_concierge.domain.enums import (
    CatalogRefreshRunStatus,
    CatalogSourceMode,
    Modality,
    Source,
)
from events_concierge.domain.policy import SourcePolicy

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("failure", ["timeout", "http_503", "http_404", "redirect", "parse"])
async def test_failed_collection_preserves_last_good_catalog_and_valid_empty_clears_it(
    db: None, failure: str
) -> None:
    source = CatalogSource(
        source_key=f"crawler-regression-{uuid4().hex}",
        display_name="Reviewed crawler regression",
        publisher="Reviewed crawler regression",
        seed_url="https://catalog.example.org/events",
        approved_origins=("https://catalog.example.org",),
        region="bay_area_9_county",
        mode=CatalogSourceMode.PUBLIC_JSONLD,
        enabled=True,
        reviewed_at=datetime.now(UTC) - timedelta(minutes=2),
        review_expires_at=None,
        refresh_interval_minutes=60,
        min_interval_ms=1,
    )
    await _seed_owner_source(source)
    event = {
        "@type": "Event",
        "name": f"Reviewed public event {uuid4().hex}",
        "startDate": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
        "url": f"https://catalog.example.org/event/{uuid4().hex}",
    }
    mode = "success"
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        assert request.url.host == "catalog.example.org"
        if mode == "timeout":
            raise httpx.ReadTimeout("unavailable", request=request)
        if mode.startswith("http_"):
            return httpx.Response(int(mode.removeprefix("http_")), request=request)
        if mode == "redirect":
            return httpx.Response(
                302, headers={"location": "https://unapproved.example.org/events"}, request=request
            )
        payload = json.dumps([event] if mode == "success" else [])
        body = f'<script type="application/ld+json">{payload}</script>'
        if mode == "parse":
            body += '<script type="application/ld+json">{broken</script>'
        return httpx.Response(200, text=body, request=request)

    repository = PostgresCatalogSourceRepository()
    catalog = PostgresCatalogRepository(DeterministicEmbedding())
    observations = PostgresCatalogObservationRepository()
    policy = StoreBackedDiscoveryPolicyGate(
        MockDiscoveryPolicyReader(
            {
                Source.PUBLIC_JSONLD: SourcePolicy(
                    source=Source.PUBLIC_JSONLD, automation_allowed={Modality.BROWSER: True}
                )
            }
        )
    )
    service = CatalogRefreshService(
        repository,
        PostgresCatalogRefreshCommitter(catalog, observations),
        {
            CatalogSourceMode.PUBLIC_JSONLD: PublicJsonLdSource(
                user_agent="isolated-test", transport=httpx.MockTransport(handler)
            )
        },
        InMemoryPacer(burst=100),
        policy,
    )
    key = source.source_key
    assert (await service.refresh(key, "seed")).outcome == CatalogRefreshOutcome.SUCCEEDED
    published, _ = await catalog.browse_current(source_keys=(key,), after=None, limit=10)
    assert len(published) == 1

    mode = failure
    if failure in {"timeout", "http_503"}:
        assert (
            await service.refresh(key, "failed-refresh")
        ).outcome == CatalogRefreshOutcome.DEFERRED
    else:
        with pytest.raises(ValueError):
            await service.refresh(key, "failed-refresh")
    run = await repository.get_refresh_run(key, "failed-refresh")
    assert run is not None and run.status == CatalogRefreshRunStatus.FAILED
    recorded = await observations.list_for_source(key)
    assert len(recorded) == 1 and recorded[0].last_run_key == "seed"
    retained, _ = await catalog.browse_current(source_keys=(key,), after=None, limit=10)
    assert retained == published
    assert len(requested) == 2

    mode = "empty"
    result = await service.refresh(key, "empty-refresh")
    assert result.outcome == CatalogRefreshOutcome.SUCCEEDED and result.candidate_count == 0
    current, _ = await catalog.browse_current(source_keys=(key,), after=None, limit=10)
    assert current == []
    # Successful empty collection advances visibility without deleting historical observations.
    assert len(await observations.list_for_source(key)) == 1
