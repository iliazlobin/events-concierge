"""Coverage-shape measurement: the guard that would have caught a shelf posing as a catalog."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from events_concierge.quality import catalog_coverage
from events_concierge.quality.catalog_coverage import (
    CatalogCoverageError,
    CoverageReport,
    SourceCoverage,
    measure_catalog_coverage,
)

_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _row(
    source_key: str = "luma-thecommons",
    *,
    mode: str = "luma_calendar_json",
    status: str | None = "succeeded",
    error: str | None = None,
    live: int = 87,
    retracted: int = 0,
    organizers: int = 5,
    hours_since_success: float | None = 1.0,
    refresh_interval_minutes: int = 360,
) -> SourceCoverage:
    return SourceCoverage(
        source_key=source_key,
        mode=mode,
        latest_run_status=status,
        latest_run_error=error,
        live_future_events=live,
        retracted_future_events=retracted,
        distinct_organizers=organizers,
        hours_since_success=hours_since_success,
        refresh_interval_minutes=refresh_interval_minutes,
    )


def _codes(report: CoverageReport, source_key: str) -> set[str]:
    return {f.code for f in report.findings if f.source_key == source_key}


def test_a_healthy_host_calendar_produces_no_findings() -> None:
    report = measure_catalog_coverage([_row()], now=_NOW)

    assert report.findings == ()
    assert report.depth_sources == 1
    assert report.live_future_events == 87


def test_a_city_listing_with_low_depth_suggests_comparison_without_claiming_missing_events() -> (
    None
):
    report = measure_catalog_coverage(
        [_row("luma-sf", mode="luma_discover_json", live=81, organizers=75)],
        now=_NOW,
    )

    assert _codes(report, "luma-sf") == {"shelf_only_coverage"}
    assert report.shelf_sources == 1
    assert report.depth_sources == 0
    finding = report.findings[0]
    assert "75 organizers at 1.08 events each" in finding.detail
    assert "compare reviewed host calendars" in finding.detail
    assert "under-represented" not in finding.detail
    assert finding.is_error is False


def test_a_small_shelf_is_not_mistaken_for_a_shallow_one() -> None:
    """Depth over a handful of hosts says nothing: four events with four hosts is small, not shallow.

    This is asserted on a *shelf* mode deliberately. On a host-calendar mode the mode gate already
    excludes the row, so the organizer floor would never be reached and the test would prove
    nothing about it.
    """
    report = measure_catalog_coverage(
        [_row("luma-sf", mode="luma_discover_json", live=4, organizers=4)],
        now=_NOW,
    )

    assert report.findings == ()
    assert report.shelf_sources == 0


def test_a_host_calendar_is_never_reported_as_a_shelf_however_flat_its_depth() -> None:
    """A member calendar where every event has its own host is complete, not shallow."""
    report = measure_catalog_coverage(
        [_row("luma-cursorcommunity", live=59, organizers=57)],
        now=_NOW,
    )

    assert report.findings == ()
    assert report.shelf_sources == 0


def test_depth_is_undefined_rather_than_zero_when_no_organizer_is_named() -> None:
    """Most civic and library feeds name no organizer; that is a schema difference, not a defect."""
    row = _row("san-jose-public-library-events", mode="bibliocommons_rss", live=3103, organizers=0)

    assert row.depth is None
    assert measure_catalog_coverage([row], now=_NOW).findings == ()


@pytest.mark.parametrize(
    "error",
    [
        "BiblioCommons source exceeds its reviewed 50-page cap",
        "Tech Week calendar exceeds its reviewed page limit",
    ],
)
def test_a_page_cap_failure_is_reported_apart_from_an_ordinary_failure(error: str) -> None:
    """Pagination caps stop fresh publication and deserve a specific finding."""
    report = measure_catalog_coverage(
        [
            _row(
                "sccld-all-physical-branches-events",
                mode="bibliocommons_rss",
                status="failed",
                error=error,
                organizers=0,
            )
        ],
        now=_NOW,
    )

    codes = _codes(report, "sccld-all-physical-branches-events")
    assert codes == {"page_cap_exceeded"}
    assert report.error_count == 1
    assert "raise page_limit" not in report.findings[0].detail


def test_a_response_byte_limit_failure_is_not_a_pagination_cap() -> None:
    report = measure_catalog_coverage(
        [_row(status="failed", error="Tech Week response exceeds its byte limit")],
        now=_NOW,
    )
    assert _codes(report, "luma-thecommons") == {"source_dark"}


def test_a_failed_run_without_a_cap_message_is_reported_as_dark() -> None:
    report = measure_catalog_coverage(
        [_row("luma-nyc", mode="luma_discover_json", status="failed", live=90, organizers=83)],
        now=_NOW,
    )

    assert _codes(report, "luma-nyc") == {"source_dark", "shelf_only_coverage"}
    assert report.error_count == 1


def test_retracted_future_events_are_reported_because_nothing_else_reports_them() -> None:
    """The write path never deletes; a future event absent from the newest fetch just stops
    being live, silently."""
    report = measure_catalog_coverage(
        [_row("berkeley-events", mode="livewhale_json", live=192, retracted=130, organizers=0)],
        now=_NOW,
    )

    assert _codes(report, "berkeley-events") == {"coverage_retracted"}
    assert report.retracted_future_events == 130


def test_a_retraction_within_ordinary_churn_is_not_reported() -> None:
    report = measure_catalog_coverage([_row(live=100, retracted=2)], now=_NOW)

    assert report.findings == ()


def test_a_source_that_has_missed_three_cadences_is_stale() -> None:
    report = measure_catalog_coverage(
        [_row(hours_since_success=318.4, refresh_interval_minutes=360, organizers=0)],
        now=_NOW,
    )

    assert "stale" in _codes(report, "luma-thecommons")
    assert report.error_count == 1


@pytest.mark.parametrize(("hours", "error_count"), [(18.0, 0), (18.01, 1)])
def test_freshness_fails_only_after_three_cadences(hours: float, error_count: int) -> None:
    report = measure_catalog_coverage([_row(hours_since_success=hours)], now=_NOW)
    assert report.error_count == error_count


@pytest.mark.parametrize(
    ("row", "expected_status"),
    [
        (_row(hours_since_success=19), 1),
        (_row(live=0, organizers=0), 0),
        (_row(live=100, retracted=10), 0),
        (_row("luma-sf", mode="luma_discover_json", live=81, organizers=75), 0),
    ],
)
def test_default_cli_fails_on_staleness_but_keeps_shape_findings_diagnostic(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    row: SourceCoverage,
    expected_status: int,
) -> None:
    async def read() -> list[SourceCoverage]:
        return [row]

    monkeypatch.setattr(catalog_coverage, "read_source_coverage", read)
    assert catalog_coverage.main([]) == expected_status
    payload = json.loads(capsys.readouterr().out)
    assert payload["error_count"] == expected_status


def test_a_source_one_cadence_behind_is_not_stale() -> None:
    report = measure_catalog_coverage(
        [_row(hours_since_success=7.0, refresh_interval_minutes=360)],
        now=_NOW,
    )

    assert report.findings == ()


def test_a_successful_run_that_admitted_nothing_is_reported() -> None:
    report = measure_catalog_coverage(
        [_row("sunnyvale-legistar-meetings", mode="sunnyvale_legistar", live=0, organizers=0)],
        now=_NOW,
    )

    assert _codes(report, "sunnyvale-legistar-meetings") == {"no_live_events"}


def test_the_report_payload_is_counts_and_closed_codes_only() -> None:
    """The report is archived as build evidence, so it must never carry event content."""
    payload = measure_catalog_coverage(
        [_row("luma-sf", mode="luma_discover_json", live=81, organizers=75)],
        now=_NOW,
    ).as_payload()

    assert payload["generated_at"] == "2026-08-26T12:00:00Z"
    assert set(payload) == {
        "generated_at",
        "source_count",
        "live_future_events",
        "retracted_future_events",
        "shelf_sources",
        "depth_sources",
        "error_count",
        "findings",
    }
    findings = payload["findings"]
    assert isinstance(findings, list)
    assert set(findings[0]) == {"code", "source_key", "detail"}


def test_an_empty_fleet_is_refused_rather_than_reported_as_healthy() -> None:
    with pytest.raises(CatalogCoverageError, match="at least one reviewed source"):
        measure_catalog_coverage([], now=_NOW)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_key", ""),
        ("live_future_events", -1),
        ("retracted_future_events", -1),
        ("distinct_organizers", -1),
        ("refresh_interval_minutes", 0),
    ],
)
def test_an_impossible_measurement_is_refused(field: str, value: object) -> None:
    kwargs: dict[str, object] = {
        "source_key": "luma-sf",
        "mode": "luma_discover_json",
        "latest_run_status": "succeeded",
        "latest_run_error": None,
        "live_future_events": 1,
        "retracted_future_events": 0,
        "distinct_organizers": 1,
        "hours_since_success": 1.0,
        "refresh_interval_minutes": 360,
        field: value,
    }
    with pytest.raises(CatalogCoverageError):
        SourceCoverage(**kwargs)  # type: ignore[arg-type]


def test_the_headline_shelf_count_never_contradicts_the_findings() -> None:
    """The counter and the finding must be the same predicate, or the report argues with itself."""
    report = measure_catalog_coverage(
        [
            _row("luma-sf", mode="luma_discover_json", live=81, organizers=75),
            _row("luma-nyc", mode="luma_discover_json", live=90, organizers=83),
            # Flat depth, but a host calendar: not a shelf, and must not be counted as one.
            _row("luma-cursorcommunity", live=59, organizers=57),
            # Shelf mode, but too few hosts to read a depth from.
            _row("meetup-sf", mode="meetup_city_jsonld", live=4, organizers=4),
        ],
        now=_NOW,
    )

    shelf_findings = [f for f in report.findings if f.code == "shelf_only_coverage"]
    assert report.shelf_sources == len(shelf_findings) == 2
    assert {f.source_key for f in shelf_findings} == {"luma-sf", "luma-nyc"}
