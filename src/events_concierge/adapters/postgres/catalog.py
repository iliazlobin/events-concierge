"""Tenant-neutral catalog repository: dedup-on-ingest into canonical events (retaining all source
links) and hybrid retrieval (dense pgvector ANN + sparse tsvector) fused by Reciprocal Rank Fusion."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain import dedup
from ...domain.catalog_browse import (
    MAX_CATALOG_NAME_SUGGESTIONS,
    MIN_CATALOG_NAME_QUERY_LENGTH,
    CatalogBrowseCity,
    CatalogBrowseCursor,
    CatalogBrowseDay,
    CatalogBrowseDayTopic,
    CatalogBrowseEvent,
    CatalogBrowseProvider,
    CatalogBrowseSort,
    CatalogBrowseSource,
    CatalogBrowseTopic,
    CatalogNameKind,
    CatalogNameSuggestion,
)
from ...domain.enums import PriceStatus, Source
from ...domain.event_semantics import (
    CATALOG_TOPICS,
    MAX_CATALOG_TOPIC_SELECTIONS,
    TOPIC_LABELS,
    EventSemanticProjection,
    extract_event_semantics,
    extraction_evidence_payload,
)
from ...domain.events import (
    MAX_PUBLIC_PRICE_CENTS,
    CandidateEvent,
    CanonicalEvent,
    EventEntityProfile,
    EventSourceLink,
    aggregate_price_range,
    aggregate_price_status,
    event_entity_profiles_payload,
)
from ...domain.request import RequestConstraints
from ...infra.db import system_session_scope
from ...ports.ranking import EmbeddingPort
from ._mapping import canonical_from_row, link_from_row, vector_literal

RRF_K = 60
_FUZZY_LOCK_BUCKET = dedup.TIME_DELTA
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_MAX_CATALOG_BROWSE_LIMIT = 101
_MAX_CATALOG_BROWSE_WINDOW = timedelta(days=370)
_MAX_SOURCE_CATALOG_ARCHIVE_WINDOW = timedelta(days=7_305)
_MAX_CATALOG_FILTER_LENGTH = 160
_MAX_CATALOG_CITY_FILTERS = 20
_MAX_CATALOG_SOURCE_FILTERS = 40
_MAX_CATALOG_DATE_RANGES = 8
_CATALOG_LOCATION_SCOPES = frozenset({"bay_area", "manhattan", "los_angeles_area"})
_MIN_PRINTABLE_CODEPOINT = 0x20
_DELETE_CODEPOINT = 0x7F


_CATALOG_TIME_ZONE_PATTERN = re.compile(r"[A-Za-z0-9+_/-]{1,64}")
# Events carrying no topics are counted under this synthetic bucket, matching the consumer
# calendar's own taxonomy. It is deliberately not a member of CATALOG_TOPICS.
_CATALOG_UNTOPICED_BUCKET = "other"


def _validated_catalog_filter_inputs(
    *,
    query: str | None,
    source_keys: tuple[str, ...] = (),
    city_filters: tuple[str, ...],
    location_scopes: tuple[str, ...],
    price: str | None,
    price_max_cents: int | None,
    price_min_cents: int | None = None,
    topics: tuple[str, ...],
    availability: str | None = None,
) -> tuple[str, ...]:
    """Reject the scalar filters both catalog read paths share and normalize the topic selection.

    The page and the day summary must refuse identical inputs, or a summarized range could
    describe a filter the paged agenda rejects.
    """
    if len(city_filters) > _MAX_CATALOG_CITY_FILTERS:
        raise ValueError("catalog browse has too many city filters")
    if len(source_keys) > _MAX_CATALOG_SOURCE_FILTERS:
        raise ValueError("catalog browse has too many source filters")
    for value in (query, *city_filters):
        if value is not None and (
            len(value) > _MAX_CATALOG_FILTER_LENGTH
            or any(
                ord(character) < _MIN_PRINTABLE_CODEPOINT or ord(character) == _DELETE_CODEPOINT
                for character in value
            )
        ):
            raise ValueError("catalog browse filter is invalid")
    if price not in {None, "free", "paid", "unknown"}:
        raise ValueError("catalog browse price is invalid")
    if availability not in {None, "available", "sold_out"}:
        raise ValueError("catalog browse availability is invalid")
    if any(scope not in _CATALOG_LOCATION_SCOPES for scope in location_scopes) or len(
        location_scopes
    ) > len(_CATALOG_LOCATION_SCOPES):
        raise ValueError("catalog browse location scope is invalid")
    if price_max_cents is not None and (
        isinstance(price_max_cents, bool)
        or not 1 <= price_max_cents <= MAX_PUBLIC_PRICE_CENTS
        or price == "unknown"
    ):
        raise ValueError("catalog browse maximum price is invalid")
    # A floor cannot describe a free or unpriced event, and an inverted band selects nothing, so
    # both are rejected here rather than returning a silently empty page.
    if price_min_cents is not None and (
        isinstance(price_min_cents, bool)
        or not 1 <= price_min_cents <= MAX_PUBLIC_PRICE_CENTS
        or price in {"free", "unknown"}
        or (price_max_cents is not None and price_min_cents > price_max_cents)
    ):
        raise ValueError("catalog browse minimum price is invalid")
    normalized_topics = tuple(dict.fromkeys(topics))
    if len(normalized_topics) > MAX_CATALOG_TOPIC_SELECTIONS or any(
        topic not in CATALOG_TOPICS for topic in normalized_topics
    ):
        raise ValueError("catalog browse topic filter is invalid")
    return normalized_topics


def _validated_catalog_time_zone(time_zone: str) -> str:
    """Reject a time zone before it reaches the database.

    The capability validates the zone too, but a database exception surfaces as a
    driver error rather than an invalid-argument the API can answer with 422.
    """
    if not _CATALOG_TIME_ZONE_PATTERN.fullmatch(time_zone):
        raise ValueError("catalog browse time zone is invalid")
    try:
        ZoneInfo(time_zone)
    except (ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError("catalog browse time zone is invalid") from error
    return time_zone


def _catalog_day_topic_label(topic: str) -> str:
    if topic == _CATALOG_UNTOPICED_BUCKET:
        return "Other"
    return TOPIC_LABELS.get(topic, topic)


def _validated_catalog_sort(sort: CatalogBrowseSort) -> CatalogBrowseSort:
    if sort not in {"soonest", "latest"}:
        raise ValueError("catalog browse sort is invalid")
    return sort


def _catalog_browse_ranges(
    *,
    starts_after: datetime | None,
    starts_before: datetime | None,
    date_ranges: tuple[tuple[datetime, datetime], ...],
    max_window: timedelta,
) -> tuple[tuple[datetime, datetime], ...]:
    if (starts_after is None) != (starts_before is None):
        raise ValueError("catalog browse window must provide both bounds")
    if date_ranges and starts_after is not None:
        raise ValueError("catalog browse window must use one date encoding")
    requested = (
        date_ranges
        if date_ranges
        else (((starts_after, cast(datetime, starts_before)),) if starts_after is not None else ())
    )
    if len(requested) > _MAX_CATALOG_DATE_RANGES:
        raise ValueError("catalog browse has too many date ranges")
    if (
        any(
            start.tzinfo is None
            or start.utcoffset() is None
            or end.tzinfo is None
            or end.utcoffset() is None
            or end <= start
            or end - start > max_window
            for start, end in requested
        )
        or sum((end - start for start, end in requested), timedelta()) > max_window
    ):
        raise ValueError("catalog browse window is invalid")
    return requested


def _semantic_projection(candidate: CandidateEvent) -> EventSemanticProjection:
    projection = extract_event_semantics(candidate.title, candidate.description, candidate.raw)
    if _is_tech_week_candidate(candidate):
        # This official calendar has no admission-price field. Mentions of complimentary
        # refreshments must not turn an unknown ticket price or its evidence into "free".
        return EventSemanticProjection(
            projection.topics, None,
            tuple(item for item in projection.evidence if item.field != "price_status"),
        )
    return projection


def _is_tech_week_candidate(candidate: CandidateEvent) -> bool:
    return candidate.source is Source.PUBLIC_JSONLD and candidate.source_event_id.startswith("tech-week:")


def _conservative_shared_entities(
    existing: CanonicalEvent,
    candidate: CandidateEvent,
) -> tuple[
    str | None,
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    tuple[EventEntityProfile, ...],
]:
    """Fill verified links without letting one publisher rewrite another's identity."""
    organizer_name = existing.organizer_name or candidate.organizer_name

    def merged_names(
        role: str,
        current: tuple[str, ...],
        observed: tuple[str, ...],
    ) -> tuple[str, ...]:
        proposed = observed if len(observed) > len(current) else current
        protected = {
            profile.name.casefold() for profile in existing.entity_profiles if profile.role == role
        }
        if protected.issubset(name.casefold() for name in proposed):
            return proposed
        return current

    host_names = merged_names("host", existing.host_names, candidate.host_names)
    speaker_names = merged_names("speaker", existing.speaker_names, candidate.speaker_names)
    partner_names = merged_names("partner", existing.partner_names, candidate.partner_names)
    if existing.entity_profiles:
        return (
            organizer_name,
            host_names,
            speaker_names,
            partner_names,
            existing.entity_profiles,
        )

    allowed = {
        "organizer": ({organizer_name.casefold()} if organizer_name is not None else set()),
        "host": {name.casefold() for name in host_names},
        "speaker": {name.casefold() for name in speaker_names},
        "partner": {name.casefold() for name in partner_names},
    }
    profiles = (
        candidate.entity_profiles
        if all(
            profile.name.casefold() in allowed[profile.role]
            for profile in candidate.entity_profiles
        )
        else ()
    )
    return organizer_name, host_names, speaker_names, partner_names, profiles


