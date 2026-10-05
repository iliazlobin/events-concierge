"""Check publication and freshness for currently admitted public-catalog sources.

Event totals count source-event memberships: an event present in two sources counts twice.
Low events-per-organizer depth in a city listing is diagnostic, not proof of missing events or
complete platform coverage. Empty feeds and retractions also require source-specific review.
The report carries source keys, counts and closed finding codes, without event content.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from sqlalchemy import text

from ..config import get_settings
from ..infra.db import dispose_engine, init_engine, system_session_scope

_MINUTES_PER_HOUR: Final = 60.0

#: City listings where low events-per-organizer depth merits comparison with host calendars.
_SHELF_MODES: Final = frozenset({"luma_discover_json", "meetup_city_jsonld"})

#: Threshold for suggesting a host-calendar comparison; this alone proves no missing events.
_MIN_DEPTH: Final = 1.5

#: Depth over a handful of events says nothing -- a four-event calendar with four hosts is small,
#: not shallow.  Only a source naming this many hosts has a depth worth reading.
_MIN_ORGANIZERS_FOR_DEPTH: Final = 10

#: A retraction rate above this means the source's live set is churning, not accumulating.
_MAX_RETRACTED_RATIO: Final = 0.05

#: How many refresh intervals a source may miss before its live set is treated as aged.
_STALE_INTERVAL_MULTIPLE: Final = 3.0

_ERROR_CODES: Final = frozenset({"source_dark", "page_cap_exceeded", "stale"})


class CatalogCoverageError(ValueError):
    """The measured rows cannot support a truthful coverage report."""


@dataclass(frozen=True, slots=True)
class SourceCoverage:
    """One reviewed source's admitted-catalog shape at measurement time."""

    source_key: str
    mode: str
    latest_run_status: str | None
    latest_run_error: str | None
    live_future_events: int
    retracted_future_events: int
    distinct_organizers: int
    hours_since_success: float | None
    refresh_interval_minutes: int

    def __post_init__(self) -> None:
        if not self.source_key:
            raise CatalogCoverageError("source_key is required")
        for name in (
            "live_future_events",
            "retracted_future_events",
            "distinct_organizers",
        ):
            if getattr(self, name) < 0:
                raise CatalogCoverageError(f"{name} cannot be negative")
        if self.refresh_interval_minutes <= 0:
            raise CatalogCoverageError("refresh_interval_minutes must be positive")

    @property
    def depth(self) -> float | None:
        """Live future events per distinct organizer, or None when no organizer is named.

        Most civic and library feeds name no organizer at all, so depth is undefined for them
        rather than zero -- reporting 0.0 there would invent a coverage failure out of a schema
        difference.
        """
        if self.distinct_organizers == 0:
            return None
        return self.live_future_events / self.distinct_organizers

    @property
    def retracted_ratio(self) -> float:
        total = self.live_future_events + self.retracted_future_events
        if total == 0:
            return 0.0
        return self.retracted_future_events / total


@dataclass(frozen=True, slots=True)
class CoverageFinding:
    """One closed, source-scoped observation about admitted coverage."""

    code: str
    source_key: str
    detail: str

    @property
    def is_error(self) -> bool:
        return self.code in _ERROR_CODES


@dataclass(frozen=True, slots=True)
class CoverageReport:
    """Fleet totals sum per-source event memberships, not globally unique events."""

    generated_at: str
    source_count: int
    live_future_events: int
    retracted_future_events: int
    shelf_sources: int
    depth_sources: int
    findings: tuple[CoverageFinding, ...]

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.is_error)

    def as_payload(self) -> dict[str, object]:
        return {
            **{key: value for key, value in asdict(self).items() if key != "findings"},
            "error_count": self.error_count,
            "findings": [asdict(finding) for finding in self.findings],
        }


def measure_catalog_coverage(
    rows: Sequence[SourceCoverage],
    *,
    now: datetime | None = None,
) -> CoverageReport:
    """Turn per-source counts into a report whose findings each name one actionable defect."""
    if not rows:
        raise CatalogCoverageError("at least one reviewed source is required")
    measured_at = (now or datetime.now(UTC)).astimezone(UTC)

    findings: list[CoverageFinding] = []
    for row in sorted(rows, key=lambda item: item.source_key):
        findings.extend(_findings_for(row))

    depths = [row.depth for row in rows]
    return CoverageReport(
        generated_at=measured_at.isoformat().replace("+00:00", "Z"),
        source_count=len(rows),
        live_future_events=sum(row.live_future_events for row in rows),
        retracted_future_events=sum(row.retracted_future_events for row in rows),
        # Counted on exactly the predicate `_findings_for` uses, so the headline number and the
        # findings under it can never disagree.
        shelf_sources=sum(1 for row in rows if _is_shelf(row)),
        depth_sources=sum(1 for depth in depths if depth is not None and depth >= _MIN_DEPTH),
        findings=tuple(findings),
    )


