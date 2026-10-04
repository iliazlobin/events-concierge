"""Official API reads for social URLs already attached by a catalog source.

No search, posts, contacts or raw payload storage. Authenticated requests use fixed origins
and never follow redirects; the general public-page crawler must not carry these credentials.
"""

from __future__ import annotations

import asyncio
import json
import re
from http import HTTPStatus
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr

from ...domain.catalog_entities import CatalogEntityExternalFactDraft
from ...domain.social_profiles import social_profile_from_url
from .public_sources import CollectedPublicSource, PublicSourceError

_MAX_BODY = 64_000
_MAX_TEXT = 1_000
_AUTH_ERRORS = {401, 403}
_MAX_ID_LENGTH = 32
_MAX_URL_LENGTH = 2_048
_MIN_PRINTABLE = 32
_MAX_RETRY_DIGITS = 10
_META_AUTH_CODES = frozenset({10, 190, 200})
_META_RATE_CODES = frozenset({4, 17, 32, 613})
_META_UNSUPPORTED = 100
_ORIGINS = frozenset({"https://api.x.com", "https://graph.facebook.com"})


class SocialApiError(PublicSourceError):
    def __init__(self, code: str, *, retry_seconds: int = 86_400) -> None:
        super().__init__(code, "social API request failed")
        self.retry_seconds = max(3_600, min(retry_seconds, 7 * 86_400))


