"""Refresh structured public facts for exact event-entity identities."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

from ..adapters.entity_intelligence.public_sources import (
    CollectedPublicSource,
    GitHubOrganizationSource,
    OfficialWebsiteSource,
    PublicSourceError,
    WikidataSource,
    linked_profile_source,
    social_profile_source,
)
from ..domain.catalog_entities import (
    CatalogEntityDetail,
    CatalogEntityExternalSourceSnapshot,
    CatalogEntityKind,
)
from ..domain.social_profiles import SOCIAL_PROVIDER_KEYS, SOCIAL_PROVIDER_LABELS

_LINKEDIN_HOSTS = frozenset({"linkedin.com", "www.linkedin.com"})
_FRESH_TTL = timedelta(days=7)
_WIKIDATA_TTL = timedelta(days=14)
_LINK_TTL = timedelta(days=30)
_FAILURE_TTL = timedelta(days=1)
_BLOCKED_TTL = timedelta(days=30)


class CatalogEntityIntelligenceRepository(Protocol):
    async def get(self, entity_id: UUID, *, event_limit: int = 50) -> CatalogEntityDetail | None:
        ...

    async def replace_external_source(
        self,
        snapshot: CatalogEntityExternalSourceSnapshot,
    ) -> None:
        ...

    async def list_due_for_refresh(self, limit: int) -> list[UUID]:
        ...


class EntityIntelligenceService:
    """Resolve only URL-anchored public sources and materialize bounded facts."""

    def __init__(
        self,
        repository: CatalogEntityIntelligenceRepository,
        *,
        official_website: OfficialWebsiteSource,
        github: GitHubOrganizationSource,
        wikidata: WikidataSource,
    ) -> None:
        self._repository = repository
        self._official_website = official_website
        self._github = github
        self._wikidata = wikidata

    async def refresh(self, entity_id: UUID) -> CatalogEntityDetail | None:
        detail = await self._repository.get(entity_id)
        if detail is None:
            return None
        entity = detail.entity
        profile_url = entity.canonical_profile_url
        if entity.identity_status != "profile_verified" or profile_url is None:
            return detail

        try:
            primary = await self._collect_primary(entity_id, profile_url, entity.kind)
        except PublicSourceError:
            return await self._repository.get(entity_id)
        await self._store_collection(entity_id, primary)
        processed = {primary.provider_key}
        for profile in primary.discovered_profiles:
            if profile.provider_key in processed:
                continue
            processed.add(profile.provider_key)
            try:
                if profile.provider_key == "linkedin_profile":
                    collected = linked_profile_source(profile.url)
                elif profile.provider_key == "github_public":
                    collected = await self._github.collect(profile.url)
                elif profile.provider_key == "wikidata_public":
                    collected = await self._wikidata.collect(profile.url)
                elif profile.provider_key == "official_website":
                    collected = await self._official_website.collect(profile.url)
                elif profile.provider_key in SOCIAL_PROVIDER_KEYS:
                    # Link only.  The platform is never fetched -- not to enrich it, and not to
                    # "verify" that the profile exists.
                    collected = social_profile_source(profile.url)
                else:
                    continue
                await self._store_collection(entity_id, collected)
            except PublicSourceError as error:
                await self._store_failure(
                    entity_id=entity_id,
                    provider_key=profile.provider_key,
                    external_id=profile.external_id,
                    source_url=profile.url,
                    display_name=profile.display_name,
                    error=error,
                )
        return await self._repository.get(entity_id)

    async def refresh_due(self, limit: int) -> list[UUID]:
        refreshed: list[UUID] = []
        for entity_id in await self._repository.list_due_for_refresh(limit):
            if await self.refresh(entity_id) is not None:
                refreshed.append(entity_id)
        return refreshed

    async def _collect_primary(
        self,
        entity_id: UUID,
        profile_url: str,
        kind: CatalogEntityKind,
    ) -> CollectedPublicSource:
        host = (urlsplit(profile_url).hostname or "").casefold()
        try:
            if host in _LINKEDIN_HOSTS:
                return linked_profile_source(profile_url)
            if self._github.supports(profile_url, kind):
                return await self._github.collect(profile_url)
            if self._wikidata.supports(profile_url, kind):
                return await self._wikidata.collect(profile_url)
            if self._official_website.supports(profile_url, kind):
                return await self._official_website.collect(profile_url)
            raise PublicSourceError("unsupported_profile", "profile source is unsupported")
        except PublicSourceError as error:
            provider_key = _provider_for_url(profile_url)
            await self._store_failure(
                entity_id=entity_id,
                provider_key=provider_key,
                external_id=f"profile:{profile_url}",
                source_url=profile_url,
                display_name=_provider_label(provider_key),
                error=error,
            )
            raise

    async def _store_collection(
        self,
        entity_id: UUID,
        collected: CollectedPublicSource,
    ) -> None:
        now = datetime.now(UTC)
        # A social row is a link we were given, never a page we read, so it carries no fetched_at
        # for the same reason a LinkedIn row does not.
        linked = (
            collected.provider_key == "linkedin_profile"
            or collected.provider_key in SOCIAL_PROVIDER_KEYS
        )
        ttl = (
            _LINK_TTL
            if linked
            else _WIKIDATA_TTL
            if collected.provider_key == "wikidata_public"
            else _FRESH_TTL
        )
        await self._repository.replace_external_source(
            CatalogEntityExternalSourceSnapshot(
                entity_id=entity_id,
                provider_key=collected.provider_key,
                external_id=collected.external_id,
                source_url=collected.source_url,
                display_name=collected.display_name,
                status="linked" if linked else "fresh",
                fetched_at=None if linked else now,
                next_refresh_at=now + ttl,
                error_code=None,
                facts=collected.facts,
            )
        )

    async def _store_failure(
        self,
        *,
        entity_id: UUID,
        provider_key: str,
        external_id: str,
        source_url: str,
        display_name: str,
        error: PublicSourceError,
    ) -> None:
        now = datetime.now(UTC)
        blocked = error.code == "network_policy"
        await self._repository.replace_external_source(
            CatalogEntityExternalSourceSnapshot(
                entity_id=entity_id,
                provider_key=provider_key,
                external_id=external_id[:500],
                source_url=source_url,
                display_name=display_name,
                status="blocked" if blocked else "failed",
                fetched_at=None,
                next_refresh_at=now + (_BLOCKED_TTL if blocked else _FAILURE_TTL),
                error_code=error.code,
            )
        )


def _provider_for_url(url: str) -> str:
    host = (urlsplit(url).hostname or "").casefold()
    if host in _LINKEDIN_HOSTS:
        return "linkedin_profile"
    if host in {"github.com", "www.github.com"}:
        return "github_public"
    if host in {"wikidata.org", "www.wikidata.org"}:
        return "wikidata_public"
    return "official_website"


def _provider_label(provider_key: str) -> str:
    return {
        "linkedin_profile": "LinkedIn",
        "github_public": "GitHub",
        "wikidata_public": "Wikidata",
        "official_website": "Official website",
        **SOCIAL_PROVIDER_LABELS,
    }.get(provider_key, "Public source")


def source_provider_keys(sources: Sequence[CollectedPublicSource]) -> tuple[str, ...]:
    """Return stable connector identities for diagnostics and tests."""
    return tuple(dict.fromkeys(source.provider_key for source in sources))