def _is_shelf(row: SourceCoverage) -> bool:
    """True when a shelf-mode source is sampling across hosts rather than carrying a programme."""
    depth = row.depth
    return (
        row.mode in _SHELF_MODES
        and depth is not None
        and depth < _MIN_DEPTH
        and row.distinct_organizers >= _MIN_ORGANIZERS_FOR_DEPTH
    )


def _findings_for(row: SourceCoverage) -> list[CoverageFinding]:
    findings: list[CoverageFinding] = []
    error = (row.latest_run_error or "").lower()
    if "page cap" in error or "reviewed page limit" in error:
        # Atomic publication retains the prior catalog when a pagination cap stops the refresh.
        findings.append(
            CoverageFinding(
                "page_cap_exceeded",
                row.source_key,
                "the reviewed pagination cap stopped publication; review source limits before "
                "changing the cap",
            )
        )
    elif row.latest_run_status != "succeeded":
        findings.append(
            CoverageFinding(
                "source_dark",
                row.source_key,
                f"latest run status is {row.latest_run_status or 'missing'}",
            )
        )

    if row.retracted_ratio > _MAX_RETRACTED_RATIO:
        findings.append(
            CoverageFinding(
                "coverage_retracted",
                row.source_key,
                f"{row.retracted_future_events} future events are absent from the newest "
                f"successful fetch and are no longer live",
            )
        )

    stale_after_hours = (
        row.refresh_interval_minutes / _MINUTES_PER_HOUR
    ) * _STALE_INTERVAL_MULTIPLE
    if row.hours_since_success is not None and row.hours_since_success > stale_after_hours:
        findings.append(
            CoverageFinding(
                "stale",
                row.source_key,
                f"last success was {row.hours_since_success:.1f}h ago against a "
                f"{row.refresh_interval_minutes}m cadence",
            )
        )

    if _is_shelf(row):
        findings.append(
            CoverageFinding(
                "shelf_only_coverage",
                row.source_key,
                f"names {row.distinct_organizers} organizers at {row.depth:.2f} events each; "
                "compare reviewed host calendars before drawing coverage conclusions",
            )
        )

    if row.latest_run_status == "succeeded" and row.live_future_events == 0:
        findings.append(
            CoverageFinding(
                "no_live_events",
                row.source_key,
                "the source has no retained ongoing or future events",
            )
        )
    return findings


# All current catalog modes share the public_jsonld/browser discovery policy. Mutation kill
# switches and paid/RSVP policy do not govern this read-only collection lane.
_QUERY: Final = """
    SELECT coverage.*
    FROM public.fn_report_catalog_source_coverage_v1() AS coverage
    JOIN public.catalog_sources AS source USING (source_key)
    JOIN public.source_policy AS policy ON policy.source = 'public_jsonld'
    WHERE source.enabled
      AND source.retired_at IS NULL
      AND source.handoff_only
      AND source.reviewed_at IS NOT NULL
      AND source.reviewed_at <= statement_timestamp()
      AND (source.review_expires_at IS NULL OR source.review_expires_at > statement_timestamp())
      AND NOT policy.quarantined
      AND policy.automation_allowed -> 'browser' = 'true'::jsonb
    ORDER BY coverage.source_key
"""


async def read_source_coverage() -> list[SourceCoverage]:
    """Read one row per review-current, admitted, non-fixture source from the live catalog.

    This CLI runs outside the composition root, so it owns its own read-only engine rather than
    borrowing a container it would otherwise have to build a whole application graph for.
    ``init_engine`` reassigns process globals without disposing what was there, so the engine is
    created once here and disposed on the way out.
    """
    init_engine(get_settings().database_url, pool_size=1)
    try:
        async with system_session_scope() as session:
            result = await session.execute(text(_QUERY))
            return [
                SourceCoverage(
                    source_key=row.source_key,
                    mode=row.mode,
                    latest_run_status=row.latest_run_status,
                    latest_run_error=row.latest_run_error,
                    live_future_events=int(row.live_future_events),
                    retracted_future_events=int(row.retracted_future_events),
                    distinct_organizers=int(row.distinct_organizers),
                    hours_since_success=(
                        None if row.hours_since_success is None else float(row.hours_since_success)
                    ),
                    refresh_interval_minutes=int(row.refresh_interval_minutes),
                )
                for row in result
            ]
    finally:
        await dispose_engine()


def main(argv: Sequence[str] | None = None) -> int:
    """Fail on missing/failed publication, pagination caps or freshness past three cadences."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--max-warnings",
        type=int,
        default=None,
        help="fail when non-error findings exceed this count",
    )
    parser.add_argument("--out", type=str, default=None, help="write the JSON report here")
    arguments = parser.parse_args(argv)

    rows = asyncio.run(read_source_coverage())
    report = measure_catalog_coverage(rows)
    payload = json.dumps(report.as_payload(), indent=2, sort_keys=True)
    if arguments.out:
        Path(arguments.out).write_text(payload + "\n", encoding="utf-8")
    print(payload)

    warnings = len(report.findings) - report.error_count
    if report.error_count:
        return 1
    if arguments.max_warnings is not None and warnings > arguments.max_warnings:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint
    sys.exit(main())
