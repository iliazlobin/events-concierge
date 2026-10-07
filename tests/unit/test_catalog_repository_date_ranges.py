"""Repository callers share the API's absolute-time, union-of-windows contract."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from events_concierge.adapters.postgres.catalog import _catalog_browse_ranges


def test_repository_merges_overlaps_before_counting_the_window_budget() -> None:
    lower = datetime(2026, 1, 1, tzinfo=UTC)
    upper = lower + timedelta(days=300)
    assert _catalog_browse_ranges(
        starts_after=None,
        starts_before=None,
        date_ranges=((lower, upper), (lower, upper)),
        max_window=timedelta(days=370),
    ) == ((lower, upper),)


def test_repository_orders_fall_back_windows_by_absolute_time() -> None:
    zone = ZoneInfo("America/Los_Angeles")
    lower = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    upper = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)
    assert _catalog_browse_ranges(
        starts_after=lower,
        starts_before=upper,
        date_ranges=(),
        max_window=timedelta(days=1),
    ) == ((datetime(2026, 11, 1, 8, 30, tzinfo=UTC), datetime(2026, 11, 1, 9, 15, tzinfo=UTC)),)


@pytest.mark.parametrize(
    ("lower", "upper"),
    [
        (datetime(2026, 1, 1), datetime(2026, 1, 2, tzinfo=UTC)),
        (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
        (datetime(2026, 1, 2, tzinfo=UTC), datetime(2026, 1, 1, tzinfo=UTC)),
    ],
)
def test_repository_rejects_invalid_windows(lower: datetime, upper: datetime) -> None:
    with pytest.raises(ValueError, match="window is invalid"):
        _catalog_browse_ranges(
            starts_after=lower,
            starts_before=upper,
            date_ranges=(),
            max_window=timedelta(days=370),
        )
