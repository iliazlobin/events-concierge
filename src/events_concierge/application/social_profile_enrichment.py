"""Background-only social API refresh with durable claims and a shared daily request cap."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from ..adapters.entity_intelligence.public_sources import CollectedPublicSource
from ..adapters.entity_intelligence.social_api import SocialApiError

_MAX_BATCH = 10


@dataclass(frozen=True, slots=True)
class SocialProfileClaim:
    entity_id: UUID
    provider_key: str
    source_url: str
    previous_id: str | None
    lease_token: UUID


class SocialProfileSource(Protocol):
    provider_key: str

    async def collect(self, url: str, previous_id: str | None = None) -> CollectedPublicSource: ...


class SocialProfileRefreshRepository(Protocol):
    async def claim(
        self, providers: tuple[str, ...], daily_limit: int
    ) -> SocialProfileClaim | None: ...

    async def finish(
        self,
        claim: SocialProfileClaim,
        collection: CollectedPublicSource | None,
        *,
        error_code: str | None,
        refresh_seconds: int,
    ) -> bool: ...


class SocialProfileEnrichmentService:
    def __init__(
        self,
        repository: SocialProfileRefreshRepository,
        sources: tuple[SocialProfileSource, ...],
        *,
        daily_limit: int = 100,
        refresh_seconds: int = 86_400,
    ) -> None:
        self._repository = repository
        self._sources = {source.provider_key: source for source in sources}
        self._daily_limit = daily_limit
        self._refresh_seconds = refresh_seconds

    async def refresh_due(self, limit: int) -> list[UUID]:
        if not self._sources:
            return []
        if not 1 <= limit <= _MAX_BATCH:
            raise ValueError("social refresh batch must be between 1 and 10")
        refreshed: list[UUID] = []
        for _ in range(limit):
            claim = await self._repository.claim(tuple(self._sources), self._daily_limit)
            if claim is None:
                break
            collection = None
            error_code = None
            refresh_seconds = self._refresh_seconds
            try:
                async with asyncio.timeout(25):
                    collection = await self._sources[claim.provider_key].collect(
                        claim.source_url, claim.previous_id
                    )
            except SocialApiError as error:
                error_code = error.code
                refresh_seconds = max(self._refresh_seconds, error.retry_seconds)
            except TimeoutError:
                error_code = "unavailable"
            if await self._repository.finish(
                claim, collection, error_code=error_code, refresh_seconds=refresh_seconds
            ):
                refreshed.append(claim.entity_id)
        return refreshed
