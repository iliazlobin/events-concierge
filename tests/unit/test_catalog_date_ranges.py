"""Bounded additive date-window parsing for catalog browse."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from events_concierge.api.app import (
    _catalog_filter_scope,
    _normalize_catalog_date_ranges,
)


def test_date_only_ranges_are_inclusive_and_overlaps_are_merged() -> None:
    ranges = _normalize_catalog_date_ranges(
        [
            "2026-08-01..2026-08-03",
            "2026-08-03..2026-08-05",
            "2026-08-10..2026-08-10",
        ],
        starts_after=None,
        starts_before=None,
        source_keys=(),
    )

    assert ranges == (
        (
            datetime(2026, 8, 1, tzinfo=UTC),
            datetime(2026, 8, 6, tzinfo=UTC),
        ),
        (
            datetime(2026, 8, 10, tzinfo=UTC),
            datetime(2026, 8, 11, tzinfo=UTC),
        ),
    )


def test_datetime_ranges_require_timezone_and_preserve_exclusive_end() -> None:
    ranges = _normalize_catalog_date_ranges(
        ["2026-08-01T07:00:00-07:00..2026-08-02T07:00:00-07:00"],
        starts_after=None,
        starts_before=None,
        source_keys=(),
    )

    assert ranges == (
        (
            datetime(2026, 8, 1, 14, tzinfo=UTC),
            datetime(2026, 8, 2, 14, tzinfo=UTC),
        ),
    )
    with pytest.raises(ValueError, match="timezone"):
        _normalize_catalog_date_ranges(
            ["2026-08-01T07:00:00..2026-08-02T07:00:00"],
            starts_after=None,
            starts_before=None,
            source_keys=(),
        )


def test_range_order_and_overlap_do_not_change_cursor_filter_scope() -> None:
    first = _normalize_catalog_date_ranges(
        ["2026-08-10..2026-08-10", "2026-08-01..2026-08-03"],
        starts_after=None,
        starts_before=None,
        source_keys=(),
    )
    second = _normalize_catalog_date_ranges(
        ["2026-08-01..2026-08-02", "2026-08-03..2026-08-03", "2026-08-10..2026-08-10"],
        starts_after=None,
        starts_before=None,
        source_keys=(),
    )

    def scope(ranges: tuple[tuple[datetime, datetime], ...]) -> str:
        return _catalog_filter_scope(
            source_keys=(),
            starts_after=None,
            starts_before=None,
            date_ranges=ranges,
            query=None,
            cities=(),
            location_scopes=(),
            price=None,
            price_max_cents=None,
            price_min_cents=None,
            topics=(),
        )

    assert first == second
    assert scope(first) == scope(second)


@pytest.mark.parametrize(
    "values",
    [
        ["2026-08-02..2026-08-01"],
        ["2026-08-01/2026-08-02"],
        [f"2026-08-{day:02d}..2026-08-{day:02d}" for day in range(1, 10)],
    ],
)
def test_invalid_or_unbounded_range_sets_are_rejected(values: list[str]) -> None:
    with pytest.raises(ValueError):
        _normalize_catalog_date_ranges(
            values,
            starts_after=None,
            starts_before=None,
            source_keys=(),
        )
