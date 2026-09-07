"""Bounded public-source readers for exact event-entity profile URLs.

These adapters never search by display name.  They start from an exact profile URL already
attached to a source event, retain only a small typed fact vocabulary, and discard raw payloads.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import socket
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlsplit, urlunsplit

import httpx
from selectolax.parser import HTMLParser

from ...domain.catalog_entities import CatalogEntityExternalFactDraft, CatalogEntityKind
from ...domain.social_profiles import SOCIAL_PROVIDER_KEYS, social_profile_from_url

_MAX_BODY_BYTES = 1_000_000
_MAX_REDIRECTS = 3
_HTTP_OK_MIN = 200
_HTTP_REDIRECT_MIN = 300
_HTTP_RATE_LIMITED = 429
_MAX_GITHUB_SLUG_LENGTH = 100
_WIKIDATA_PATH_PARTS = 2
_MAX_DISCOVERED_PROFILES = 20
_DATE_PREFIX_LENGTH = 10
_MAX_FACTS = 100
_MAX_REL_ME_LINKS = 50
_MAX_ANCHOR_LINKS = 500
_RESERVED_GITHUB_PATHS = frozenset(
    {
        "about",
        "apps",
        "collections",
        "contact",
        "customer-stories",
        "enterprise",
        "events",
        "features",
        "issues",
        "login",
        "marketplace",
        "new",
        "orgs",
        "pricing",
        "search",
        "security",
        "settings",
        "site",
        "sponsors",
        "topics",
    }
)
_LINKEDIN_HOSTS = frozenset({"linkedin.com", "www.linkedin.com"})
_GITHUB_HOSTS = frozenset({"github.com", "www.github.com"})
_WIKIDATA_HOSTS = frozenset({"wikidata.org", "www.wikidata.org"})


class PublicSourceError(RuntimeError):
    """A bounded, display-safe source failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class DiscoveredProfile:
    provider_key: str
    url: str
    external_id: str
    display_name: str


@dataclass(frozen=True, slots=True)
class CollectedPublicSource:
    provider_key: str
    external_id: str
    source_url: str
    display_name: str
    facts: tuple[CatalogEntityExternalFactDraft, ...]
    discovered_profiles: tuple[DiscoveredProfile, ...] = ()


@dataclass(frozen=True, slots=True)
class _PublicResponse:
    url: str
    content_type: str
    body: bytes


HostResolver = Callable[[str, int], Awaitable[tuple[str, ...]]]


class SafePublicHttpClient:
    """Small HTTPS client with redirect, DNS, size, and special-address fences."""

    def __init__(
        self,
        *,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: HostResolver | None = None,
        timeout_seconds: float = 8.0,
    ) -> None:
        self._user_agent = user_agent
        self._transport = transport
        self._resolver = resolver or _resolve_public_addresses
        self._timeout = timeout_seconds

    async def get(
        self,
        url: str,
        *,
        accept: str,
        headers: Mapping[str, str] | None = None,
    ) -> _PublicResponse:
        current = _https_url(url)
        async with httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
            follow_redirects=False,
        ) as client:
            for redirect in range(_MAX_REDIRECTS + 1):
                await self._validate_public_url(current)
                try:
                    async with client.stream(
                        "GET",
                        current,
                        headers={
                            "Accept": accept,
                            "User-Agent": self._user_agent,
                            **dict(headers or {}),
                        },
                    ) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            if redirect == _MAX_REDIRECTS:
                                raise PublicSourceError("invalid_response", "too many redirects")
                            location = response.headers.get("location")
                            if not location:
                                raise PublicSourceError(
                                    "invalid_response", "source redirect had no location"
                                )
                            current = _https_url(urljoin(current, location))
                            continue
                        if response.status_code == _HTTP_RATE_LIMITED:
                            raise PublicSourceError("rate_limited", "public source rate limited")
                        if (
                            response.status_code < _HTTP_OK_MIN
                            or response.status_code >= _HTTP_REDIRECT_MIN
                        ):
                            raise PublicSourceError(
                                "unavailable", f"public source returned {response.status_code}"
                            )
                        content_length = response.headers.get("content-length")
                        if (
                            content_length
                            and content_length.isdigit()
                            and int(content_length) > _MAX_BODY_BYTES
                        ):
                            raise PublicSourceError("invalid_response", "source body is too large")
                        chunks: list[bytes] = []
                        size = 0
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > _MAX_BODY_BYTES:
                                raise PublicSourceError("invalid_response", "source body is too large")
                            chunks.append(chunk)
                        return _PublicResponse(
                            url=_https_url(str(response.url)),
                            content_type=response.headers.get("content-type", ""),
                            body=b"".join(chunks),
                        )
                except PublicSourceError:
                    raise
                except (httpx.HTTPError, TimeoutError) as error:
                    raise PublicSourceError("unavailable", "public source is unavailable") from error
        raise PublicSourceError("invalid_response", "public source redirect failed")

    async def _validate_public_url(self, url: str) -> None:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if hostname is None or parsed.username is not None or parsed.password is not None:
            raise PublicSourceError("network_policy", "source URL is not public")
        if parsed.port not in {None, 443}:
            raise PublicSourceError("network_policy", "source port is not allowed")
        normalized = hostname.rstrip(".").casefold()
        if (
            normalized == "localhost"
            or normalized.endswith(".localhost")
            or normalized.endswith(".local")
            or "." not in normalized
        ):
            raise PublicSourceError("network_policy", "source host is not public")
        addresses = await self._resolver(normalized, 443)
        if not addresses:
            raise PublicSourceError("unavailable", "source host did not resolve")
        for value in addresses:
            try:
                address = ipaddress.ip_address(value)
            except ValueError as error:
                raise PublicSourceError("network_policy", "source address is invalid") from error
            mapped = getattr(address, "ipv4_mapped", None)
            if mapped is not None:
                address = mapped
            if not address.is_global:
                raise PublicSourceError("network_policy", "source address is not public")


