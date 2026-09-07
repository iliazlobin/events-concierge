"""PostgreSQL read projections for the authenticated consumer UI.

Every tenant table is read through ``tenant_session_scope`` and every query repeats the tenant
predicate. Catalog joins are tenant-neutral, but can only be reached through an already-visible
lifecycle or handoff row. No workflow identity, token digest, metadata, or notification payload is
returned from this adapter.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.engine import Row

from ...domain.consumer import (
    ConsumerIdentity,
    ConsumerRegistrationSummary,
    ConsumerRequestOutcome,
    ConsumerRequestSummary,
    ConsumerTaskSummary,
)
from ...domain.enums import (
    EventStatus,
    HandoffReason,
    HandoffState,
    Lane,
    LifecycleState,
    PriceStatus,
    Source,
)
from ...infra.db import tenant_session_scope
from ...ports.consumer import (
    ConsumerRequestOutcomeConflictError,
    ConsumerRequestOutcomeLinkStatus,
)

_MAX_PAGE_FETCH = 51
_TIME_WINDOW_PARTS = 2


class PostgresConsumerReadRepository:
    """Serve small, bounded pages for one authenticated tenant."""

    async def get_identity(self, tenant_id: UUID) -> ConsumerIdentity | None:
        async with tenant_session_scope(tenant_id) as session:
            row = (
                await session.execute(
                    text(
                        """SELECT tenant.tenant_id, tenant.notify_email,
                                  COALESCE(profile.revision, 0) AS preference_revision,
                                  COALESCE(profile.explicit_affinities, '{}'::jsonb)
                                      AS explicit_affinities
                           FROM public.tenants AS tenant
                           LEFT JOIN public.tenant_ranking_profiles AS profile
                             ON profile.tenant_id = tenant.tenant_id
                           WHERE tenant.tenant_id = :tenant_id"""
                    ),
                    {"tenant_id": tenant_id},
                )
            ).first()
        if row is None:
            return None
        affinities = _json_object(row.explicit_affinities, "explicit affinities")
        interests = tuple(sorted(key for key, value in affinities.items() if _positive(value)))
        return ConsumerIdentity(
            tenant_id=row.tenant_id,
            notify_email=str(row.notify_email),
            preference_revision=int(row.preference_revision),
            interests=interests,
        )

    async def list_requests(
        self, tenant_id: UUID, *, offset: int, limit: int
    ) -> tuple[ConsumerRequestSummary, ...]:
        _validate_page(offset, limit)
        async with tenant_session_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        """SELECT request.request_id,
                                  request.raw_text,
                                  request.state,
                                  request.created_at,
                                  request.constraints,
                                  outcome.lifecycle_id AS outcome_lifecycle_id,
                                  outcome.canonical_event_id AS outcome_canonical_event_id,
                                  outcome.title AS outcome_title,
                                  outcome.start_at AS outcome_start_at,
                                  outcome.state AS outcome_state,
                                  outcome.lane AS outcome_lane,
                                  outcome.registration_source AS outcome_source,
                                  outcome.updated_at AS outcome_updated_at
                           FROM public.event_requests AS request
                           LEFT JOIN LATERAL (
                               SELECT lifecycle.lifecycle_id,
                                      lifecycle.canonical_event_id,
                                      event.title,
                                      event.start_at,
                                      lifecycle.state,
                                      lifecycle.lane,
                                      lifecycle.registration_source,
                                      lifecycle.updated_at
                               FROM public.request_outcome_links AS link
                               JOIN public.lifecycle AS lifecycle
                                 ON lifecycle.tenant_id = link.tenant_id
                                AND lifecycle.lifecycle_id = link.lifecycle_id
                               JOIN public.canonical_events AS event
                                 ON event.canonical_event_id = lifecycle.canonical_event_id
                               WHERE link.tenant_id = :tenant_id
                                 AND link.request_id = request.request_id
                                 AND lifecycle.tenant_id = :tenant_id
                                 -- A malformed/premature link must never expose an internal
                                 -- candidate attempt as the selected consumer outcome.
                                 AND lifecycle.state IN (
                                     'handoff', 'registered', 'scheduled', 'reconciled',
                                     'withdrawing', 'completed', 'cancelled', 'expired'
                                 )
                               LIMIT 1
                           ) AS outcome ON true
                           WHERE request.tenant_id = :tenant_id
                           ORDER BY request.created_at DESC, request.request_id DESC
                           OFFSET :offset LIMIT :limit"""
                    ),
                    {"tenant_id": tenant_id, "offset": offset, "limit": limit},
                )
            ).all()
        return tuple(_request_from_row(row) for row in rows)

    async def link_request_outcome(
        self,
        tenant_id: UUID,
        request_id: UUID,
        lifecycle_id: UUID,
    ) -> ConsumerRequestOutcomeLinkStatus:
        """Append one selected lifecycle link, converging exact retries and rejecting rebinding."""
        async with tenant_session_scope(tenant_id) as session:
            inserted = (
                await session.execute(
                    text(
                        """INSERT INTO public.request_outcome_links
                               (tenant_id, request_id, lifecycle_id)
                           SELECT request.tenant_id, request.request_id, lifecycle.lifecycle_id
                           FROM public.event_requests AS request
                           JOIN public.lifecycle AS lifecycle
                             ON lifecycle.tenant_id = request.tenant_id
                           WHERE request.tenant_id = :tenant_id
                             AND request.request_id = :request_id
                             AND lifecycle.tenant_id = :tenant_id
                             AND lifecycle.lifecycle_id = :lifecycle_id
                             AND lifecycle.state IN (
                                 'handoff', 'registered', 'scheduled', 'reconciled',
                                 'withdrawing', 'completed', 'cancelled', 'expired'
                             )
                           ON CONFLICT (tenant_id, request_id) DO NOTHING
                           RETURNING lifecycle_id"""
                    ),
                    {
                        "tenant_id": tenant_id,
                        "request_id": request_id,
                        "lifecycle_id": lifecycle_id,
                    },
                )
            ).first()
            if inserted is not None:
                return ConsumerRequestOutcomeLinkStatus.LINKED
            existing = (
                await session.execute(
                    text(
                        """SELECT lifecycle_id
                           FROM public.request_outcome_links
                           WHERE tenant_id = :tenant_id
                             AND request_id = :request_id"""
                    ),
                    {"tenant_id": tenant_id, "request_id": request_id},
                )
            ).first()
        if existing is None:
            raise ValueError(
                "request outcome requires a tenant-visible request and non-failed lifecycle"
            )
        if existing.lifecycle_id == lifecycle_id:
            return ConsumerRequestOutcomeLinkStatus.REPLAYED
        raise ConsumerRequestOutcomeConflictError(
            "request is already linked to a different selected lifecycle"
        )

    async def list_registrations(
        self, tenant_id: UUID, *, offset: int, limit: int
    ) -> tuple[ConsumerRegistrationSummary, ...]:
        _validate_page(offset, limit)
        async with tenant_session_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        """SELECT lifecycle.canonical_event_id,
                                  event.title,
                                  event.start_at,
                                  event.end_at,
                                  event.venue_name,
                                  event.city_norm,
                                  left(event.description, 800) AS description,
                                  event.price_status,
                                  event.event_status,
                                  lifecycle.state,
                                  lifecycle.lane,
                                  lifecycle.registration_source,
                                  lifecycle.conflict_warning,
                                  lifecycle.updated_at,
                                  source_link.source AS link_source,
                                  source_link.registration_url
                           FROM public.lifecycle AS lifecycle
                           JOIN public.canonical_events AS event
                             ON event.canonical_event_id = lifecycle.canonical_event_id
                           LEFT JOIN LATERAL (
                               SELECT link.source, link.registration_url
                               FROM public.event_source_links AS link
                               WHERE link.canonical_event_id = event.canonical_event_id
                                 AND (
                                     lifecycle.registration_source IS NULL
                                     OR link.source = lifecycle.registration_source
                                 )
                               -- Pre-registration lifecycles use the newest known destination.
                               -- Once a provider is selected, never pair its label with another
                               -- provider's URL; a missing selected link intentionally yields NULL.
                               ORDER BY link.last_seen_at DESC NULLS LAST,
                                        link.source
                               LIMIT 1
                           ) AS source_link ON true
                           WHERE lifecycle.tenant_id = :tenant_id
                             AND lifecycle.state <> 'failed_no_candidate'
                             AND event.start_at >= clock_timestamp() - INTERVAL '1 day'
                           ORDER BY event.start_at, lifecycle.updated_at DESC,
                                    lifecycle.canonical_event_id
                           OFFSET :offset LIMIT :limit"""
                    ),
                    {"tenant_id": tenant_id, "offset": offset, "limit": limit},
                )
            ).all()
        return tuple(_registration_from_row(row) for row in rows)

    async def list_tasks(
        self,
        tenant_id: UUID,
        *,
        actionable_only: bool,
        offset: int,
        limit: int,
    ) -> tuple[ConsumerTaskSummary, ...]:
        _validate_page(offset, limit)
        async with tenant_session_scope(tenant_id) as session:
            rows = (
                await session.execute(
                    text(
                        """SELECT task.task_id,
                                  task.canonical_event_id,
                                  task.event_summary,
                                  task.reason,
                                  task.state,
                                  task.deep_link,
                                  task.ttl_expires_at,
                                  task.created_at,
                                  event.title,
                                  event.start_at,
                                  event.venue_name,
                                  event.city_norm
                           FROM public.handoff_tasks AS task
                           JOIN public.canonical_events AS event
                             ON event.canonical_event_id = task.canonical_event_id
                           WHERE task.tenant_id = :tenant_id
                             AND (
                                 NOT :actionable_only
                                 OR (
                                     task.state IN ('open', 'notified')
                                     AND task.ttl_expires_at > clock_timestamp()
                                 )
                             )
                           ORDER BY CASE task.state
                                      WHEN 'open' THEN 0
                                      WHEN 'notified' THEN 1
                                      ELSE 2
                                    END,
                                    task.ttl_expires_at,
                                    task.task_id
                           OFFSET :offset LIMIT :limit"""
                    ),
                    {
                        "tenant_id": tenant_id,
                        "actionable_only": actionable_only,
                        "offset": offset,
                        "limit": limit,
                    },
                )
            ).all()
        return tuple(_task_from_row(row) for row in rows)


def _validate_page(offset: int, limit: int) -> None:
    if offset < 0 or not 1 <= limit <= _MAX_PAGE_FETCH:
        raise ValueError("consumer read page is outside its bounded range")


def _json_object(value: object, label: str) -> dict[str, object]:
    decoded = json.loads(value) if isinstance(value, str) else value
    if not isinstance(decoded, dict):
        raise RuntimeError(f"consumer {label} row is malformed")
    return cast("dict[str, object]", decoded)


def _positive(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and float(value) > 0


def _request_from_row(row: Row[Any]) -> ConsumerRequestSummary:
    constraints = _json_object(row.constraints, "request constraints")
    categories_value = constraints.get("categories", [])
    categories = (
        tuple(str(value) for value in categories_value if isinstance(value, str))
        if isinstance(categories_value, list)
        else ()
    )
    window_start, window_end = _request_window(constraints.get("time_window"))
    return ConsumerRequestSummary(
        request_id=row.request_id,
        text=str(row.raw_text),
        state=str(row.state),
        created_at=row.created_at,
        categories=categories,
        budget_free=constraints.get("budget_free") is True,
        window_start=window_start,
        window_end=window_end,
        outcome=_request_outcome_from_row(row),
    )


def _request_outcome_from_row(row: Row[Any]) -> ConsumerRequestOutcome | None:
    if row.outcome_lifecycle_id is None:
        return None
    return ConsumerRequestOutcome(
        canonical_event_id=row.outcome_canonical_event_id,
        title=str(row.outcome_title),
        start_at=row.outcome_start_at,
        state=LifecycleState(row.outcome_state),
        lane=Lane(row.outcome_lane) if row.outcome_lane else None,
        source=Source(row.outcome_source) if row.outcome_source else None,
        updated_at=row.outcome_updated_at,
    )


def _request_window(value: object) -> tuple[datetime | None, datetime | None]:
    """Decode both the current tuple-shaped persistence and the older named projection."""
    if value is None:
        return None, None
    if isinstance(value, list) and len(value) == _TIME_WINDOW_PARTS:
        start_value, end_value = value
    elif isinstance(value, dict) and set(value) == {"start", "end"}:
        start_value, end_value = value["start"], value["end"]
    else:
        raise RuntimeError("consumer request time window is malformed")
    start = _optional_datetime(start_value)
    end = _optional_datetime(end_value)
    if start is None or end is None or end < start:
        raise RuntimeError("consumer request time window is malformed")
    return start, end


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise RuntimeError("consumer request time window is malformed")
    try:
        return datetime.fromisoformat(value)
    except ValueError as error:
        raise RuntimeError("consumer request time window is malformed") from error


def _registration_from_row(row: Row[Any]) -> ConsumerRegistrationSummary:
    raw_source = row.registration_source or row.link_source
    return ConsumerRegistrationSummary(
        canonical_event_id=row.canonical_event_id,
        title=str(row.title),
        start_at=row.start_at,
        end_at=row.end_at,
        venue_name=row.venue_name,
        city=row.city_norm,
        description=str(row.description),
        price_status=PriceStatus(row.price_status),
        event_status=EventStatus(row.event_status),
        state=LifecycleState(row.state),
        lane=Lane(row.lane) if row.lane else None,
        source=Source(raw_source) if raw_source else None,
        conflict_warning=bool(row.conflict_warning),
        registration_url=row.registration_url,
        updated_at=row.updated_at,
    )


def _task_from_row(row: Row[Any]) -> ConsumerTaskSummary:
    return ConsumerTaskSummary(
        task_id=str(row.task_id),
        canonical_event_id=row.canonical_event_id,
        event_summary=str(row.event_summary),
        title=str(row.title),
        start_at=row.start_at,
        venue_name=row.venue_name,
        city=row.city_norm,
        reason=HandoffReason(row.reason),
        state=HandoffState(row.state),
        deep_link=str(row.deep_link),
        expires_at=row.ttl_expires_at,
        created_at=row.created_at,
    )
