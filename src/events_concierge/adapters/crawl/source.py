"""PublicJsonLdSource: the free-crawl SourcePort implementation (FR-3.x).

Discovery-only foundation source. discover() fetches seed pages (or replays an offline fixture),
runs the JSON-LD ACL, and post-filters to the request constraints. register() always routes to human
handoff -- a public crawl surface cannot secure a place autonomously."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from urllib.parse import urlparse
from uuid import UUID

import httpx

from ...domain.catalog_sources import CatalogSource
from ...domain.catalog_window import collection_reference_time
from ...domain.enums import CatalogSourceMode, GroupCondition, Modality, RsvpState, Source
from ...domain.events import CandidateEvent
from ...domain.request import RequestConstraints
from ...infra.logging import get_logger
from ...ports.sources import (
    RegisterOutcome,
    RegisterResult,
    RegistrationTarget,
    SourceCapability,
)
from .acl import parse_jsonld

_log = get_logger("crawl.source")

_FETCH_TIMEOUT_S = 15.0


class PublicJsonLdSource:
    """Implements SourcePort over public schema.org/JSON-LD pages (Source.PUBLIC_JSONLD)."""

    def __init__(
        self,
        *,
        user_agent: str,
        min_interval_ms: int = 1500,
        seed_urls: list[str] | None = None,
        fixture_events: list[CandidateEvent] | None = None,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._min_interval_ms = min_interval_ms
        self._seed_urls = list(seed_urls or [])
        self._fixture_events = fixture_events
        self._now = now or (lambda: datetime.now(UTC))
        self._clock = clock or monotonic
        self._sleep = sleep or asyncio.sleep
        self._transport = transport
        self._last_request_at: dict[str, float] = {}
        self._pacing_lock = asyncio.Lock()
        self.capability = SourceCapability(
            source=Source.PUBLIC_JSONLD,
            supports_api=False,
            supports_browser_discovery=True,
            supports_autonomous_register=False,
        )

    async def discover(self, constraints: RequestConstraints) -> list[CandidateEvent]:
        """Return constraint-filtered candidates. Uses the fixture (offline slice) when set,
        else crawls each seed URL at a human cadence (FR-10.4)."""
        if self._fixture_events is not None:
            candidates = list(self._fixture_events)
        else:
            candidates = await self._crawl_seeds()
        now = self._now()
        return [c for c in candidates if self._matches(c, constraints, now)]

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        """Fetch one reviewed registry source without sign-in, registration, or redirect escape.

        ``CatalogRefreshService`` owns the durable run lease and cross-worker Pacer lease before
        calling this adapter. This adapter contributes its per-host human cadence and validates
        every redirect against the source's explicit HTTPS-origin allowlist (FR-10.3/10.4).
        """
        if source.mode is not CatalogSourceMode.PUBLIC_JSONLD:
            raise ValueError(f"unsupported public crawler mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("public JSON-LD catalog sources must remain handoff-only")
        candidates = await self._crawl_urls([source.seed_url], catalog_source=source)
        now = collection_reference_time(source, self._now())
        return [
            candidate
            for candidate in candidates
            if self._matches(candidate, RequestConstraints(), now)
        ]

    async def _crawl_seeds(self) -> list[CandidateEvent]:
        return await self._crawl_urls(self._seed_urls)

    async def _crawl_urls(
        self, urls: list[str], *, catalog_source: CatalogSource | None = None
    ) -> list[CandidateEvent]:
        candidates: list[CandidateEvent] = []
        headers = {"User-Agent": self._user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=catalog_source is None,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            for url in urls:
                try:
                    response: httpx.Response | None
                    if catalog_source is None:
                        await self._wait_for_host_slot(url)
                        response = await client.get(url)
                        response.raise_for_status()
                    else:
                        response = await self._get_approved_source_response(client, catalog_source)
                        if response is None:
                            continue
                except httpx.HTTPError as exc:
                    _log.warning("crawl_fetch_failed", url=url, error=str(exc))
                    continue
                assert response is not None
                candidates.extend(parse_jsonld(response.text, url))
        return candidates

    async def _get_approved_source_response(
        self, client: httpx.AsyncClient, source: CatalogSource
    ) -> httpx.Response | None:
        """Follow at most five redirects, making no request beyond approved origins (FR-10.3)."""
        url = source.seed_url
        for _ in range(5):
            await self._wait_for_host_slot(url, min_interval_ms=source.min_interval_ms)
            response = await client.get(url, follow_redirects=False)
            if response.is_redirect:
                location = response.headers.get("location")
                next_url = str(response.url.join(location)) if location else ""
                if not location or not source.allows_url(next_url):
                    _log.warning(
                        "catalog_redirect_rejected", source_key=source.source_key, url=next_url
                    )
                    return None
                url = next_url
                continue
            if not source.allows_url(str(response.url)):
                _log.warning(
                    "catalog_final_url_rejected",
                    source_key=source.source_key,
                    url=str(response.url),
                )
                return None
            response.raise_for_status()
            return response
        _log.warning(
            "catalog_redirect_limit", source_key=source.source_key, seed_url=source.seed_url
        )
        return None

    async def _wait_for_host_slot(self, url: str, *, min_interval_ms: int | None = None) -> None:
        """Enforce the FR-10.4 human-cadence floor across repeated discovery calls per host."""
        host = urlparse(url).netloc.lower()
        if not host:
            raise ValueError("crawl seed URL must include a host")
        interval_s = (min_interval_ms or self._min_interval_ms) / 1000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()

    @staticmethod
    def _matches(candidate: CandidateEvent, constraints: RequestConstraints, now: datetime) -> bool:
        """Apply time and explicit free-only constraints; never resurface an elapsed listing (FR-4.6)."""
        if candidate.start_at < now:
            return False
        if constraints.time_window is not None and not constraints.time_window.contains(
            candidate.start_at
        ):
            return False
        return constraints.accepts_price(candidate.price_status)

    async def register(
        self,
        tenant_id: UUID,
        target: RegistrationTarget,
        modality: Modality,
        idempotency_key: str,
    ) -> RegisterResult:
        """A public crawl source never registers autonomously -> hand off to the user (FR-3.x)."""
        return RegisterResult(
            outcome=RegisterOutcome.NEEDS_HANDOFF,
            detail="public crawl source is discovery-only",
        )

    async def read_registration_state(
        self, tenant_id: UUID, target: RegistrationTarget, modality: Modality
    ) -> RsvpState:
        """A discovery-only surface has no RSVP state to inspect (FR-5.3/5.5)."""
        return RsvpState.NOT_PRESENT

    async def read_membership_state(
        self, tenant_id: UUID, target: RegistrationTarget
    ) -> GroupCondition:
        """Public discovery carries no authenticated group-membership concept (FR-5.2)."""
        return GroupCondition.UNKNOWN