async def _resolve_public_addresses(host: str, port: int) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    try:
        rows = await loop.run_in_executor(
            None,
            lambda: socket.getaddrinfo(host, port, type=socket.SOCK_STREAM),
        )
    except socket.gaierror as error:
        raise PublicSourceError("unavailable", "source host did not resolve") from error
    return tuple(sorted({str(row[4][0]) for row in rows}))


class OfficialWebsiteSource:
    provider_key = "official_website"

    def __init__(self, http: SafePublicHttpClient) -> None:
        self._http = http

    def supports(self, url: str, kind: CatalogEntityKind) -> bool:
        host = _host(url)
        return (
            kind == "organization"
            and host not in _LINKEDIN_HOSTS | _GITHUB_HOSTS | _WIKIDATA_HOSTS
        )

    async def collect(self, url: str) -> CollectedPublicSource:
        response = await self._http.get(
            url,
            accept="text/html,application/xhtml+xml;q=0.9",
        )
        if "html" not in response.content_type.casefold() and not response.body.lstrip().startswith(
            b"<"
        ):
            raise PublicSourceError("invalid_response", "official source did not return HTML")
        try:
            html = response.body.decode("utf-8", errors="replace")
            tree = HTMLParser(html)
        except (TypeError, ValueError) as error:
            raise PublicSourceError("invalid_response", "official source HTML is invalid") from error

        objects = _jsonld_objects(tree)
        organization = next((item for item in objects if _organization_object(item)), None)
        name = _first_text(
            organization.get("name") if organization else None,
            _meta(tree, "property", "og:site_name"),
            _title(tree),
            _host(response.url),
            limit=160,
        )
        description = _first_text(
            organization.get("description") if organization else None,
            _meta(tree, "property", "og:description"),
            _meta(tree, "name", "description"),
            limit=1000,
        )
        canonical = _canonical_link(tree, response.url) or response.url
        facts: list[CatalogEntityExternalFactDraft] = [
            CatalogEntityExternalFactDraft("website", name, canonical, 0)
        ]
        if description:
            facts.append(CatalogEntityExternalFactDraft("description", description, None, 0))

        if organization:
            entity_type = _schema_type(organization)
            if entity_type:
                facts.append(CatalogEntityExternalFactDraft("entity_type", entity_type, None, 0))
            founded = _first_text(organization.get("foundingDate"), limit=80)
            if founded:
                facts.append(CatalogEntityExternalFactDraft("founded", founded, None, 0))
            location, country = _address(organization.get("address"))
            if location:
                facts.append(CatalogEntityExternalFactDraft("location", location, None, 0))
            if country:
                facts.append(CatalogEntityExternalFactDraft("country", country, None, 0))
            for index, focus in enumerate(
                _text_values(organization.get("knowsAbout"), limit=160)
                + _keyword_values(organization.get("keywords"))
            ):
                facts.append(CatalogEntityExternalFactDraft("focus", focus, None, index))
            for index, industry in enumerate(
                _text_values(organization.get("industry"), limit=160)
            ):
                facts.append(CatalogEntityExternalFactDraft("industry", industry, None, index))

        profiles = _profile_links(tree, organization, response.url)
        for index, profile in enumerate(profiles):
            facts.append(
                CatalogEntityExternalFactDraft(
                    "profile", profile.display_name, profile.url, index
                )
            )
        return CollectedPublicSource(
            provider_key=self.provider_key,
            external_id=_host(canonical),
            source_url=canonical,
            display_name=name,
            facts=_dedupe_facts(facts),
            discovered_profiles=profiles,
        )


