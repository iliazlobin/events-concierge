"""Official endpoint shapes, minimal persistence and credential containment."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from events_concierge.adapters.entity_intelligence.social_api import (
    InstagramPublicProfileSource,
    SocialApiClient,
    SocialApiError,
    XPublicProfileSource,
)
from events_concierge.config import Settings
from events_concierge.workers.entity_intelligence import build_social_enrichment

TOKEN = "fixture-private-token"
X_URL = "https://x.com/public_builder"


def client(origin, handler):
    return SocialApiClient(origin, SecretStr(TOKEN), transport=httpx.MockTransport(handler))


def x_payload():
    return {
        "data": {
            "id": "123456",
            "username": "Public_Builder",
            "protected": False,
            "description": "Public AI\ncommunity",
            "profile_image_url": "https://pbs.twimg.com/profile_images/avatar.png",
            "public_metrics": {"followers_count": 6412, "following_count": 900},
            "email": "discard@example.test",
            "private_message": "discard",
        }
    }


async def test_x_exact_lookup_keeps_only_public_facts():
    def handler(request):
        assert (
            str(request.url.copy_with(query=None))
            == "https://api.x.com/2/users/by/username/public_builder"
        )
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert TOKEN not in str(request.url)
        assert (
            request.url.params["user.fields"]
            == "description,profile_image_url,public_metrics,protected"
        )
        return httpx.Response(200, json=x_payload())

    result = await XPublicProfileSource(client("https://api.x.com", handler)).collect(X_URL)
    assert result.external_id == "123456"
    assert result.provider_key == "x_public_api"
    assert {(f.fact_key, f.value) for f in result.facts} == {
        ("description", "Public AI community"),
        ("avatar", "Profile image"),
        ("followers", "6412"),
    }
    assert "discard" not in str(result)


async def test_instagram_only_public_business_discovery_fields():
    def handler(request):
        assert request.url.path == "/v26.0/1784001234"
        assert (
            request.url.params["fields"]
            == "business_discovery.username(public.creator){id,username,biography,followers_count}"
        )
        assert request.headers["authorization"] == "Bearer " + TOKEN
        return httpx.Response(
            200,
            json={
                "business_discovery": {
                    "id": "1784999999",
                    "username": "Public.Creator",
                    "biography": "Community host",
                    "followers_count": 4300,
                    "profile_picture_url": "https://cdninstagram.com/discard.png",
                    "media": ["discard"],
                }
            },
        )

    source = InstagramPublicProfileSource(
        client("https://graph.facebook.com", handler), account_id="1784001234"
    )
    result = await source.collect("https://www.instagram.com/public.creator")
    assert [(f.fact_key, f.value) for f in result.facts] == [
        ("description", "Community host"),
        ("followers", "4300"),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://x.com/search?q=builder",
        "https://evil.test/builder",
        "https://x.com/public_builder/status/1",
    ],
)
async def test_rejected_urls_make_no_request(url):
    def handler(request):
        pytest.fail("rejected profile must not send credentials")

    with pytest.raises(SocialApiError, match="social API request failed"):
        await XPublicProfileSource(client("https://api.x.com", handler)).collect(url)


@pytest.mark.parametrize(
    "protected,previous,code",
    [(True, None, "unsupported_profile"), (False, "999", "identity_changed")],
)
async def test_private_or_recycled_account_is_not_persisted(protected, previous, code):
    payload = x_payload()
    payload["data"]["protected"] = protected
    source = XPublicProfileSource(
        client("https://api.x.com", lambda request: httpx.Response(200, json=payload))
    )
    with pytest.raises(SocialApiError) as error:
        await source.collect(X_URL, previous)
    assert error.value.code == code


@pytest.mark.parametrize(
    "status,payload,code",
    [
        (302, None, "unavailable"),
        (401, None, "credentials_rejected"),
        (403, None, "credentials_rejected"),
        (404, None, "unsupported_profile"),
        (429, None, "rate_limited"),
        (400, {"error": {"code": 190, "message": TOKEN}}, "credentials_rejected"),
        (200, [], "invalid_response"),
        (200, {"errors": [{"message": TOKEN}]}, "invalid_response"),
    ],
)
async def test_failures_do_not_leak_response_or_follow_redirect(status, payload, code):
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(
            status,
            headers={"location": "https://evil.test/steal", "retry-after": "999999999999999999999"},
            content=json.dumps(payload).encode(),
        )

    with pytest.raises(SocialApiError) as error:
        await client("https://api.x.com", handler).get("/2/users/123", {})
    assert error.value.code == code
    assert TOKEN not in str(error.value)
    assert len(calls) == 1 and "evil.test" not in calls[0]
    assert 3600 <= error.value.retry_seconds <= 604800


async def test_body_cap_and_untrusted_avatar():
    with pytest.raises(SocialApiError) as error:
        await client(
            "https://api.x.com", lambda request: httpx.Response(200, content=b"x" * 64001)
        ).get("/2/users/123", {})
    assert error.value.code == "invalid_response"
    payload = x_payload()
    payload["data"]["profile_image_url"] = "https://pbs.twimg.com.evil.test/a"
    payload["data"]["public_metrics"]["followers_count"] = True
    result = await XPublicProfileSource(
        client("https://api.x.com", lambda request: httpx.Response(200, json=payload))
    ).collect(X_URL)
    assert [f.fact_key for f in result.facts] == ["description"]


def test_configuration_is_off_by_default_and_requires_scoped_credentials(tmp_path):
    settings = Settings(_env_file=None)
    assert not settings.x_profile_api_enabled and not settings.instagram_profile_api_enabled
    assert settings.social_profile_daily_limit == 100
    assert build_social_enrichment(settings)._sources == {}
    for fields in (
        {"x_profile_api_enabled": True},
        {"instagram_profile_api_enabled": True},
        {"social_profile_refresh_seconds": 3600},
    ):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **fields)
    tokenfile = tmp_path / "token"
    tokenfile.write_text(TOKEN)
    settings = Settings(
        _env_file=None, x_profile_api_enabled=True, x_profile_bearer_token_file=str(tokenfile)
    )
    assert settings.x_profile_bearer_token.get_secret_value() == TOKEN
    assert TOKEN not in str(settings.model_dump()) and TOKEN not in repr(settings)