class SocialApiClient:
    def __init__(
        self, origin: str, token: SecretStr, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        if origin not in _ORIGINS or not token.get_secret_value().strip():
            raise ValueError("social API requires a fixed origin and a token")
        self._origin = origin
        self._token = token
        self._transport = transport

    async def get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        if not re.fullmatch(r"/[A-Za-z0-9_./-]+", path) or ".." in path:
            raise ValueError("invalid social API path")
        try:
            async with (
                asyncio.timeout(20),
                httpx.AsyncClient(
                    transport=self._transport, timeout=15, follow_redirects=False, trust_env=False
                ) as client,
                client.stream(
                    "GET",
                    self._origin + path,
                    params=params,
                    headers={
                        "Authorization": f"Bearer {self._token.get_secret_value()}",
                        "Accept": "application/json",
                    },
                ) as response,
            ):
                if response.status_code == HTTPStatus.TOO_MANY_REQUESTS:
                    retry = response.headers.get("retry-after", "")
                    raise SocialApiError(
                        "rate_limited",
                        retry_seconds=int(retry)
                        if retry.isdigit() and len(retry) < _MAX_RETRY_DIGITS
                        else 86_400,
                    )
                if response.status_code in _AUTH_ERRORS:
                    raise SocialApiError("credentials_rejected")
                if response.status_code == HTTPStatus.NOT_FOUND:
                    raise SocialApiError("unsupported_profile")
                if response.status_code not in {HTTPStatus.OK, HTTPStatus.BAD_REQUEST}:
                    raise SocialApiError("unavailable")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_BODY:
                        raise SocialApiError("invalid_response")
                payload = json.loads(body)
        except (httpx.HTTPError, TimeoutError):
            raise SocialApiError("unavailable") from None
        except (ValueError, UnicodeError):
            raise SocialApiError("invalid_response") from None
        if not isinstance(payload, dict):
            raise SocialApiError("invalid_response")
        _check_payload_error(payload)
        if response.status_code != HTTPStatus.OK:
            raise SocialApiError("invalid_response")
        return payload


def _check_payload_error(payload: dict[str, Any]) -> None:
    if "error" not in payload and "errors" not in payload:
        return
    error = payload.get("error")
    raw_code = error.get("code") if isinstance(error, dict) else None
    code = raw_code if isinstance(raw_code, int) and not isinstance(raw_code, bool) else None
    if code in _META_AUTH_CODES:
        raise SocialApiError("credentials_rejected")
    if code in _META_RATE_CODES:
        raise SocialApiError("rate_limited")
    if code == _META_UNSUPPORTED:
        raise SocialApiError("unsupported_profile")
    raise SocialApiError("invalid_response")


def _profile_handle(url: str, key: str) -> str:
    profile = social_profile_from_url(url)
    if profile is None or profile.provider_key != key:
        raise SocialApiError("unsupported_profile")
    return urlsplit(profile.url).path.strip("/")


def _identity(data: dict[str, Any], handle: str, previous_id: str | None) -> str:
    account_id = data.get("id")
    username = data.get("username")
    if (
        not isinstance(account_id, str)
        or not account_id.isascii()
        or not account_id.isdigit()
        or not 1 <= len(account_id) <= _MAX_ID_LENGTH
        or not isinstance(username, str)
        or username.casefold() != handle.casefold()
    ):
        raise SocialApiError("invalid_response")
    if previous_id is not None and previous_id != account_id:
        raise SocialApiError("identity_changed", retry_seconds=7 * 86_400)
    return account_id


def _facts(
    data: dict[str, Any], *, description: str, avatar: str | None, followers: object, cdns: tuple[str, ...]
) -> tuple[CatalogEntityExternalFactDraft, ...]:
    facts: list[CatalogEntityExternalFactDraft] = []
    text = data.get(description)
    if isinstance(text, str):
        clean = " ".join(text.split())[:_MAX_TEXT]
        clean = "".join(char for char in clean if char.isprintable())
        if clean:
            facts.append(CatalogEntityExternalFactDraft("description", clean))
    image = data.get(avatar) if avatar is not None else None
    if isinstance(image, str) and len(image) <= _MAX_URL_LENGTH:
        try:
            parsed = urlsplit(image)
            safe = (
                parsed.scheme == "https"
                and parsed.username is None
                and parsed.password is None
                and parsed.port is None
                and not parsed.fragment
                and not any(char.isspace() or ord(char) < _MIN_PRINTABLE for char in image)
                and any(
                    parsed.hostname == cdn or (parsed.hostname or "").endswith("." + cdn)
                    for cdn in cdns
                )
            )
        except ValueError:
            safe = False
        if safe:
            facts.append(CatalogEntityExternalFactDraft("avatar", "Profile image", image))
    if isinstance(followers, int) and not isinstance(followers, bool) and 0 <= followers <= 10**12:
        facts.append(CatalogEntityExternalFactDraft("followers", str(followers)))
    return tuple(facts)


class XPublicProfileSource:
    provider_key = "x_public_api"

    def __init__(self, http: SocialApiClient) -> None:
        self._http = http

    async def collect(self, url: str, previous_id: str | None = None) -> CollectedPublicSource:
        handle = _profile_handle(url, "x_profile")
        payload = await self._http.get(
            f"/2/users/by/username/{handle}",
            {"user.fields": "description,profile_image_url,public_metrics,protected"},
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise SocialApiError("invalid_response")
        account_id = _identity(data, handle, previous_id)
        if data.get("protected") is not False:
            raise SocialApiError("unsupported_profile")
        metrics = data.get("public_metrics")
        followers = metrics.get("followers_count") if isinstance(metrics, dict) else None
        return CollectedPublicSource(
            self.provider_key,
            account_id,
            f"https://x.com/{handle}",
            "X API",
            _facts(
                data,
                description="description",
                avatar="profile_image_url",
                followers=followers,
                cdns=("pbs.twimg.com",),
            ),
        )


class InstagramPublicProfileSource:
    provider_key = "instagram_public_api"

    def __init__(self, http: SocialApiClient, *, account_id: str, version: str = "v26.0") -> None:
        if not re.fullmatch(r"[0-9]{1,32}", account_id) or not re.fullmatch(
            r"v[0-9]{1,2}\.0", version
        ):
            raise ValueError("Instagram requires a professional account ID and API version")
        self._http = http
        self._account_id = account_id
        self._version = version

    async def collect(self, url: str, previous_id: str | None = None) -> CollectedPublicSource:
        handle = _profile_handle(url, "instagram_profile")
        payload = await self._http.get(
            f"/{self._version}/{self._account_id}",
            {
                # Only fields marked Public in Meta's IG User reference. Its current picture
                # field describes the app user's own profile and is not a public discovery field.
                "fields": f"business_discovery.username({handle}){{id,username,biography,followers_count}}"
            },
        )
        data = payload.get("business_discovery")
        if not isinstance(data, dict):
            raise SocialApiError("unsupported_profile")
        account_id = _identity(data, handle, previous_id)
        return CollectedPublicSource(
            self.provider_key,
            account_id,
            f"https://www.instagram.com/{handle}",
            "Instagram API",
            _facts(
                data,
                description="biography",
                avatar=None,
                followers=data.get("followers_count"),
                cdns=(),
            ),
        )