class GitHubOrganizationSource:
    provider_key = "github_public"

    def __init__(self, http: SafePublicHttpClient) -> None:
        self._http = http

    def supports(self, url: str, kind: CatalogEntityKind) -> bool:
        return kind == "organization" and _github_slug(url) is not None

    async def collect(self, url: str) -> CollectedPublicSource:
        slug = _github_slug(url)
        if slug is None:
            raise PublicSourceError("unsupported_profile", "GitHub profile is not an organization")
        response = await self._http.get(
            f"https://api.github.com/orgs/{quote(slug, safe='')}",
            accept="application/vnd.github+json",
            headers={"X-GitHub-Api-Version": "2022-11-28"},
        )
        payload = _json_object(response.body)
        if str(payload.get("type", "")).casefold() != "organization":
            raise PublicSourceError("unsupported_profile", "GitHub profile is not an organization")
        source_url = _optional_https(payload.get("html_url")) or f"https://github.com/{slug}"
        name = _first_text(payload.get("name"), payload.get("login"), slug, limit=160)
        facts: list[CatalogEntityExternalFactDraft] = [
            CatalogEntityExternalFactDraft("profile", "GitHub", source_url, 0),
            CatalogEntityExternalFactDraft("entity_type", "Organization", None, 0),
        ]
        description = _first_text(payload.get("description"), limit=1000)
        if description:
            facts.append(CatalogEntityExternalFactDraft("description", description, None, 0))
        website = _optional_https(payload.get("blog"))
        profiles: list[DiscoveredProfile] = []
        if website:
            facts.append(CatalogEntityExternalFactDraft("website", name, website, 0))
            official = _official_website_profile(website)
            if official:
                profiles.append(official)
        location = _first_text(payload.get("location"), limit=200)
        if location:
            facts.append(CatalogEntityExternalFactDraft("location", location, None, 0))
        created = _first_text(payload.get("created_at"), limit=80)
        if created:
            facts.append(CatalogEntityExternalFactDraft("founded", created[:10], None, 0))
        public_repositories = payload.get("public_repos")
        if isinstance(public_repositories, int) and public_repositories >= 0:
            facts.append(
                CatalogEntityExternalFactDraft(
                    "public_repositories", f"{public_repositories:,}", None, 0
                )
            )
        followers = payload.get("followers")
        if isinstance(followers, int) and followers >= 0:
            facts.append(CatalogEntityExternalFactDraft("followers", f"{followers:,}", None, 0))
        return CollectedPublicSource(
            provider_key=self.provider_key,
            external_id=f"github:{payload.get('id', slug)}",
            source_url=source_url,
            display_name="GitHub",
            facts=_dedupe_facts(facts),
            discovered_profiles=tuple(profiles),
        )


