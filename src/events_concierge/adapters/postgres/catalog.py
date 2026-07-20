"""Tenant-neutral catalog repository: dedup-on-ingest into canonical events (retaining all source
links) and hybrid retrieval (dense pgvector ANN + sparse tsvector) fused by Reciprocal Rank Fusion."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from ...domain import dedup
from ...domain.enums import PriceStatus
from ...domain.events import (
    CandidateEvent,
    CanonicalEvent,
    EventSourceLink,
    aggregate_price_status,
)
from ...domain.request import RequestConstraints
from ...infra.db import system_session_scope
from ...ports.ranking import EmbeddingPort
from ._mapping import canonical_from_row, link_from_row, vector_literal

RRF_K = 60
_FUZZY_LOCK_BUCKET = dedup.TIME_DELTA
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class PostgresCatalogRepository:
    """Implements CatalogRepository. Embeds at normalize time so any index rebuild is model-free."""

    def __init__(self, embedding: EmbeddingPort) -> None:
        self._embedding = embedding

    async def upsert_candidates(self, candidates: list[CandidateEvent]) -> list[CanonicalEvent]:
        if not candidates:
            return []
        vectors = await self.embed_candidates(candidates)
        async with system_session_scope() as s:  # catalog is tenant-neutral
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
            await self._enrich_existing(s, exact_canonical_id, candidate, vector)
            await self._attach_link(s, exact_canonical_id, candidate)
            await self._refresh_price_status(s, exact_canonical_id)
            return await self._load(s, exact_canonical_id)

        city_norm = dedup.normalize_city(candidate.city)
        lo = candidate.start_at - dedup.TIME_DELTA
        hi = candidate.start_at + dedup.TIME_DELTA
        rows = (
            await s.execute(
                text(
                    """
                    SELECT * FROM canonical_events
                    WHERE city_norm IS NOT DISTINCT FROM :city
                      AND start_at BETWEEN :lo AND :hi
                    """
                ),
                {"city": city_norm, "lo": lo, "hi": hi},
            )
        ).all()
        for row in rows:
            existing = canonical_from_row(row, [])
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
                await self._enrich_existing(s, existing.canonical_event_id, candidate, vector)
                await self._attach_link(s, existing.canonical_event_id, candidate)
                await self._refresh_price_status(s, existing.canonical_event_id)
                return await self._load(s, existing.canonical_event_id)

        canonical_id = uuid4()
        await s.execute(
            text(
                """
                INSERT INTO canonical_events
                    (canonical_event_id, title, start_at, end_at, venue_name, lat, lon,
                     city_norm, description, price_status, embedding)
                VALUES
                    (:cid, :title, :start, :end, :venue, :lat, :lon,
                     :city, :descr, :price_status, (:emb)::vector)
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
                "emb": vector_literal(vector),
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
            await self._enrich_existing(s, linked_canonical_id, candidate, vector)
            await self._refresh_price_status(s, linked_canonical_id)
            return await self._load(s, linked_canonical_id)

        await self._refresh_price_status(s, canonical_id)
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
        canonical_id: UUID,
        candidate: CandidateEvent,
        vector: list[float],
    ) -> None:
        """Fill absent canonical metadata from a repeat observation without rewriting its identity (FR-3.8).

        A source may begin returning an optional public field after its initial crawl.  A duplicate
        observation is therefore allowed to fill only missing end/venue/geo fields and replace an
        empty or shorter description (with its corresponding embedding).  It cannot override a
        previously established identity or location with a conflicting value.
        """
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
                    embedding = CASE
                        WHEN char_length(:description) > char_length(COALESCE(description, ''))
                            THEN (:embedding)::vector
                        ELSE embedding
                    END
                WHERE canonical_event_id = :canonical_id
                """
            ),
            {
                "canonical_id": canonical_id,
                "end_at": candidate.end_at,
                "venue_name": candidate.venue_name,
                "lat": candidate.geo.lat if candidate.geo is not None else None,
                "lon": candidate.geo.lon if candidate.geo is not None else None,
                "description": candidate.description,
                "embedding": vector_literal(vector),
            },
        )

    @staticmethod
    async def _refresh_price_status(s: AsyncSession, canonical_id: UUID) -> None:
        """Recompute canonical price from every retained source link (FR-3.7/FR-3.8/FR-5.10)."""
        rows = (
            await s.execute(
                text(
                    """
                    SELECT price_status FROM event_source_links
                    WHERE canonical_event_id = :cid
                    """
                ),
                {"cid": canonical_id},
            )
        ).all()
        price_status = aggregate_price_status(PriceStatus(row.price_status) for row in rows)
        await s.execute(
            text(
                """
                UPDATE canonical_events
                SET price_status = :price_status
                WHERE canonical_event_id = :cid
                """
            ),
            {"price_status": price_status.value, "cid": canonical_id},
        )

    async def _attach_link(
        self, s: AsyncSession, canonical_id: UUID, candidate: CandidateEvent
    ) -> UUID:
        linked_canonical_id = (
            await s.execute(
                text(
                    """
                    INSERT INTO event_source_links
                        (source, source_event_id, canonical_event_id, registration_url,
                         last_seen_at, price_status)
                    VALUES (:src, :sid, :cid, :url, now(), :price_status)
                    ON CONFLICT (source, source_event_id)
                    DO UPDATE SET registration_url = EXCLUDED.registration_url,
                                  last_seen_at = now(),
                                  price_status = EXCLUDED.price_status
                    RETURNING canonical_event_id
                    """
                ),
                {
                    "src": candidate.source.value,
                    "sid": candidate.source_event_id,
                    "cid": canonical_id,
                    "url": candidate.registration_url,
                    "price_status": candidate.price_status.value,
                },
            )
        ).scalar_one()
        return cast(UUID, linked_canonical_id)

    async def retrieve(
        self, constraints: RequestConstraints, intent_embedding: list[float] | None, limit: int
    ) -> list[CanonicalEvent]:
        where, params = self._constraint_sql(constraints)
        params["lim"] = limit
        if intent_embedding is not None:
            params["emb"] = vector_literal(intent_embedding)
            sql = f"""
                WITH filtered AS (SELECT * FROM canonical_events WHERE {where}),
                dense AS (
                    SELECT canonical_event_id,
                           row_number() OVER (ORDER BY embedding <=> (:emb)::vector) AS r
                    FROM filtered WHERE embedding IS NOT NULL LIMIT 200),
                sparse AS (
                    SELECT canonical_event_id,
                           row_number() OVER (ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', :q)) DESC) AS r
                    FROM filtered WHERE tsv @@ plainto_tsquery('english', :q) LIMIT 200),
                fused AS (
                    SELECT canonical_event_id, sum(1.0/({RRF_K} + r)) AS score
                    FROM (SELECT * FROM dense UNION ALL SELECT * FROM sparse) u
                    GROUP BY canonical_event_id)
                SELECT ce.* FROM canonical_events ce
                JOIN fused f USING (canonical_event_id)
                ORDER BY f.score DESC LIMIT :lim
            """
            params["q"] = " ".join(constraints.categories) or ""
        else:
            sql = f"SELECT * FROM canonical_events WHERE {where} ORDER BY start_at ASC LIMIT :lim"

        async with system_session_scope() as s:
            rows = (await s.execute(text(sql), params)).all()
            return [await self._load(s, row.canonical_event_id) for row in rows]

    async def get(self, canonical_event_id: UUID) -> CanonicalEvent | None:
        async with system_session_scope() as s:
            exists = (
                await s.execute(
                    text("SELECT 1 FROM canonical_events WHERE canonical_event_id = :cid"),
                    {"cid": canonical_event_id},
                )
            ).first()
            if not exists:
                return None
            return await self._load(s, canonical_event_id)

    @staticmethod
    def _constraint_sql(c: RequestConstraints) -> tuple[str, dict[str, object]]:
        # Source adapters reject elapsed rows when they ingest them, but the durable catalog keeps
        # those rows after wall time advances. Reapply the invariant at read time so an omitted
        # request window can never surface—or attempt registration for—an elapsed event.
        clauses = ["start_at >= CURRENT_TIMESTAMP"]
        params: dict[str, object] = {}
        if c.budget_free:
            clauses.append("price_status = :price_status")
            params["price_status"] = "free"
        if c.time_window is not None:
            clauses.append("start_at BETWEEN :tw_lo AND :tw_hi")
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
