"""PostgreSQL read adapter for the indexed public entity projection."""

from __future__ import annotations

import builtins
import json
from datetime import UTC, datetime
from typing import Any, cast, get_args
from uuid import UUID

from sqlalchemy import text

from ...domain.catalog_entities import (
    CatalogEntity,
    CatalogEntityCollaborator,
    CatalogEntityDetail,
    CatalogEntityEvent,
    CatalogEntityExternalFact,
    CatalogEntityExternalSource,
    CatalogEntityExternalSourceSnapshot,
    CatalogEntityExternalSourceStatus,
    CatalogEntityFactKey,
    CatalogEntityIdentityStatus,
    CatalogEntityInsights,
    CatalogEntityKind,
    CatalogEntityResearchStatus,
)
from ...domain.catalog_entity_graph import CatalogEntityDirectory, CatalogEntityGraph
from ...infra.db import system_session_scope
from .catalog_entity_graph import directory_from_payload, graph_from_payload

# Derived from the domain ``Literal`` rather than restated, so widening the vocabulary in one
# migration cannot leave a stale Python copy rejecting a value the SQL now accepts.
_ENTITY_KINDS = frozenset(get_args(CatalogEntityKind))
_IDENTITY_STATUSES = frozenset(get_args(CatalogEntityIdentityStatus))
_ENTITY_ROLES = frozenset({"organizer", "host", "speaker", "partner"})
_MAX_ENTITY_LIMIT = 100
_MAX_ENTITY_NAME_LENGTH = 160
_MAX_ENTITY_KINDS = 3
_MIN_PRINTABLE_CODEPOINT = 32
_DELETE_CODEPOINT = 127
# The ego graph and the directory keep their caps here rather than in the parsing module, so the
# five bounds above are declared exactly once in Python.  Each mirrors an ERRCODE '22023' check the
# capability enforces for itself, because both are also reachable from ``psql``; rejecting an
# out-of-range argument here means a malformed request never borrows a connection at all.
_MAX_GRAPH_EVENTS = 24
_MAX_GRAPH_PEERS = 48
_MAX_GRAPH_TOPICS = 6
_MAX_DIRECTORY_LIMIT = 60
_MAX_DIRECTORY_MIN_EVENTS = 10
_MAX_DIRECTORY_CITIES = 8
_MAX_DIRECTORY_CITY_LENGTH = 64
_MAX_OVERVIEW_ENTITIES = 60
_MAX_OVERVIEW_PAIRS = 60
_MAX_ENTITY_IDENTITY_STATUSES = len(_IDENTITY_STATUSES)


def _has_control_character(value: str) -> bool:
    """C0 controls and DEL, the same class the capabilities reject with ``'[\\x00-\\x1f\\x7f]'``."""
    return any(
        ord(character) < _MIN_PRINTABLE_CODEPOINT or ord(character) == _DELETE_CODEPOINT
        for character in value
    )