class PostgresCatalogRepository:
    """Implements CatalogRepository. Embeds at normalize time so any index rebuild is model-free."""

    def __init__(
        self,
        embedding: EmbeddingPort,
        *,
        session_scope: Callable[
            [], AbstractAsyncContextManager[AsyncSession]
        ] = system_session_scope,
    ) -> None:
        self._embedding = embedding
        self._session_scope = session_scope

    async def upsert_candidates(self, candidates: list[CandidateEvent]) -> list[CanonicalEvent]:
        if not candidates:
            return []
        vectors = await self.embed_candidates(candidates)
        async with self._session_scope() as s:  # catalog is tenant-neutral
            return await self.upsert_candidates_in_session(s, candidates, vectors)

    async def embed_candidates(self, candidates: list[CandidateEvent]) -> list[list[float]]:
        """Create vectors before P15b opens its final catalog transaction (NFR-8).

        The ordinary one-shot ingest and the staged promotion share the exact same deterministic or
        provisioned embedding boundary. Keeping model work outside the transaction avoids holding
        catalog row locks while a remote embedding implementation is slow (FR-3.8, ADR-001).
        """
        if not candidates:
            return []
        return await self._embedding.embed([f"{c.title}. {c.description}" for c in candidates])

    async def upsert_candidates_in_session(
        self,
        session: AsyncSession,
        candidates: list[CandidateEvent],
        vectors: list[list[float]],
    ) -> list[CanonicalEvent]:
        """Merge already-embedded candidates inside a caller-owned atomic transaction (P15b)."""
        if len(candidates) != len(vectors):
            raise ValueError("catalog candidates and embeddings must have the same length")
        await self._lock_merge_domains(session, candidates)
        await self._lock_candidate_canonicals(session, candidates)
        out: list[CanonicalEvent] = []
        for candidate, vector in zip(candidates, vectors, strict=True):
            out.append(await self._merge_or_insert(session, candidate, vector))
        return out

    async def _merge_or_insert(
        self, s: AsyncSession, candidate: CandidateEvent, vector: list[float]
    ) -> CanonicalEvent:
        # The publisher's stable identity is stronger than fuzzy event similarity. Serialize all
        # observations for one source identity before looking it up: without the batch lock,
        # concurrent first observations can both mint a canonical before the source-link uniqueness
        # constraint chooses one, leaving the losing canonical orphaned.
        exact_canonical_id = await self._find_source_identity(s, candidate)
        if exact_canonical_id is not None:
            existing = await self._load(s, exact_canonical_id)
            if len(existing.source_links) == 1:
                # A stable publisher identity is authoritative for its own exclusive canonical.
                # In particular, an organizer can move an occurrence outside the fuzzy dedup
                # window.  Keeping the old time made the current-observation browse silently drop
                # a successfully refreshed future event once its original time elapsed.
                await self._replace_exclusive_source_event(s, exact_canonical_id, candidate, vector)
                await self._attach_link(s, exact_canonical_id, candidate)
                await self._refresh_price(s, exact_canonical_id)
                return await self._load(s, exact_canonical_id)

            if dedup.is_duplicate(candidate, existing):
                # Multiple publishers still describe the same occurrence. Preserve the established
                # shared identity and only fill metadata gaps; one source must not rewrite fields
                # that belong to all retained links.
                await self._enrich_existing(s, existing, candidate, vector)
                await self._attach_link(s, exact_canonical_id, candidate)
                await self._refresh_price(s, exact_canonical_id)
                return await self._load(s, exact_canonical_id)

            # This publisher moved away from an occurrence that is still independently asserted by
            # another source. Split only its link, then let the ordinary fuzzy path attach it to an
            # already-known moved occurrence or mint a new canonical. The refresh committer updates
            # this source_key's catalog observation to the returned canonical in the same
            # transaction, while observations belonging to other sources remain on the old event.
            await self._detach_source_link(s, exact_canonical_id, candidate)
            await self._refresh_price(s, exact_canonical_id)

        city_norm = dedup.normalize_city(candidate.city)
        explicit_calendar_identity = _is_tech_week_candidate(candidate)
        lo = candidate.start_at - dedup.TIME_DELTA
        hi = candidate.start_at + dedup.TIME_DELTA
        rows = (
            await s.execute(
                text(
                    """
                    SELECT canonical_events.*, EXISTS (
                        SELECT 1 FROM event_source_links link
                        WHERE link.canonical_event_id=canonical_events.canonical_event_id
                          AND link.source='public_jsonld'
                          AND link.source_event_id LIKE 'tech-week:%'
                    ) AS has_tech_week_identity
                    FROM canonical_events
                    WHERE city_norm IS NOT DISTINCT FROM :city
                      AND start_at BETWEEN :lo AND :hi
                      AND (CAST(:explicit_identity_prefix AS text) IS NULL OR NOT EXISTS (
                        SELECT 1 FROM event_source_links link
                        WHERE link.canonical_event_id=canonical_events.canonical_event_id
                          AND link.source=:source
                          AND link.source_event_id LIKE CAST(:explicit_identity_prefix AS text) || '%'
                      ))
                    """
                ),
                {"city": city_norm, "lo": lo, "hi": hi, "source": candidate.source.value,
                 # Distinct official calendar IDs must remain discoverable even when their
                 # titles/times resemble each other and the provider supplies no coordinates.
                 "explicit_identity_prefix": (
                     "tech-week:" if explicit_calendar_identity else None
                 )},
            )
        ).all()
        for row in rows:
            existing = canonical_from_row(row, [])
            if (explicit_calendar_identity or row.has_tech_week_identity) and (
                existing.start_at != candidate.start_at
                or dedup.normalize_title(existing.title) != dedup.normalize_title(candidate.title)
                or not candidate.venue_name or not existing.venue_name
                or dedup.normalize_title(existing.venue_name) != dedup.normalize_title(candidate.venue_name)
            ):
                # Without public coordinates, approximate title/time similarity cannot prove
                # that an official conference entry is the same occurrence as another publisher.
                continue
            if dedup.is_duplicate(candidate, existing):
                # Serialize refreshes for one canonical event so every aggregate sees the prior
                # source-link price observation (FR-3.8/FR-5.10).
                await s.execute(
                    text(
                        """
                        SELECT canonical_event_id FROM canonical_events
                        WHERE canonical_event_id = :cid FOR UPDATE
                        """
                    ),
                    {"cid": existing.canonical_event_id},
                )
                await self._enrich_existing(s, existing, candidate, vector)
                await self._attach_link(s, existing.canonical_event_id, candidate)
                await self._refresh_price(s, existing.canonical_event_id)
                return await self._load(s, existing.canonical_event_id)

        canonical_id = uuid4()
        semantics = _semantic_projection(candidate)
        await s.execute(
            text(
                """
                INSERT INTO canonical_events
                    (canonical_event_id, title, start_at, end_at, venue_name, lat, lon,
                     city_norm, description, price_status, price_min_cents, price_max_cents,
                     price_currency, embedding, organizer_name,
                     host_names, speaker_names, partner_names, entity_profiles, attendance_count,
                     registration_status, topics, extraction_evidence)
                VALUES
                    (:cid, :title, :start, :end, :venue, :lat, :lon,
                     :city, :descr, :price_status, :price_min_cents, :price_max_cents,
                     :price_currency, (:emb)::vector, :organizer_name,
                     CAST(:host_names AS text[]), CAST(:speaker_names AS text[]),
                     CAST(:partner_names AS text[]), CAST(:entity_profiles AS jsonb),
                     :attendance_count,
                     :registration_status, CAST(:topics AS text[]),
                     CAST(:extraction_evidence AS jsonb))
                """
            ),
            {
                "cid": canonical_id,
                "title": candidate.title,
                "start": candidate.start_at,
                "end": candidate.end_at,
                "venue": candidate.venue_name,
                "lat": candidate.geo.lat if candidate.geo else None,
                "lon": candidate.geo.lon if candidate.geo else None,
                "city": city_norm,
                "descr": candidate.description,
                "price_status": candidate.price_status.value,
                "price_min_cents": candidate.price_min_cents,
                "price_max_cents": candidate.price_max_cents,
                "price_currency": candidate.price_currency,
                "emb": vector_literal(vector),
                "organizer_name": candidate.organizer_name,
                "host_names": list(candidate.host_names),
                "speaker_names": list(candidate.speaker_names),
                "partner_names": list(candidate.partner_names),
                "entity_profiles": json.dumps(
                    event_entity_profiles_payload(candidate.entity_profiles),
                    separators=(",", ":"),
                ),
                "attendance_count": candidate.attendance_count,
                "registration_status": candidate.registration_status.value,
                "topics": list(semantics.topics),
                "extraction_evidence": json.dumps(
                    extraction_evidence_payload(semantics.evidence), separators=(",", ":")
                ),
            },
        )
        linked_canonical_id = await self._attach_link(s, canonical_id, candidate)
        if linked_canonical_id != canonical_id:
            # The advisory lock makes this branch defensive under the normal READ COMMITTED
            # transaction. Keep the uniqueness constraint as the final authority in case an older
            # writer races this deployment or a caller supplies a different isolation level.
            await s.execute(
                text(
                    """
                    DELETE FROM canonical_events
                    WHERE canonical_event_id = :canonical_id
                      AND NOT EXISTS (
                          SELECT 1 FROM event_source_links
                          WHERE canonical_event_id = :canonical_id
                      )
                    """
                ),
                {"canonical_id": canonical_id},
            )
            await s.execute(
                text(
                    """
                    SELECT canonical_event_id FROM canonical_events
                    WHERE canonical_event_id = :canonical_id FOR UPDATE
                    """
                ),
                {"canonical_id": linked_canonical_id},
            )
            linked_existing = await self._load(s, linked_canonical_id)
            await self._enrich_existing(s, linked_existing, candidate, vector)
            await self._refresh_price(s, linked_canonical_id)
            return await self._load(s, linked_canonical_id)

        await self._refresh_price(s, canonical_id)
        return await self._load(s, canonical_id)

    @staticmethod
    async def _lock_merge_domains(s: AsyncSession, candidates: list[CandidateEvent]) -> None:
        """Serialize every exact or fuzzy domain a batch can merge, in one stable lock order.

        Exact source locks prevent concurrent replays from minting an orphan. Fuzzy window locks
        also cover distinct sources: every candidate acquires each fixed time bucket touched by its
        +/- dedup window, so two windows that can match the same canonical share at least one lock.
        Hashes are resolved and sorted before any lock is taken; sorting the input strings alone
        would still permit a rare hash-collision lock-order inversion.
        """
        identities = {
            json.dumps(
                ("source", candidate.source.value, candidate.source_event_id),
                separators=(",", ":"),
            )
            for candidate in candidates
        }
        for candidate in candidates:
            city_norm = dedup.normalize_city(candidate.city) or "_"
            first_bucket = (
                candidate.start_at - dedup.TIME_DELTA - _UNIX_EPOCH
            ) // _FUZZY_LOCK_BUCKET
            last_bucket = (
                candidate.start_at + dedup.TIME_DELTA - _UNIX_EPOCH
            ) // _FUZZY_LOCK_BUCKET
            identities.update(
                json.dumps(("fuzzy", city_norm, bucket), separators=(",", ":"))
                for bucket in range(first_bucket, last_bucket + 1)
            )

        lock_keys = (
            await s.execute(
                text(
                    """
                    SELECT DISTINCT hashtextextended(identity, 0) AS lock_key
                    FROM unnest(CAST(:identities AS text[])) AS domain(identity)
                    ORDER BY lock_key
                    """
                ),
                {"identities": sorted(identities)},
            )
        ).scalars()
        for lock_key in lock_keys:
            await s.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": lock_key},
            )

    @staticmethod
    async def _lock_candidate_canonicals(
        s: AsyncSession,
        candidates: list[CandidateEvent],
    ) -> None:
        """Pre-lock every existing row this batch may touch in canonical UUID order.

        Current-domain advisory locks prevent fuzzy first-ingest races, but an exact source event
        can be rescheduled or lose its city and therefore point back to a canonical outside its
        present fuzzy domain. Resolve both exact links and conservative fuzzy windows first, then
        acquire their row locks once in a global order so crossed batches cannot deadlock.
        """
        if not candidates:
            return
        identities = [
            {
                "source": candidate.source.value,
                "source_event_id": candidate.source_event_id,
            }
            for candidate in candidates
        ]
        windows = [
            {
                "city_norm": dedup.normalize_city(candidate.city),
                "window_start": (candidate.start_at - dedup.TIME_DELTA).isoformat(),
                "window_end": (candidate.start_at + dedup.TIME_DELTA).isoformat(),
            }
            for candidate in candidates
        ]
        await s.execute(
            text(
                """
                WITH identity_input AS (
                    SELECT source, source_event_id
                    FROM jsonb_to_recordset(CAST(:identities AS jsonb))
                         AS identity(source text, source_event_id text)
                ),
                window_input AS (
                    SELECT city_norm, window_start, window_end
                    FROM jsonb_to_recordset(CAST(:windows AS jsonb))
                         AS candidate_window(
                             city_norm text,
                             window_start timestamptz,
                             window_end timestamptz
                         )
                )
                SELECT canonical.canonical_event_id
                FROM canonical_events AS canonical
                WHERE canonical.canonical_event_id IN (
                    SELECT link.canonical_event_id
                    FROM event_source_links AS link
                    JOIN identity_input AS identity
                      ON identity.source = link.source
                     AND identity.source_event_id = link.source_event_id
                    UNION
                    SELECT fuzzy.canonical_event_id
                    FROM canonical_events AS fuzzy
                    JOIN window_input AS candidate_window
                      ON fuzzy.city_norm IS NOT DISTINCT FROM candidate_window.city_norm
                     AND fuzzy.start_at
                         BETWEEN candidate_window.window_start AND candidate_window.window_end
                )
                ORDER BY canonical.canonical_event_id
                FOR UPDATE OF canonical
                """
            ),
            {
                "identities": json.dumps(identities, separators=(",", ":")),
                "windows": json.dumps(windows, separators=(",", ":")),
            },
        )

    @staticmethod
    async def _find_source_identity(s: AsyncSession, candidate: CandidateEvent) -> UUID | None:
        """Resolve a stable source identity before consulting the fuzzy dedup window."""
        return (
            await s.execute(
                text(
                    """
                    SELECT canonical.canonical_event_id
                    FROM event_source_links AS link
                    JOIN canonical_events AS canonical
                      ON canonical.canonical_event_id = link.canonical_event_id
                    WHERE link.source = :source
                      AND link.source_event_id = :source_event_id
                    FOR UPDATE OF canonical
                    """
                ),
                {
                    "source": candidate.source.value,
                    "source_event_id": candidate.source_event_id,
                },
            )
        ).scalar_one_or_none()

    @staticmethod
    async def _enrich_existing(
        s: AsyncSession,
        existing: CanonicalEvent,
        candidate: CandidateEvent,
        vector: list[float],
    ) -> None:
        """Fill absent canonical metadata from a repeat observation without rewriting its identity (FR-3.8).

        A source may begin returning an optional public field after its initial crawl.  A duplicate
        observation is therefore allowed to fill only missing end/venue/geo fields and replace an
        empty or shorter description (with its corresponding embedding).  It cannot override a
        previously established identity or location with a conflicting value.
        """
        (
            organizer_name,
            host_names,
            speaker_names,
            partner_names,
            entity_profiles,
        ) = _conservative_shared_entities(existing, candidate)
        semantics = _semantic_projection(candidate)
        topics = tuple(dict.fromkeys((*existing.topics, *semantics.topics)))
        evidence = tuple(dict.fromkeys((*existing.extraction_evidence, *semantics.evidence)))
        await s.execute(
            text(
                """
                UPDATE canonical_events
                SET end_at = COALESCE(end_at, :end_at),
                    venue_name = COALESCE(NULLIF(venue_name, ''), :venue_name),
                    lat = COALESCE(lat, :lat),
                    lon = COALESCE(lon, :lon),
                    description = CASE
                        WHEN char_length(:description) > char_length(COALESCE(description, ''))
                            THEN :description
                        ELSE description
                    END,
                    organizer_name = :organizer_name,
                    host_names = CAST(:host_names AS text[]),
                    speaker_names = CAST(:speaker_names AS text[]),
                    partner_names = CAST(:partner_names AS text[]),
                    entity_profiles = CAST(:entity_profiles AS jsonb),
                    attendance_count = CASE
                        WHEN CAST(:attendance_count AS integer) IS NULL THEN attendance_count
                        ELSE GREATEST(
                            COALESCE(attendance_count, 0),
                            CAST(:attendance_count AS integer)
                        )
                    END,
                    registration_status = CASE
                        WHEN CAST(:registration_status AS text) <> 'unknown'
                            THEN CAST(:registration_status AS text)
                        ELSE registration_status
                    END,
                    topics = CAST(:topics AS text[]),
                    extraction_evidence = CAST(:extraction_evidence AS jsonb),
                    embedding = CASE
                        WHEN char_length(:description) > char_length(COALESCE(description, ''))
                            THEN (:embedding)::vector
                        ELSE embedding
                    END
                WHERE canonical_event_id = :canonical_id
                """
            ),
            {
                "canonical_id": existing.canonical_event_id,
                "end_at": candidate.end_at,
                "venue_name": candidate.venue_name,
                "lat": candidate.geo.lat if candidate.geo is not None else None,
                "lon": candidate.geo.lon if candidate.geo is not None else None,
                "description": candidate.description,
                "embedding": vector_literal(vector),
                "organizer_name": organizer_name,
                "host_names": list(host_names),
                "speaker_names": list(speaker_names),
                "partner_names": list(partner_names),
                "entity_profiles": json.dumps(
                    event_entity_profiles_payload(entity_profiles),
                    separators=(",", ":"),
                ),
                "attendance_count": candidate.attendance_count,
                "registration_status": candidate.registration_status.value,
                "topics": list(topics),
                "extraction_evidence": json.dumps(
                    extraction_evidence_payload(evidence), separators=(",", ":")
                ),
            },
        )

    @staticmethod
    async def _replace_exclusive_source_event(
        s: AsyncSession,
        canonical_id: UUID,
        candidate: CandidateEvent,
        vector: list[float],
    ) -> None:
        """Refresh every publisher-owned field when one source exclusively owns a canonical.

        Source identity, not fuzzy similarity, determines that this is the same publisher event.
        Optional values are deliberately replaced (including with NULL) so a removed or relocated
        venue cannot leave stale map coordinates behind. Lifecycle ``event_status`` and the
        normalizer/merge algorithm versions are separate concerns and remain unchanged.
        """
        semantics = _semantic_projection(candidate)
        await s.execute(
            text(
                """
                UPDATE canonical_events
                SET title = :title,
                    start_at = :start_at,
                    end_at = :end_at,
                    venue_name = :venue_name,
                    lat = :lat,
                    lon = :lon,
                    city_norm = :city_norm,
                    description = :description,
                    organizer_name = :organizer_name,
                    host_names = CAST(:host_names AS text[]),
                    speaker_names = CAST(:speaker_names AS text[]),
                    partner_names = CAST(:partner_names AS text[]),
                    entity_profiles = CAST(:entity_profiles AS jsonb),
                    attendance_count = :attendance_count,
                    registration_status = :registration_status,
                    topics = CAST(:topics AS text[]),
                    extraction_evidence = CAST(:extraction_evidence AS jsonb),
                    embedding = (:embedding)::vector
                WHERE canonical_event_id = :canonical_id
                """
            ),
            {
                "canonical_id": canonical_id,
                "title": candidate.title,
                "start_at": candidate.start_at,
                "end_at": candidate.end_at,
                "venue_name": candidate.venue_name,
                "lat": candidate.geo.lat if candidate.geo is not None else None,
                "lon": candidate.geo.lon if candidate.geo is not None else None,
                "city_norm": dedup.normalize_city(candidate.city),
                "description": candidate.description,
                "embedding": vector_literal(vector),
                "organizer_name": candidate.organizer_name,
                "host_names": list(candidate.host_names),
                "speaker_names": list(candidate.speaker_names),
                "partner_names": list(candidate.partner_names),
                "entity_profiles": json.dumps(
                    event_entity_profiles_payload(candidate.entity_profiles),
                    separators=(",", ":"),
                ),
                "attendance_count": candidate.attendance_count,
                "registration_status": candidate.registration_status.value,
                "topics": list(semantics.topics),
                "extraction_evidence": json.dumps(
                    extraction_evidence_payload(semantics.evidence), separators=(",", ":")
                ),
            },
        )

    @staticmethod
    async def _detach_source_link(
        s: AsyncSession,
        canonical_id: UUID,
        candidate: CandidateEvent,
    ) -> None:
        """Detach exactly one locked source identity before re-running fuzzy placement."""
        detached_canonical_id = (
            await s.execute(
                text(
                    """
                    DELETE FROM event_source_links
                    WHERE source = :source
                      AND source_event_id = :source_event_id
                      AND canonical_event_id = :canonical_id
                    RETURNING canonical_event_id
                    """
                ),
                {
                    "source": candidate.source.value,
                    "source_event_id": candidate.source_event_id,
                    "canonical_id": canonical_id,
                },
            )
        ).scalar_one_or_none()
        if detached_canonical_id != canonical_id:
            raise RuntimeError("locked catalog source identity could not be detached")

    @staticmethod
    async def _refresh_price(s: AsyncSession, canonical_id: UUID) -> None:
        """Recompute conservative canonical pricing from every retained source link."""
        rows = (
            await s.execute(
                text(
                    """
                    SELECT price_status, price_min_cents, price_max_cents, price_currency
                    FROM event_source_links
                    WHERE canonical_event_id = :cid
                    """
                ),
                {"cid": canonical_id},
            )
        ).all()
        price_status = aggregate_price_status(PriceStatus(row.price_status) for row in rows)
        price_range = (
            aggregate_price_range(
                (
                    row.price_min_cents,
                    row.price_max_cents,
                    row.price_currency,
                )
                for row in rows
            )
            if price_status is PriceStatus.PAID
            else (None, None, None)
        )
        await s.execute(
            text(
                """
                UPDATE canonical_events
                SET price_status = :price_status,
                    price_min_cents = :price_min_cents,
                    price_max_cents = :price_max_cents,
                    price_currency = :price_currency
                WHERE canonical_event_id = :cid
                """
            ),
            {
                "price_status": price_status.value,
                "price_min_cents": price_range[0],
                "price_max_cents": price_range[1],
                "price_currency": price_range[2],
                "cid": canonical_id,
            },
        )

    async def _attach_link(
        self, s: AsyncSession, canonical_id: UUID, candidate: CandidateEvent
    ) -> UUID:
        semantics = _semantic_projection(candidate)
        # Text is weaker than a provider price.  It may fill the only source observation for a
        # canonical, but it must not silently turn an additional deduplicated source into a free
        # assertion.  Zero links is a newly minted canonical; one matching link is an exact replay.
        # Any other shape is shared provenance and therefore remains unknown without structured
        # provider evidence.
        link_shape = (
            await s.execute(
                text(
                    """
                    SELECT count(*) AS link_count,
                           count(*) FILTER (
                               WHERE source = :source AND source_event_id = :source_event_id
                           ) AS matching_count
                    FROM event_source_links
                    WHERE canonical_event_id = :canonical_id
                    """
                ),
                {
                    "canonical_id": canonical_id,
                    "source": candidate.source.value,
                    "source_event_id": candidate.source_event_id,
                },
            )
        ).one()
        unambiguous_single_source = (
            int(link_shape.link_count) == 0
            or int(link_shape.link_count) == int(link_shape.matching_count) == 1
        )
        price_status = (
            semantics.inferred_price_status
            if candidate.price_status is PriceStatus.UNKNOWN
            and semantics.inferred_price_status is not None
            and unambiguous_single_source
            else candidate.price_status
        )
        linked_canonical_id = (
            await s.execute(
                text(
                    """
                    INSERT INTO event_source_links
                        (source, source_event_id, canonical_event_id, registration_url,
                         last_seen_at, price_status, price_min_cents, price_max_cents,
                         price_currency)
                    VALUES (:src, :sid, :cid, :url, now(), :price_status, :price_min_cents,
                            :price_max_cents, :price_currency)
                    ON CONFLICT (source, source_event_id)
                    DO UPDATE SET registration_url = EXCLUDED.registration_url,
                                  last_seen_at = now(),
                                  price_status = EXCLUDED.price_status,
                                  price_min_cents = EXCLUDED.price_min_cents,
                                  price_max_cents = EXCLUDED.price_max_cents,
                                  price_currency = EXCLUDED.price_currency
                    RETURNING canonical_event_id
                    """
                ),
                {
                    "src": candidate.source.value,
                    "sid": candidate.source_event_id,
                    "cid": canonical_id,
                    "url": candidate.registration_url,
                    "price_status": price_status.value,
                    "price_min_cents": (
                        candidate.price_min_cents if price_status is PriceStatus.PAID else None
                    ),
                    "price_max_cents": (
                        candidate.price_max_cents if price_status is PriceStatus.PAID else None
                    ),
                    "price_currency": (
                        candidate.price_currency if price_status is PriceStatus.PAID else None
                    ),
                },
            )
        ).scalar_one()
        return cast(UUID, linked_canonical_id)

    async def retrieve(
        self, constraints: RequestConstraints, intent_embedding: list[float] | None, limit: int
    ) -> list[CanonicalEvent]:
        where, params = self._constraint_sql(constraints)
        # Request-discovered events may have no registry observation. Once registry-owned,
        # require the same current/retained admission as catalog browse: rolloff is not cancel.
        where += """
            AND (
                NOT EXISTS (SELECT 1 FROM catalog_event_observations observation
                            WHERE observation.canonical_event_id = canonical_events.canonical_event_id)
                OR canonical_event_id IN (
                    SELECT admitted.canonical_event_id
                    FROM public.fn_list_retained_catalog_browse_observations_v2(
                        NULL, :admission_start, :admission_end
                    ) admitted
                )
            )
        """
        params["admission_start"] = constraints.time_window.start if constraints.time_window else None
        params["admission_end"] = constraints.time_window.end if constraints.time_window else None
        params["lim"] = limit
        if intent_embedding is not None:
            params["emb"] = vector_literal(intent_embedding)
            sql = f"""
                WITH filtered AS (SELECT * FROM canonical_events WHERE {where}),
                dense AS (
                    SELECT canonical_event_id,
                           row_number() OVER (ORDER BY embedding <=> (:emb)::vector, canonical_event_id) AS r
                    FROM filtered WHERE embedding IS NOT NULL LIMIT 200),
                sparse AS (
                    SELECT canonical_event_id,
                           row_number() OVER (ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', :q)) DESC, canonical_event_id) AS r
                    FROM filtered WHERE tsv @@ plainto_tsquery('english', :q) LIMIT 200),
                fused AS (
                    SELECT canonical_event_id, sum(1.0/({RRF_K} + r)) AS score
                    FROM (SELECT * FROM dense UNION ALL SELECT * FROM sparse) u
                    GROUP BY canonical_event_id)
                SELECT ce.* FROM canonical_events ce
                JOIN fused f USING (canonical_event_id)
                ORDER BY f.score DESC, ce.canonical_event_id LIMIT :lim
            """
            params["q"] = " ".join(constraints.categories) or ""
        else:
            sql = f"SELECT * FROM canonical_events WHERE {where} ORDER BY start_at ASC, canonical_event_id ASC LIMIT :lim"

        async with self._session_scope() as s:
            rows = (await s.execute(text(sql), params)).all()
            return [await self._load(s, row.canonical_event_id) for row in rows]

    async def get(self, canonical_event_id: UUID) -> CanonicalEvent | None:
        async with self._session_scope() as s:
            exists = (
                await s.execute(
                    text("SELECT 1 FROM canonical_events WHERE canonical_event_id = :cid"),
                    {"cid": canonical_event_id},
                )
            ).first()
            if not exists:
                return None
            return await self._load(s, canonical_event_id)

    async def get_browse_event(self, canonical_event_id: UUID) -> CatalogBrowseEvent | None:
        # Use the same admission/publication rules as browsing. An explicit window around this
        # occurrence also admits retained past observations, as the entity graph does.
        async with self._session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT event.*, observation.source_key, observation.source_label,
                               observation.publisher, observation.provider, observation.seed_url,
                               observation.observation_source, observation.source_event_id,
                               observation.registration_url, observation.last_seen_at,
                               observation.refresh_run_key
                        FROM public.canonical_events AS event
                        CROSS JOIN LATERAL public.fn_list_retained_catalog_browse_observations_v2(
                            '{}'::text[], event.start_at - interval '1 microsecond',
                            event.start_at + interval '1 microsecond'
                        ) AS observation
                        WHERE event.canonical_event_id = :canonical_event_id
                          AND observation.canonical_event_id = event.canonical_event_id
                        ORDER BY observation.source_key, observation.observation_source,
                                 observation.source_event_id
                        """
                    ),
                    {"canonical_event_id": canonical_event_id},
                )
            ).all()
        if not rows:
            return None
        return CatalogBrowseEvent(
            canonical_event=canonical_from_row(rows[0], []),
            sources=tuple(
                CatalogBrowseSource(
                    source_key=str(row.source_key),
                    label=str(row.source_label),
                    publisher=str(row.publisher),
                    provider=str(row.provider),
                    seed_url=str(row.seed_url),
                    source=Source(str(row.observation_source)),
                    source_event_id=str(row.source_event_id),
                    registration_url=str(row.registration_url),
                    last_seen_at=row.last_seen_at,
                    refresh_run_key=str(row.refresh_run_key),
                )
                for row in rows
            ),
        )

    async def suggest_names(
        self,
        *,
        query: str,
        source_keys: tuple[str, ...] = (),
        date_ranges: tuple[tuple[datetime, datetime], ...] = (),
        cities: tuple[str, ...] = (),
        location_scopes: tuple[str, ...] = (),
        price: str | None = None,
        price_max_cents: int | None = None,
        price_min_cents: int | None = None,
        topics: tuple[str, ...] = (),
        availability: str | None = None,
        limit: int = 8,
    ) -> list[CatalogNameSuggestion]:
        if (
            not MIN_CATALOG_NAME_QUERY_LENGTH <= len(query.strip()) <= _MAX_CATALOG_FILTER_LENGTH
            or not 1 <= limit <= MAX_CATALOG_NAME_SUGGESTIONS
        ):
            raise ValueError("catalog name suggestion query is invalid")
        normalized_topics = _validated_catalog_filter_inputs(
            query=query, source_keys=source_keys, city_filters=cities,
            location_scopes=location_scopes, price=price, price_max_cents=price_max_cents,
            price_min_cents=price_min_cents, topics=topics, availability=availability,
        )
        windows = _catalog_browse_ranges(
            starts_after=None, starts_before=None, date_ranges=date_ranges,
            max_window=(
                _MAX_SOURCE_CATALOG_ARCHIVE_WINDOW if source_keys else _MAX_CATALOG_BROWSE_WINDOW
            ),
        )
        async with self._session_scope() as session:
            rows = (await session.execute(
                text("""
                    SELECT * FROM public.fn_suggest_catalog_names_v1(
                        :query, CAST(:source_keys AS text[]),
                        CAST(:window_starts AS timestamptz[]), CAST(:window_ends AS timestamptz[]),
                        CAST(:cities AS text[]), CAST(:location_scopes AS text[]),
                        :price, :price_max_cents, :price_min_cents, CAST(:topics AS text[]),
                        :availability, :limit
                    )
                """),
                {
                    "query": query.strip(), "source_keys": list(dict.fromkeys(source_keys)),
                    "window_starts": [start for start, _ in windows] if windows else [None],
                    "window_ends": [end for _, end in windows] if windows else [None],
                    "cities": list(dict.fromkeys(cities)),
                    "location_scopes": list(dict.fromkeys(location_scopes)),
                    "price": price, "price_max_cents": price_max_cents,
                    "price_min_cents": price_min_cents, "topics": list(normalized_topics),
                    "availability": availability, "limit": limit,
                },
            )).all()
        return [CatalogNameSuggestion(
            name=row.name, kinds=tuple(cast(list[CatalogNameKind], row.kinds)),
            event_count=int(row.event_count),
        ) for row in rows]

    async def browse_current(
        self,
        *,
        source_keys: tuple[str, ...] = (),
        after: CatalogBrowseCursor | None,
        limit: int,
        starts_after: datetime | None = None,
        starts_before: datetime | None = None,
        date_ranges: tuple[tuple[datetime, datetime], ...] = (),
        query: str | None = None,
        city: str | None = None,
        cities: tuple[str, ...] | None = None,
        location_scopes: tuple[str, ...] = (),
        price: str | None = None,
        price_max_cents: int | None = None,
        price_min_cents: int | None = None,
        topics: tuple[str, ...] = (),
        availability: str | None = None,
        sort: CatalogBrowseSort = "soonest",
        include_providers: bool = True,
    ) -> tuple[list[CatalogBrowseEvent], list[CatalogBrowseProvider]]:
        """Browse admitted source observations in a stable chronological order.

        Omitted and current/future windows use only the latest successful source projection.  An
        explicit past portion may use a retained observation after the identity rolls off that
        projection.  Retention is one last-known observation and the canonical row is latest known
        state; this is not event version history or an as-of reconstruction.  The database
        capability owns those lane, admission, and fixture rules.
        """
        if limit < 1 or limit > _MAX_CATALOG_BROWSE_LIMIT:
            raise ValueError("catalog browse limit must be between 1 and 101")
        sort = _validated_catalog_sort(sort)
        max_window = (
            _MAX_SOURCE_CATALOG_ARCHIVE_WINDOW if source_keys else _MAX_CATALOG_BROWSE_WINDOW
        )
        requested_ranges = _catalog_browse_ranges(
            starts_after=starts_after,
            starts_before=starts_before,
            date_ranges=date_ranges,
            max_window=max_window,
        )
        windows: tuple[tuple[datetime | None, datetime | None], ...] = (
            requested_ranges if requested_ranges else ((None, None),)
        )
        city_filters = cities if cities is not None else ((city,) if city else ())
        normalized_topics = _validated_catalog_filter_inputs(
            query=query,
            source_keys=source_keys,
            city_filters=city_filters,
            location_scopes=location_scopes,
            price=price,
            price_max_cents=price_max_cents,
            price_min_cents=price_min_cents,
            topics=topics,
            availability=availability,
        )
        params = {
            "source_keys": list(dict.fromkeys(source_keys)),
            "query": query,
            "cities": list(dict.fromkeys(city_filters)),
            "location_scopes": list(dict.fromkeys(location_scopes)),
            "price": price,
            "price_max_cents": price_max_cents,
            "price_min_cents": price_min_cents,
            "topics": list(normalized_topics),
            "availability": availability,
            "sort": sort,
            "after_start": after.start_at if after is not None else None,
            "after_id": after.canonical_event_id if after is not None else None,
            "limit": limit,
        }
        rows: list[Any] = []
        provider_rows: list[Any] = []
        async with self._session_scope() as session:
            for window_start, window_end in windows:
                window_params = {
                    **params,
                    "window_start": window_start,
                    "window_end": window_end,
                }
                rows.extend(
                    (
                        await session.execute(
                            text(
                                """
                                SELECT *
                                FROM public.fn_browse_filtered_current_catalog_events_v10(
                                    CAST(:source_keys AS text[]), :window_start, :window_end,
                                    :query, CAST(:cities AS text[]),
                                    CAST(:location_scopes AS text[]), :price, :price_max_cents,
                                    :price_min_cents, CAST(:topics AS text[]), :availability,
                                    :sort, :after_start, :after_id, :limit
                                )
                                """
                            ),
                            window_params,
                        )
                    ).all()
                )
                # Ordinary facets are global so the client can replace its source list, including
                # admitted sources with zero events in this range. A long archive remains source-only.
                #
                # The rollup scans the whole admitted catalog, so it costs about as much as the page
                # it accompanies. A caller that never renders a source list skips it outright rather
                # than paying for a result it discards.
                if not include_providers:
                    continue
                # The rollup takes one source, and it is pinned only for the single-source
                # archive view. Any wider selection reads the whole admitted catalog, which is
                # what a facet list should offer anyway.
                provider_source_key = (
                    source_keys[0]
                    if len(source_keys) == 1
                    and window_start is not None
                    and window_end is not None
                    and window_end - window_start > _MAX_CATALOG_BROWSE_WINDOW
                    else None
                )
                provider_rows.extend(
                    (
                        await session.execute(
                            text(
                                """
                                SELECT *
                                FROM public.fn_list_current_catalog_providers_v3(
                                    :source_key, :window_start, :window_end
                                )
                                """
                            ),
                            {
                                "source_key": provider_source_key,
                                "window_start": window_start,
                                "window_end": window_end,
                            },
                        )
                    ).all()
                )

        grouped: dict[UUID, tuple[CanonicalEvent, list[CatalogBrowseSource]]] = {}
        for row in rows:
            canonical_id = cast(UUID, row.canonical_event_id)
            if canonical_id not in grouped:
                grouped[canonical_id] = (canonical_from_row(row, []), [])
            grouped[canonical_id][1].append(
                CatalogBrowseSource(
                    source_key=str(row.source_key),
                    label=str(row.source_label),
                    publisher=str(row.publisher),
                    provider=str(row.provider),
                    seed_url=str(row.seed_url),
                    source=Source(str(row.observation_source)),
                    source_event_id=str(row.source_event_id),
                    registration_url=str(row.registration_url),
                    last_seen_at=row.last_seen_at,
                    refresh_run_key=str(row.refresh_run_key),
                )
            )
        items = [
            CatalogBrowseEvent(canonical_event=event, sources=tuple(sources))
            for event, sources in grouped.values()
        ]
        items.sort(
            key=lambda item: (
                item.canonical_event.start_at,
                item.canonical_event.canonical_event_id,
            ),
            reverse=sort == "latest",
        )
        provider_groups: dict[str, CatalogBrowseProvider] = {}
        for row in provider_rows:
            row_source_key = str(row.source_key)
            existing = provider_groups.get(row_source_key)
            provider_groups[row_source_key] = CatalogBrowseProvider(
                source_key=row_source_key,
                display_name=str(row.source_label),
                publisher=str(row.publisher),
                provider=str(row.provider),
                seed_url=str(row.seed_url),
                event_count=int(row.event_count) + (existing.event_count if existing else 0),
            )
        return items[:limit], list(provider_groups.values())

    async def list_city_facets(self) -> list[CatalogBrowseCity]:
        """Return the bounded global city inventory used by the filter composer."""
        async with self._session_scope() as session:
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT *
                        FROM public.fn_list_current_catalog_city_facets_v1()
                        """
                    )
                )
            ).all()
        return [
            CatalogBrowseCity(city=str(row.city), event_count=int(row.event_count)) for row in rows
        ]

    async def list_topic_facets(
        self,
        *,
        source_keys: tuple[str, ...] = (),
        starts_after: datetime | None,
        starts_before: datetime | None,
        date_ranges: tuple[tuple[datetime, datetime], ...] = (),
        query: str | None,
        cities: tuple[str, ...],
        location_scopes: tuple[str, ...],
        price: str | None,
        price_max_cents: int | None,
        price_min_cents: int | None = None,
        availability: str | None = None,
    ) -> list[CatalogBrowseTopic]:
        """Count topics after all ordinary filters and before topic selection.

        The v2 capability aggregates one unbounded eligibility scan, so each requested window
        returns one row per topic.  Summing here still combines the rows from multiple date
        ranges into one compact product facet per topic.
        """
        if date_ranges and starts_after is not None:
            raise ValueError("catalog browse window must use one date encoding")
        windows: tuple[tuple[datetime | None, datetime | None], ...] = (
            date_ranges
            if date_ranges
            else (((starts_after, starts_before),) if starts_after is not None else ((None, None),))
        )
        params = {
            "source_keys": list(dict.fromkeys(source_keys)),
            "query": query,
            "cities": list(dict.fromkeys(cities)),
            "location_scopes": list(dict.fromkeys(location_scopes)),
            "price": price,
            "price_max_cents": price_max_cents,
            "price_min_cents": price_min_cents,
            "availability": availability,
        }
        rows: list[Any] = []
        async with self._session_scope() as session:
            for window_start, window_end in windows:
                rows.extend(
                    (
                        await session.execute(
                            text(
                                """
                                SELECT *
                                FROM public.fn_list_catalog_topic_facets_v4(
                                    CAST(:source_keys AS text[]), :window_start, :window_end,
                                    :query, CAST(:cities AS text[]),
                                    CAST(:location_scopes AS text[]), :price, :price_max_cents,
                                    :price_min_cents, :availability
                                )
                                """
                            ),
                            {
                                **params,
                                "window_start": window_start,
                                "window_end": window_end,
                            },
                        )
                    ).all()
                )
        counts: dict[str, int] = {}
        for row in rows:
            topic = str(row.topic)
            if topic in CATALOG_TOPICS:
                counts[topic] = counts.get(topic, 0) + int(row.event_count)
        return [
            CatalogBrowseTopic(topic=topic, label=label, event_count=counts.get(topic, 0))
            for topic, label in TOPIC_LABELS.items()
        ]

    async def list_day_facets(
        self,
        *,
        source_keys: tuple[str, ...] = (),
        starts_after: datetime | None,
        starts_before: datetime | None,
        date_ranges: tuple[tuple[datetime, datetime], ...] = (),
        query: str | None,
        cities: tuple[str, ...],
        location_scopes: tuple[str, ...],
        price: str | None,
        price_max_cents: int | None,
        price_min_cents: int | None = None,
        topics: tuple[str, ...] = (),
        availability: str | None = None,
        time_zone: str,
    ) -> list[CatalogBrowseDay]:
        """Bucket the filtered catalog into local calendar days without paging the events.

        The capability runs the same unbounded eligibility scan the topic facet runs and then
        groups by ``start_at AT TIME ZONE time_zone``, because a calendar day is a local
        wall-clock concept.  Unlike the topic facet this applies the topic selection, so the grid
        agrees with the agenda the same filters produce.  Counts from multiple requested windows
        are summed exactly as the topic facet sums them; the API merges overlapping and adjacent
        windows before calling, so day buckets do not overlap in practice.
        """
        if date_ranges and starts_after is not None:
            raise ValueError("catalog browse window must use one date encoding")
        _validated_catalog_time_zone(time_zone)
        city_filters = tuple(dict.fromkeys(cities))
        normalized_topics = _validated_catalog_filter_inputs(
            query=query,
            source_keys=source_keys,
            city_filters=city_filters,
            location_scopes=location_scopes,
            price=price,
            price_max_cents=price_max_cents,
            price_min_cents=price_min_cents,
            topics=topics,
            availability=availability,
        )
        windows: tuple[tuple[datetime | None, datetime | None], ...] = (
            date_ranges
            if date_ranges
            else (((starts_after, starts_before),) if starts_after is not None else ((None, None),))
        )
        params = {
            "source_keys": list(dict.fromkeys(source_keys)),
            "query": query,
            "cities": list(city_filters),
            "location_scopes": list(dict.fromkeys(location_scopes)),
            "price": price,
            "price_max_cents": price_max_cents,
            "price_min_cents": price_min_cents,
            "topics": list(normalized_topics),
            "availability": availability,
            "time_zone": time_zone,
        }
        rows: list[Any] = []
        async with self._session_scope() as session:
            for window_start, window_end in windows:
                rows.extend(
                    (
                        await session.execute(
                            text(
                                """
                                SELECT *
                                FROM public.fn_list_catalog_day_facets_v3(
                                    CAST(:source_keys AS text[]), :window_start, :window_end,
                                    :query, CAST(:cities AS text[]),
                                    CAST(:location_scopes AS text[]), :price, :price_max_cents,
                                    :price_min_cents, CAST(:topics AS text[]),
                                    :availability, :time_zone
                                )
                                """
                            ),
                            {
                                **params,
                                "window_start": window_start,
                                "window_end": window_end,
                            },
                        )
                    ).all()
                )
        day_totals: dict[date, int] = {}
        topic_counts: dict[date, dict[str, int]] = {}
        for row in rows:
            start_day = cast(date, row.start_day)
            if start_day not in day_totals:
                day_totals[start_day] = 0
                topic_counts[start_day] = {}
            day_totals[start_day] = max(day_totals[start_day], int(row.day_event_count))
            topic = str(row.topic)
            counts = topic_counts[start_day]
            counts[topic] = counts.get(topic, 0) + int(row.topic_event_count)
        days: list[CatalogBrowseDay] = []
        for start_day in sorted(day_totals):
            counts = topic_counts[start_day]
            event_count = max(day_totals[start_day], *counts.values()) if counts else 0
            if event_count < 1:
                continue
            days.append(
                CatalogBrowseDay(
                    start_day=start_day,
                    event_count=event_count,
                    topics=tuple(
                        CatalogBrowseDayTopic(
                            topic=topic,
                            label=_catalog_day_topic_label(topic),
                            event_count=count,
                        )
                        for topic, count in sorted(
                            counts.items(),
                            key=lambda entry: (
                                -entry[1],
                                _catalog_day_topic_label(entry[0]),
                            ),
                        )
                        if count > 0
                    ),
                )
            )
        return days

    @staticmethod
    def _constraint_sql(c: RequestConstraints) -> tuple[str, dict[str, object]]:
        # Archive identity and current eligibility are separate. Ordinary discovery excludes
        # ended/cancelled events before ranking; explicit windows can retrieve retained history.
        # Known end times allow ongoing events, with end-exclusive interval boundaries.
        clauses = [
            "event_status <> 'cancelled'",
            "(end_at IS NULL OR end_at > start_at)",
        ]
        if c.time_window is None:
            clauses.append("coalesce(end_at, start_at) > CURRENT_TIMESTAMP")
        params: dict[str, object] = {}
        if c.budget_free:
            clauses.append("price_status = :price_status")
            params["price_status"] = "free"
        if c.time_window is not None:
            clauses.append("coalesce(end_at, start_at) > :tw_lo AND start_at < :tw_hi")
            params["tw_lo"] = c.time_window.start
            params["tw_hi"] = c.time_window.end
        if c.geo is not None:
            # Bounding-box prefilter (approx; ~111 km per degree latitude).
            deg = c.geo.radius_km / 111.0
            clauses.append("(lat IS NULL OR lat BETWEEN :lat_lo AND :lat_hi)")
            clauses.append("(lon IS NULL OR lon BETWEEN :lon_lo AND :lon_hi)")
            params["lat_lo"] = c.geo.center.lat - deg
            params["lat_hi"] = c.geo.center.lat + deg
            params["lon_lo"] = c.geo.center.lon - deg
            params["lon_hi"] = c.geo.center.lon + deg
        return " AND ".join(clauses), params

    @staticmethod
    async def _load(s: AsyncSession, canonical_id: UUID) -> CanonicalEvent:
        row = (
            await s.execute(
                text("SELECT * FROM canonical_events WHERE canonical_event_id = :cid"),
                {"cid": canonical_id},
            )
        ).one()
        link_rows = (
            await s.execute(
                text(
                    "SELECT * FROM event_source_links WHERE canonical_event_id = :cid ORDER BY source"
                ),
                {"cid": canonical_id},
            )
        ).all()
        links: list[EventSourceLink] = [link_from_row(r) for r in link_rows]
        canonical = canonical_from_row(row, links)
        canonical.source_links = links
        return canonical