class WikidataSource:
    provider_key = "wikidata_public"

    def __init__(self, http: SafePublicHttpClient) -> None:
        self._http = http

    def supports(self, url: str, kind: CatalogEntityKind) -> bool:
        del kind
        return _wikidata_id(url) is not None

    async def collect(self, url: str) -> CollectedPublicSource:
        item_id = _wikidata_id(url)
        if item_id is None:
            raise PublicSourceError("unsupported_profile", "Wikidata item is invalid")
        response = await self._http.get(
            f"https://www.wikidata.org/wiki/Special:EntityData/{item_id}.json",
            accept="application/json",
        )
        payload = _json_object(response.body)
        entities = payload.get("entities")
        item = entities.get(item_id) if isinstance(entities, dict) else None
        if not isinstance(item, dict):
            raise PublicSourceError("invalid_response", "Wikidata item is missing")
        description = _localized_value(item.get("descriptions"), "en")
        label = _localized_value(item.get("labels"), "en") or item_id
        raw_claims = item.get("claims")
        claims: dict[str, Any] = raw_claims if isinstance(raw_claims, dict) else {}
        source_url = f"https://www.wikidata.org/wiki/{item_id}"
        facts: list[CatalogEntityExternalFactDraft] = [
            CatalogEntityExternalFactDraft("profile", "Wikidata", source_url, 0)
        ]
        if description:
            facts.append(CatalogEntityExternalFactDraft("description", description[:1000], None, 0))
        founded = _wikidata_time(claims.get("P571"))
        if founded:
            facts.append(CatalogEntityExternalFactDraft("founded", founded, None, 0))
        profiles: list[DiscoveredProfile] = []
        for index, website in enumerate(_wikidata_strings(claims.get("P856"))):
            normalized = _optional_https(website)
            if normalized:
                facts.append(CatalogEntityExternalFactDraft("website", label, normalized, index))
                official = _official_website_profile(normalized)
                if official:
                    profiles.append(official)
        for username in _wikidata_strings(claims.get("P2037")):
            profile = _discovered_profile(f"https://github.com/{username}")
            if profile:
                profiles.append(profile)
        for index, profile in enumerate(profiles, start=10):
            facts.append(
                CatalogEntityExternalFactDraft("profile", profile.display_name, profile.url, index)
            )
        return CollectedPublicSource(
            provider_key=self.provider_key,
            external_id=item_id,
            source_url=source_url,
            display_name="Wikidata",
            facts=_dedupe_facts(facts),
            discovered_profiles=tuple(profiles),
        )


def linked_profile_source(url: str) -> CollectedPublicSource:
    profile = _discovered_profile(url)
    if profile is None or profile.provider_key != "linkedin_profile":
        raise PublicSourceError("unsupported_profile", "profile source is unsupported")
    return CollectedPublicSource(
        provider_key=profile.provider_key,
        external_id=profile.external_id,
        source_url=profile.url,
        display_name=profile.display_name,
        facts=(CatalogEntityExternalFactDraft("profile", "LinkedIn", profile.url, 0),),
    )


def _https_url(value: str) -> str:
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError as error:
        raise PublicSourceError("network_policy", "source URL is invalid") from error
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise PublicSourceError("network_policy", "source URL must be direct HTTPS")
    return urlunsplit(parsed)