class PostgresCatalogEntityRepository:
    """Read entities only through bounded security-definer capabilities."""

    async def list(
        self,
        *,
        query: str | None,
        kinds: tuple[str, ...],
        limit: int,
    ) -> list[CatalogEntity]:
        if not 1 <= limit <= _MAX_ENTITY_LIMIT:
            raise ValueError("entity browse limit must be between 1 and 100")
        if query is not None and (
            len(query) > _MAX_ENTITY_NAME_LENGTH or _has_control_character(query)
        ):
            raise ValueError("entity query is invalid")
        if len(kinds) > _MAX_ENTITY_KINDS or any(kind not in _ENTITY_KINDS for kind in kinds):
            raise ValueError("entity kind filter is invalid")
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT * FROM public.fn_list_catalog_entities_v1(
                            :query, CAST(:kinds AS text[]), :limit, NULL, NULL
                        )
                        """
                    ),
                    {"query": query, "kinds": list(dict.fromkeys(kinds)), "limit": limit},
                )
            ).all()
        return [self._entity(row) for row in rows]

    async def get(self, entity_id: UUID, *, event_limit: int = 50) -> CatalogEntityDetail | None:
        if not 1 <= event_limit <= _MAX_ENTITY_LIMIT:
            raise ValueError("entity event limit must be between 1 and 100")
        async with system_session_scope() as session:
            row = (
                await session.execute(
                    text("SELECT * FROM public.fn_get_catalog_entity_v1(:entity_id)"),
                    {"entity_id": entity_id},
                )
            ).first()
            if row is None:
                return None
            event_rows = (
                await session.execute(
                    text(
                        """
                        SELECT * FROM public.fn_list_catalog_entity_events_v2(
                            :entity_id, :limit
                        )
                        """
                    ),
                    {"entity_id": entity_id, "limit": event_limit},
                )
            ).all()
            insight_row = (
                await session.execute(
                    text(
                        "SELECT * FROM public.fn_get_catalog_entity_insights_v2("
                        ":entity_id, :min_shared)"
                    ),
                    # 0156 parameterised the collaborator floor and dropped v1.  1, not v1's
                    # hardcoded 2: the floor of 2 rendered an empty collaborator panel on 87% of
                    # entity detail pages against a graph in which only 11.5% of entities are
                    # actually isolated.  The 16-column shape is unchanged, so the mapper below
                    # is untouched.
                    {"entity_id": entity_id, "min_shared": 1},
                )
            ).first()
            source_rows = (
                await session.execute(
                    text(
                        "SELECT * FROM public.fn_list_catalog_entity_external_sources_v1(:entity_id)"
                    ),
                    {"entity_id": entity_id},
                )
            ).all()
            fact_rows = (
                await session.execute(
                    text(
                        "SELECT * FROM public.fn_list_catalog_entity_external_facts_v2(:entity_id)"
                    ),
                    {"entity_id": entity_id},
                )
            ).all()
        entity = self._entity(row)
        sources = tuple(self._external_source(source) for source in source_rows)
        return CatalogEntityDetail(
            entity=entity,
            events=tuple(self._event(event) for event in event_rows),
            external_sources=sources,
            external_facts=tuple(self._external_fact(fact) for fact in fact_rows),
            refresh_due=(
                entity.identity_status == "profile_verified"
                and (
                    not sources
                    or any(source.next_refresh_at <= datetime.now(UTC) for source in sources)
                )
            ),
            insights=self._insights(insight_row) if insight_row is not None else None,
        )

    async def replace_external_source(
        self,
        snapshot: CatalogEntityExternalSourceSnapshot,
    ) -> None:
        facts = [
            {
                "key": fact.fact_key,
                "value": fact.value,
                "url": fact.value_url,
                "order": fact.sort_order,
            }
            for fact in snapshot.facts
        ]
        async with system_session_scope() as session:
            await session.execute(
                text(
                    """
                    SELECT public.fn_replace_catalog_entity_external_source_v1(
                        :entity_id, :provider_key, :external_id, :source_url,
                        :display_name, :status, :fetched_at, :next_refresh_at,
                        :error_code, CAST(:facts AS jsonb)
                    )
                    """
                ),
                {
                    "entity_id": snapshot.entity_id,
                    "provider_key": snapshot.provider_key,
                    "external_id": snapshot.external_id,
                    "source_url": snapshot.source_url,
                    "display_name": snapshot.display_name,
                    "status": snapshot.status,
                    "fetched_at": snapshot.fetched_at,
                    "next_refresh_at": snapshot.next_refresh_at,
                    "error_code": snapshot.error_code,
                    "facts": json.dumps(facts, separators=(",", ":")),
                },
            )

    async def list_due_for_refresh(self, limit: int) -> builtins.list[UUID]:
        if not 1 <= limit <= _MAX_ENTITY_LIMIT:
            raise ValueError("entity refresh limit must be between 1 and 100")
        async with system_session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT entity_id FROM public.fn_list_catalog_entities_due_for_refresh_v1(:limit)"
                    ),
                    {"limit": limit},
                )
            ).all()
        return [row.entity_id for row in rows]

    async def graph(
        self,
        entity_id: UUID,
        *,
        event_limit: int = 18,
        peer_limit: int = 32,
        topic_limit: int = 4,
    ) -> CatalogEntityGraph | None:
        """Return one bounded ego bundle, or ``None`` when the entity does not exist.

        One round trip, always, and deliberately not routed through :meth:`get`: that method issues
        five sequential statements for a single entity, so hydrating even 32 peers through it would
        be 160 round trips against a pool configured ``pool_size=5, max_overflow=0``.  The
        capability returns the ego, its events, its peers, its topics, the mention edges between
        them and the exact-name review candidates as a single ``jsonb`` document.
        """
        if not 1 <= event_limit <= _MAX_GRAPH_EVENTS:
            raise ValueError("entity graph event limit must be between 1 and 24")
        if not 1 <= peer_limit <= _MAX_GRAPH_PEERS:
            raise ValueError("entity graph peer limit must be between 1 and 48")
        if not 0 <= topic_limit <= _MAX_GRAPH_TOPICS:
            raise ValueError("entity graph topic limit must be between 0 and 6")
        async with system_session_scope() as session:
            payload = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_get_catalog_entity_graph_v1(
                            :entity_id, :events, :peers, :topics
                        )
                        """
                    ),
                    {
                        "entity_id": entity_id,
                        "events": event_limit,
                        "peers": peer_limit,
                        "topics": topic_limit,
                    },
                )
            ).scalar_one()
        return None if payload is None else graph_from_payload(payload)

    async def overview(
        self,
        *,
        query: str | None,
        kinds: tuple[str, ...],
        identity_statuses: tuple[str, ...],
        entity_limit: int = 40,
        pair_limit: int = 40,
    ) -> CatalogEntityGraph:
        """Return the landing graph: the top hubs and the events that bridge them, in one trip.

        Same envelope as :meth:`graph`, so the same dataclasses and the same parser serve both.
        The difference is the subject: there is no ego, so ring 0 is every ranked hub and ring 1 is
        one representative event per connected pair -- the most recent event the pair shares.  The
        capability draws one event per *pair* rather than every bridging event because the top 40
        hubs are bridged by 268 distinct events but express only 18 connected pairs; drawing all
        268 is unreadable, and keeping the events that join the most entities instead was measured
        to collapse those 40 connected entities to 10.

        Never routed through :meth:`get`, which issues five statements per entity: hydrating 40
        hubs that way would be 200 round trips against a pool configured
        ``pool_size=5, max_overflow=0``.

        Every bound below mirrors an ``ERRCODE '22023'`` check the capability enforces for itself,
        because the function is also reachable from ``psql``.  Rejecting here means a malformed
        request never borrows a connection at all.
        """
        if not 1 <= entity_limit <= _MAX_OVERVIEW_ENTITIES:
            raise ValueError("entity overview entity limit must be between 1 and 60")
        if not 1 <= pair_limit <= _MAX_OVERVIEW_PAIRS:
            raise ValueError("entity overview pair limit must be between 1 and 60")
        if query is not None and (
            len(query) > _MAX_ENTITY_NAME_LENGTH or _has_control_character(query)
        ):
            raise ValueError("entity query is invalid")
        # Deduplicated before the cardinality check, as in :meth:`directory`: the capability counts
        # the deduplicated array, so validating the caller's raw tuple would reject a request the
        # SQL accepts -- a spurious 4xx for a reader who clicked one chip four times.
        scoped_kinds = list(dict.fromkeys(kinds))
        scoped_statuses = list(dict.fromkeys(identity_statuses))
        if len(scoped_kinds) > _MAX_ENTITY_KINDS or any(
            kind not in _ENTITY_KINDS for kind in scoped_kinds
        ):
            raise ValueError("entity kind filter is invalid")
        if len(scoped_statuses) > _MAX_ENTITY_IDENTITY_STATUSES or any(
            status not in _IDENTITY_STATUSES for status in scoped_statuses
        ):
            raise ValueError("entity identity filter is invalid")
        async with system_session_scope() as session:
            payload = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_get_catalog_entity_overview_graph_v1(
                            :query, CAST(:kinds AS text[]), CAST(:identity_statuses AS text[]),
                            :entities, :pairs
                        )
                        """
                    ),
                    {
                        "query": query,
                        "kinds": scoped_kinds,
                        "identity_statuses": scoped_statuses,
                        "entities": entity_limit,
                        "pairs": pair_limit,
                    },
                )
            ).scalar_one()
        return graph_from_payload(payload)

    async def directory(
        self,
        *,
        query: str | None,
        kinds: tuple[str, ...],
        city_norms: tuple[str, ...],
        limit: int,
        min_events: int,
    ) -> CatalogEntityDirectory:
        """Return the ranked front door with its coverage caveat, in one round trip."""
        if not 1 <= limit <= _MAX_DIRECTORY_LIMIT:
            raise ValueError("entity directory limit must be between 1 and 60")
        if not 1 <= min_events <= _MAX_DIRECTORY_MIN_EVENTS:
            raise ValueError("entity directory minimum must be between 1 and 10")
        if query is not None and (
            len(query) > _MAX_ENTITY_NAME_LENGTH or _has_control_character(query)
        ):
            raise ValueError("entity query is invalid")
        # Deduplicated before the cardinality check, not after, because the deduplicated array is
        # what the capability receives and counts: validating the caller's raw tuple would reject
        # four repeats of one kind that the SQL would have accepted as a single-element array.
        scoped_kinds = list(dict.fromkeys(kinds))
        scoped_cities = list(dict.fromkeys(city_norms))
        if len(scoped_kinds) > _MAX_ENTITY_KINDS or any(
            kind not in _ENTITY_KINDS for kind in scoped_kinds
        ):
            raise ValueError("entity kind filter is invalid")
        if len(scoped_cities) > _MAX_DIRECTORY_CITIES or any(
            len(city) > _MAX_DIRECTORY_CITY_LENGTH or _has_control_character(city)
            for city in scoped_cities
        ):
            raise ValueError("entity city filter is invalid")
        async with system_session_scope() as session:
            payload = (
                await session.execute(
                    text(
                        """
                        SELECT public.fn_get_catalog_entity_directory_v1(
                            :query, CAST(:kinds AS text[]), CAST(:cities AS text[]),
                            :limit, :min_events
                        )
                        """
                    ),
                    {
                        "query": query,
                        "kinds": scoped_kinds,
                        "cities": scoped_cities,
                        "limit": limit,
                        "min_events": min_events,
                    },
                )
            ).scalar_one()
        return directory_from_payload(payload)

    async def resolve(self, event_id: UUID, role: str, name: str) -> UUID | None:
        normalized_name = " ".join(name.split())
        if role not in _ENTITY_ROLES or not 1 <= len(normalized_name) <= _MAX_ENTITY_NAME_LENGTH:
            raise ValueError("event entity reference is invalid")
        async with system_session_scope() as session:
            return cast(
                UUID | None,
                (
                    await session.execute(
                        text(
                            """
                            SELECT public.fn_resolve_catalog_event_entity_v1(
                                :event_id, :role, :name
                            )
                            """
                        ),
                        {"event_id": event_id, "role": role, "name": normalized_name},
                    )
                ).scalar_one(),
            )

    @staticmethod
    def _entity(row: Any) -> CatalogEntity:
        return CatalogEntity(
            entity_id=row.entity_id,
            display_name=str(row.display_name),
            kind=cast(CatalogEntityKind, str(row.kind)),
            identity_status=cast(CatalogEntityIdentityStatus, str(row.identity_status)),
            canonical_profile_url=(
                str(row.canonical_profile_url) if row.canonical_profile_url is not None else None
            ),
            summary=str(row.summary) if row.summary is not None else None,
            website_url=str(row.website_url) if row.website_url is not None else None,
            logo_url=str(row.logo_url) if row.logo_url is not None else None,
            city=str(row.city) if row.city is not None else None,
            country=str(row.country) if row.country is not None else None,
            event_count=int(row.event_count),
            roles=tuple(str(value) for value in row.roles),
            source_count=int(row.source_count),
            research_status=cast(CatalogEntityResearchStatus, str(row.research_status)),
        )

    @staticmethod
    def _event(row: Any) -> CatalogEntityEvent:
        return CatalogEntityEvent(
            canonical_event_id=row.canonical_event_id,
            title=str(row.title),
            start_at=row.start_at,
            end_at=row.end_at,
            venue_name=str(row.venue_name) if row.venue_name is not None else None,
            city=str(row.city) if row.city is not None else None,
            description=str(row.description),
            roles=tuple(str(value) for value in row.roles),
            source_labels=tuple(str(value) for value in row.source_labels),
            registration_url=str(row.registration_url),
            is_past=bool(row.is_past),
        )

    @staticmethod
    def _insights(row: Any) -> CatalogEntityInsights:
        return CatalogEntityInsights(
            event_count=int(row.event_count),
            upcoming_count=int(row.upcoming_count),
            past_count=int(row.past_count),
            first_event_at=row.first_event_at,
            last_event_at=row.last_event_at,
            recent_event_count=int(row.recent_event_count),
            active_months=int(row.active_months),
            events_per_month=(
                float(row.events_per_month) if row.events_per_month is not None else None
            ),
            typical_attendance=(
                int(row.typical_attendance) if row.typical_attendance is not None else None
            ),
            free_count=int(row.free_count),
            paid_count=int(row.paid_count),
            top_topics=tuple(str(value) for value in row.top_topics),
            top_venues=tuple(str(value) for value in row.top_venues),
            top_cities=tuple(str(value) for value in row.top_cities),
            source_labels=tuple(str(value) for value in row.source_labels),
            collaborators=tuple(
                CatalogEntityCollaborator(
                    entity_id=UUID(str(peer["entity_id"])),
                    display_name=str(peer["display_name"]),
                    kind=cast(CatalogEntityKind, str(peer["kind"])),
                    shared_event_count=int(peer["shared_event_count"]),
                )
                for peer in row.collaborators
            ),
        )

    @staticmethod
    def _external_source(row: Any) -> CatalogEntityExternalSource:
        return CatalogEntityExternalSource(
            provider_key=str(row.provider_key),
            external_id=str(row.external_id),
            source_url=str(row.source_url),
            display_name=str(row.display_name),
            status=cast(CatalogEntityExternalSourceStatus, str(row.status)),
            fetched_at=row.fetched_at,
            next_refresh_at=row.next_refresh_at,
            error_code=str(row.error_code) if row.error_code is not None else None,
        )

    @staticmethod
    def _external_fact(row: Any) -> CatalogEntityExternalFact:
        return CatalogEntityExternalFact(
            provider_key=str(row.provider_key),
            source_url=str(row.source_url),
            fact_key=cast(CatalogEntityFactKey, str(row.fact_key)),
            value=str(row.fact_value),
            value_url=str(row.fact_url) if row.fact_url is not None else None,
            sort_order=int(row.sort_order),
            observed_at=row.observed_at,
        )
