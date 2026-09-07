"""Public entity-source parsing and network-policy coverage."""

from __future__ import annotations

import json

import httpx
import pytest

from events_concierge.adapters.entity_intelligence.public_sources import (
    GitHubOrganizationSource,
    OfficialWebsiteSource,
    PublicSourceError,
    SafePublicHttpClient,
    linked_profile_source,
)


async def _public_resolver(_host: str, _port: int) -> tuple[str, ...]:
    return ("93.184.216.34",)


async def test_official_website_extracts_structured_facts_and_known_profiles() -> None:
    html = """
    <html><head>
      <title>Example Labs</title>
      <link rel="canonical" href="https://example.com/">
      <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "Example Labs",
        "description": "Tools and gatherings for practical AI builders.",
        "foundingDate": "2021",
        "address": {"addressLocality": "San Francisco", "addressRegion": "CA", "addressCountry": "US"},
        "knowsAbout": ["AI infrastructure", "Developer tools"],
        "industry": "Software",
        "sameAs": ["https://github.com/example-labs", "https://www.linkedin.com/company/example-labs"]
      }
      </script>
    </head><body></body></html>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, text=html, request=request)

    http = SafePublicHttpClient(
        user_agent="events-concierge-test/1.0",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )
    result = await OfficialWebsiteSource(http).collect("https://example.com/")
    facts = {(fact.fact_key, fact.value) for fact in result.facts}

    assert result.provider_key == "official_website"
    assert result.display_name == "Example Labs"
    assert ("description", "Tools and gatherings for practical AI builders.") in facts
    assert ("founded", "2021") in facts
    assert ("location", "San Francisco, CA, US") in facts
    assert ("focus", "AI infrastructure") in facts
    assert ("industry", "Software") in facts
    assert {profile.provider_key for profile in result.discovered_profiles} == {
        "github_public",
        "linkedin_profile",
    }


async def test_github_source_keeps_only_bounded_public_organization_fields() -> None:
    payload = {
        "id": 42,
        "type": "Organization",
        "login": "example-labs",
        "name": "Example Labs",
        "html_url": "https://github.com/example-labs",
        "description": "Open tools for event builders.",
        "blog": "https://example.com",
        "location": "San Francisco",
        "public_repos": 12,
        "followers": 345,
        "created_at": "2021-04-05T00:00:00Z",
        "private_repos": 999,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("https://api.github.com/orgs/example-labs")
        assert request.headers["x-github-api-version"] == "2022-11-28"
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(payload).encode(),
            request=request,
        )

    http = SafePublicHttpClient(
        user_agent="events-concierge-test/1.0",
        transport=httpx.MockTransport(handler),
        resolver=_public_resolver,
    )
    result = await GitHubOrganizationSource(http).collect("https://github.com/example-labs")
    facts = {(fact.fact_key, fact.value) for fact in result.facts}

    assert ("public_repositories", "12") in facts
    assert ("followers", "345") in facts
    assert ("founded", "2021-04-05") in facts
    assert all(fact.value != "999" for fact in result.facts)
    assert result.discovered_profiles[0].provider_key == "official_website"
    assert result.discovered_profiles[0].url == "https://example.com"


async def test_public_source_client_rejects_non_public_addresses_before_fetch() -> None:
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, text="unexpected", request=request)

    async def private_resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("127.0.0.1",)

    http = SafePublicHttpClient(
        user_agent="events-concierge-test/1.0",
        transport=httpx.MockTransport(handler),
        resolver=private_resolver,
    )
    with pytest.raises(PublicSourceError, match="not public") as raised:
        await http.get("https://metadata.example/", accept="text/html")

    assert raised.value.code == "network_policy"
    assert called is False


def test_linkedin_profiles_are_connected_without_scraping() -> None:
    result = linked_profile_source("https://www.linkedin.com/in/aaron-example")

    assert result.provider_key == "linkedin_profile"
    assert result.facts[0].fact_key == "profile"
    assert result.facts[0].value == "LinkedIn"
