"""Bounded read-only run selection, shared across HTTP and application boundaries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TypedDict

_MAX_QUERY = 160
_MAX_RUN_KEY = 256
_MIN_VISIBLE = 32
_DELETE = 127


class RunQueryOptions(TypedDict, total=False):
    query: str
    sort_by: str
    sort_direction: str
    started_after: datetime
    started_before: datetime
    stage: str
    stage_outcome: str


def run_query_options(
    *,
    query: str | None = None,
    sort_by: str = "started",
    sort_direction: str = "desc",
    started_after: datetime | None = None,
    started_before: datetime | None = None,
    stage: str | None = None,
    stage_outcome: str | None = None,
) -> RunQueryOptions:
    if query is not None and (
        len(query) > _MAX_QUERY or any(ord(c) < _MIN_VISIBLE or ord(c) == _DELETE for c in query)
    ):
        raise ValueError("invalid run search")
    if sort_by not in {
        "source",
        "status",
        "started",
        "duration",
        "attempts",
        "output",
        "stage_duration",
    }:
        raise ValueError("invalid run sort")
    if sort_direction not in {"asc", "desc"}:
        raise ValueError("invalid run sort direction")
    if stage is not None and stage not in {
        "admission",
        "collect",
        "extract_enrich",
        "normalize_dedupe",
        "catalog_publish",
    }:
        raise ValueError("invalid run stage")
    if stage_outcome not in {None, "failed"} or (
        (stage_outcome or sort_by == "stage_duration") and stage is None
    ):
        raise ValueError("stage is required for stage outcome and duration")
    _validate_interval(started_after, started_before)
    options: RunQueryOptions = {}
    if query is not None:
        options["query"] = query.strip()
    if sort_by != "started":
        options["sort_by"] = sort_by
    if sort_direction != "desc":
        options["sort_direction"] = sort_direction
    if started_after is not None and started_before is not None:
        options["started_after"], options["started_before"] = started_after, started_before
    if stage is not None:
        options["stage"] = stage
    if stage_outcome is not None:
        options["stage_outcome"] = stage_outcome
    return options


def validate_run_key(run_key: str) -> None:
    if not 1 <= len(run_key) <= _MAX_RUN_KEY or any(
        ord(c) < _MIN_VISIBLE or ord(c) == _DELETE for c in run_key
    ):
        raise ValueError("invalid run key")


def _validate_interval(started_after: datetime | None, started_before: datetime | None) -> None:
    if (started_after is None) != (started_before is None):
        raise ValueError("run interval requires both bounds")
    if started_after is not None and started_before is not None:
        if any(value.utcoffset() is None for value in (started_after, started_before)):
            raise ValueError("run interval must be timezone-aware")
        if (
            not timedelta(0)
            < started_before.astimezone(UTC) - started_after.astimezone(UTC)
            <= timedelta(days=90)
        ):
            raise ValueError("run interval must span at most 90 days")