def _optional_https(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if not candidate.startswith(("https://", "http://")):
        candidate = f"https://{candidate}"
    if candidate.startswith("http://"):
        candidate = f"https://{candidate[7:]}"
    try:
        return _https_url(candidate)
    except PublicSourceError:
        return None


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").rstrip(".").casefold()


def _github_slug(url: str) -> str | None:
    if _host(url) not in _GITHUB_HOSTS:
        return None
    parts = [part for part in urlsplit(url).path.split("/") if part]
    if len(parts) != 1 or parts[0].casefold() in _RESERVED_GITHUB_PATHS:
        return None
    slug = parts[0]
    if not slug.replace("-", "").isalnum() or len(slug) > _MAX_GITHUB_SLUG_LENGTH:
        return None
    return slug


def _wikidata_id(url: str) -> str | None:
    if _host(url) not in _WIKIDATA_HOSTS:
        return None
    parts = [part for part in urlsplit(url).path.split("/") if part]
    if len(parts) != _WIKIDATA_PATH_PARTS or parts[0] != "wiki":
        return None
    item_id = parts[1].upper()
    return item_id if item_id.startswith("Q") and item_id[1:].isdigit() else None


def _discovered_profile(url: str) -> DiscoveredProfile | None:
    normalized = _optional_https(url)
    if normalized is None:
        return None
    host = _host(normalized)
    path = urlsplit(normalized).path.rstrip("/")
    if host in _LINKEDIN_HOSTS and (
        path.startswith("/in/") or path.startswith("/company/")
    ):
        return DiscoveredProfile(
            "linkedin_profile", normalized, f"linkedin:{path}", "LinkedIn"
        )
    slug = _github_slug(normalized)
    if slug:
        return DiscoveredProfile("github_public", normalized, f"github:{slug}", "GitHub")
    item_id = _wikidata_id(normalized)
    if item_id:
        return DiscoveredProfile(
            "wikidata_public", normalized, item_id, "Wikidata"
        )
    # A social profile the entity's own page links.  It enters the enrichment plane only; FR-19.13
    # keeps the event renderer's verified metadata to LinkedIn and explicit websites.  Nothing here
    # fetches the platform -- the URL is recorded as a link and never opened.
    social = social_profile_from_url(normalized)
    if social is not None:
        return DiscoveredProfile(
            social.provider_key, social.url, social.external_id, social.display_name
        )
    return None


def social_profile_source(url: str) -> CollectedPublicSource:
    """Record one social profile as a link, without ever fetching the platform.

    The mirror of ``linked_profile_source`` for X, Instagram, TikTok and YouTube.  Rule (e) forbids
    scraping LinkedIn; the same restraint applies to every social platform, and it applies to
    "verification" fetches too -- a profile URL is never opened to confirm it exists.  The only page
    this adapter's caller reads is the entity's own website.
    """
    profile = _discovered_profile(url)
    if profile is None or profile.provider_key not in SOCIAL_PROVIDER_KEYS:
        raise PublicSourceError("unsupported_profile", "profile source is unsupported")
    return CollectedPublicSource(
        provider_key=profile.provider_key,
        external_id=profile.external_id,
        source_url=profile.url,
        display_name=profile.display_name,
        facts=(
            CatalogEntityExternalFactDraft("profile", profile.display_name, profile.url, 0),
        ),
    )


def _official_website_profile(url: str) -> DiscoveredProfile | None:
    normalized = _optional_https(url)
    if normalized is None:
        return None
    host = _host(normalized)
    if not host or host in _LINKEDIN_HOSTS | _GITHUB_HOSTS | _WIKIDATA_HOSTS:
        return None
    return DiscoveredProfile(
        "official_website",
        normalized,
        f"website:{host}",
        "Official website",
    )


def _jsonld_objects(tree: HTMLParser) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text(deep=True, strip=False)
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            continue
        stack: list[Any] = [payload]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                output.append(item)
                graph = item.get("@graph")
                if isinstance(graph, (dict, list)):
                    stack.append(graph)
    return output[:100]


def _organization_object(value: dict[str, Any]) -> bool:
    raw = value.get("@type")
    types = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return any(
        isinstance(item, str)
        and any(
            suffix in item
            for suffix in (
                "Organization",
                "Corporation",
                "EducationalOrganization",
                "GovernmentOrganization",
                "NGO",
                "PerformingGroup",
            )
        )
        for item in types
    )


def _schema_type(value: dict[str, Any]) -> str | None:
    raw = value.get("@type")
    candidates = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    for item in candidates:
        if isinstance(item, str):
            label = item.rsplit("/", 1)[-1].rsplit("#", 1)[-1]
            if label:
                return label[:160]
    return None


def _meta(tree: HTMLParser, attribute: str, value: str) -> str | None:
    node = tree.css_first(f'meta[{attribute}="{value}"]')
    if node is None:
        return None
    return node.attributes.get("content")


def _title(tree: HTMLParser) -> str | None:
    node = tree.css_first("title")
    return node.text(strip=True) if node is not None else None


def _canonical_link(tree: HTMLParser, base_url: str) -> str | None:
    node = tree.css_first('link[rel="canonical"]')
    if node is None:
        return None
    href = node.attributes.get("href")
    normalized = _optional_https(urljoin(base_url, href)) if href else None
    return normalized if normalized and _host(normalized) == _host(base_url) else None


def _first_text(*values: object, limit: int) -> str:
    for value in values:
        candidates = _text_values(value, limit=limit)
        if candidates:
            return candidates[0]
    return ""


def _text_values(value: object, *, limit: int) -> list[str]:
    raw: Iterable[object] = value if isinstance(value, list) else (value,)
    output: list[str] = []
    for item in raw:
        candidate = item.get("name") or item.get("@value") if isinstance(item, dict) else item
        if not isinstance(candidate, (str, int, float)):
            continue
        normalized = " ".join(str(candidate).split())
        if normalized:
            output.append(normalized[:limit])
    return output


def _keyword_values(value: object) -> list[str]:
    values = _text_values(value, limit=500)
    output: list[str] = []
    for item in values:
        output.extend(
            token.strip()[:160]
            for token in item.replace(";", ",").split(",")
            if token.strip()
        )
    return output[:20]


def _address(value: object) -> tuple[str | None, str | None]:
    if isinstance(value, str):
        normalized = " ".join(value.split())[:300]
        return (normalized or None, None)
    if not isinstance(value, dict):
        return None, None
    country_value = value.get("addressCountry")
    if isinstance(country_value, dict):
        country_value = country_value.get("name")
    country = _first_text(country_value, limit=100) or None
    location = ", ".join(
        part
        for part in (
            _first_text(value.get("addressLocality"), limit=100),
            _first_text(value.get("addressRegion"), limit=100),
            country or "",
        )
        if part
    )
    return location[:300] or None, country


def _profile_links(
    tree: HTMLParser,
    organization: dict[str, Any] | None,
    base_url: str,
) -> tuple[DiscoveredProfile, ...]:
    urls: list[str] = []
    if organization:
        same_as = organization.get("sameAs")
        urls.extend(_text_values(same_as, limit=2048))
    # rel="me" is the explicit "this other profile is also me" declaration, so it is read before the
    # ordinary anchors and cannot be crowded out of the bounded result by a footer full of links.
    urls.extend(_rel_me_links(tree, base_url))
    for node in tree.css("a[href]")[:_MAX_ANCHOR_LINKS]:
        href = node.attributes.get("href")
        if href:
            urls.append(urljoin(base_url, href))
    profiles: dict[tuple[str, str], DiscoveredProfile] = {}
    for url in urls:
        profile = _discovered_profile(url)
        if profile:
            profiles[(profile.provider_key, profile.external_id)] = profile
        if len(profiles) >= _MAX_DISCOVERED_PROFILES:
            break
    return tuple(profiles.values())


def _rel_me_links(tree: HTMLParser, base_url: str) -> list[str]:
    """Read the ``rel="me"`` links the page publishes about itself, bounded.

    ``rel`` is a space-separated token list, so membership is tested per token rather than by
    substring -- ``rel="menu"`` and ``rel="format"`` are not ``me``.
    """
    output: list[str] = []
    for node in tree.css("a[rel], link[rel]")[:_MAX_ANCHOR_LINKS]:
        rel = node.attributes.get("rel") or ""
        if "me" not in rel.casefold().split():
            continue
        href = node.attributes.get("href")
        if href:
            output.append(urljoin(base_url, href))
        if len(output) >= _MAX_REL_ME_LINKS:
            break
    return output


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (TypeError, ValueError) as error:
        raise PublicSourceError("invalid_response", "public source JSON is invalid") from error
    if not isinstance(value, dict):
        raise PublicSourceError("invalid_response", "public source JSON is invalid")
    return value


def _localized_value(value: object, language: str) -> str | None:
    if not isinstance(value, dict):
        return None
    item = value.get(language)
    if not isinstance(item, dict) or not isinstance(item.get("value"), str):
        return None
    return " ".join(item["value"].split()) or None


def _wikidata_strings(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for claim in value:
        try:
            item = claim["mainsnak"]["datavalue"]["value"]
        except (KeyError, TypeError):
            continue
        if isinstance(item, str) and item.strip():
            output.append(item.strip())
    return output


def _wikidata_time(value: object) -> str | None:
    if not isinstance(value, list) or not value:
        return None
    try:
        raw = value[0]["mainsnak"]["datavalue"]["value"]["time"]
    except (KeyError, TypeError):
        return None
    if not isinstance(raw, str):
        return None
    normalized = raw.lstrip("+")
    return (
        normalized[:_DATE_PREFIX_LENGTH]
        if len(normalized) >= _DATE_PREFIX_LENGTH
        else normalized
    )


def _dedupe_facts(
    facts: Iterable[CatalogEntityExternalFactDraft],
) -> tuple[CatalogEntityExternalFactDraft, ...]:
    output: list[CatalogEntityExternalFactDraft] = []
    seen: set[tuple[str, str, str | None]] = set()
    for fact in facts:
        key = (fact.fact_key, fact.value.casefold(), fact.value_url)
        if key in seen:
            continue
        seen.add(key)
        output.append(fact)
        if len(output) >= _MAX_FACTS:
            break
    return tuple(output)
