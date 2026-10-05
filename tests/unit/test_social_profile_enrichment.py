from uuid import uuid4

import pytest

from events_concierge.adapters.entity_intelligence.public_sources import CollectedPublicSource
from events_concierge.adapters.entity_intelligence.social_api import SocialApiError
from events_concierge.application.social_profile_enrichment import (
    SocialProfileClaim,
    SocialProfileEnrichmentService,
)


class Repository:
    def __init__(self, claims, accepted=True):
        self.claims = list(claims)
        self.accepted = accepted
        self.results = []
        self.reservations = []

    async def claim(self, providers, daily_limit):
        self.reservations.append((providers, daily_limit))
        return self.claims.pop(0) if self.claims else None

    async def finish(self, claim, collection, **values):
        self.results.append((claim, collection, values))
        return self.accepted


class Source:
    provider_key = "x_public_api"

    def __init__(self, error=None):
        self.error = error

    async def collect(self, url, previous_id=None):
        assert previous_id == "123"
        if self.error:
            raise self.error
        return CollectedPublicSource(self.provider_key, "123", url, "X API", ())


def claim():
    return SocialProfileClaim(uuid4(), "x_public_api", "https://x.com/builder", "123", uuid4())


async def test_disabled_service_never_claims():
    repository = Repository([claim()])
    assert await SocialProfileEnrichmentService(repository, ()).refresh_due(2) == []
    assert repository.reservations == []


async def test_bounded_batch_and_daily_cap_pass_to_durable_claim():
    claims = [claim(), claim(), claim()]
    repository = Repository(claims)
    assert await SocialProfileEnrichmentService(
        repository, (Source(),), daily_limit=25
    ).refresh_due(2) == [claims[0].entity_id, claims[1].entity_id]
    assert len(repository.claims) == 1
    assert repository.reservations == [(("x_public_api",), 25)] * 2


@pytest.mark.parametrize(
    "error", [SocialApiError("rate_limited", retry_seconds=172800), TimeoutError()]
)
async def test_failure_records_retry_without_empty_snapshot(error):
    repository = Repository([claim()])
    await SocialProfileEnrichmentService(repository, (Source(error),)).refresh_due(2)
    _, collection, values = repository.results[0]
    assert collection is None
    assert values["error_code"] in {"rate_limited", "unavailable"}
    assert values["refresh_seconds"] >= 86400


async def test_lost_lease_is_not_reported_as_published():
    repository = Repository([claim()], accepted=False)
    assert await SocialProfileEnrichmentService(repository, (Source(),)).refresh_due(1) == []
