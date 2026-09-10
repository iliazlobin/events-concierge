"""Approved-origin DataSF Our415 adapter (FR-3.1/FR-3.7/FR-10.3/FR-10.4).

DataSF's official Our415 dataset is an anonymous Socrata catalog of San Francisco activities.
Many rows describe a bounded weekly series rather than one event, so this adapter materializes
concrete local occurrences within the owner-approved 90-day discovery horizon.  It only performs
read-only GETs against the reviewed dataset endpoint and emits human-handoff candidates; it never
follows a handoff URL, signs in, registers, purchases, or mutates the source.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from datetime import date, datetime, time, timedelta
from time import monotonic
from urllib.parse import urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import httpx
from selectolax.parser import HTMLParser

from ...domain.catalog_sources import CatalogSource
from ...domain.catalog_window import collection_end_day, collection_reference_time
from ...domain.enums import CatalogSourceMode, PriceStatus, Source
from ...domain.events import CandidateEvent, GeoPoint
from ...infra.logging import get_logger

_log = get_logger("datasf.source")

_FETCH_TIMEOUT_S = 15.0
_DATASET_PATH = "/resource/8i3s-ih2a.json"
_PAGE_SIZE = 100
_HORIZON_DAYS = 90
_DEFAULT_MAX_OCCURRENCES = 10_000
_CONTROL_CHARACTER_LIMIT = 32
_MIN_POINT_COORDINATES = 2
_SF_TIME_ZONE = ZoneInfo("America/Los_Angeles")
_SPACE_BEFORE_PUNCTUATION = re.compile(r"\s+([,.;:!?])")
_FREE_ADMISSION = re.compile(
    r"^(?:free|complimentary|no\s+charge|\$?\s*0(?:\.0{1,2})?\s*(?:usd|dollars?)?)$",
    re.IGNORECASE,
)
_PAID_ADMISSION = re.compile(
    r"^(?:\$\s*)?[1-9]\d*(?:\.\d{1,2})?\s*(?:usd|dollars?)?$", re.IGNORECASE
)
_KNOWN_BARE_HANDOFF_HOSTS = frozenset({"sfrecpark.org", "www.sfrecpark.org"})
_WEEKDAYS = {
    "m": 0,
    "mon": 0,
    "monday": 0,
    "t": 1,
    "tue": 1,
    "tuesday": 1,
    "w": 2,
    "wed": 2,
    "wednesday": 2,
    "th": 3,
    "thu": 3,
    "thursday": 3,
    "f": 4,
    "fri": 4,
    "friday": 4,
    "sa": 5,
    "sat": 5,
    "saturday": 5,
    "s": 6,
    "sun": 6,
    "sunday": 6,
}
_SELECT_COLUMNS = (
    ":id",
    "id",
    "org_name",
    "event_name",
    "event_description",
    "event_start_date",
    "event_end_date",
    "days_of_week",
    "start_time",
    "end_time",
    "more_info",
    "fee",
    "admission_price",
    "site_location_name",
    "site_address",
    "latitude",
    "longitude",
    "point",
    "events_category",
    "age_group_eligibility_tags",
)


class DataSfOur415CatalogFetcher:
    """Fetch the reviewed DataSF activity dataset at a paced, bounded cadence (FR-10.3/10.4)."""

    def __init__(
        self,
        *,
        user_agent: str,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_occurrences: int = _DEFAULT_MAX_OCCURRENCES,
    ) -> None:
        if max_occurrences <= 0:
            raise ValueError("DataSF max_occurrences must be positive")
        self._user_agent = user_agent
        self._now = now or (lambda: datetime.now(_SF_TIME_ZONE))
        self._clock = clock or monotonic
        self._sleep = sleep or asyncio.sleep
        self._transport = transport
        self._max_occurrences = max_occurrences
        self._last_request_at: dict[str, float] = {}
        self._pacing_lock = asyncio.Lock()

    async def fetch(self, source: CatalogSource) -> list[CandidateEvent]:
        """Return concrete, future Our415 occurrences inside the approved 90-day horizon (FR-3.1)."""
        if source.mode is not CatalogSourceMode.DATASF_OUR415:
            raise ValueError(f"unsupported DataSF source mode: {source.mode.value}")
        if not source.handoff_only:
            raise ValueError("DataSF catalog sources must remain handoff-only")
        if not _is_supported_seed(source.seed_url):
            raise ValueError("DataSF source must use the reviewed Our415 Socrata endpoint")

        now = _as_sf_time(collection_reference_time(source, self._now()))
        records = await self._fetch_records(source, now.date())
        duplicate_publisher_ids = _duplicate_publisher_ids(records)
        candidates: list[CandidateEvent] = []
        for record in records:
            for candidate in _candidates_from_record(record, source, now, duplicate_publisher_ids):
                if len(candidates) >= self._max_occurrences:
                    raise DataSfFetchError(
                        f"DataSF source {source.source_key} exceeds its reviewed "
                        f"{self._max_occurrences}-occurrence cap"
                    )
                candidates.append(candidate)
        return _deduplicate(candidates)

    async def _fetch_records(
        self, source: CatalogSource, start_day: date
    ) -> list[dict[str, object]]:
        """Count then page one fixed query, failing before any partial catalog effect (NFR-8)."""
        end_exclusive = (
            collection_end_day(source, start_day, _SF_TIME_ZONE)
            if source.collection_window is not None
            else start_day + timedelta(days=source.collection_horizon_days)
        )
        where = _where_clause(start_day, end_exclusive)
        headers = {"User-Agent": self._user_agent}
        async with httpx.AsyncClient(
            headers=headers,
            follow_redirects=False,
            timeout=_FETCH_TIMEOUT_S,
            transport=self._transport,
        ) as client:
            count_url = _request_url(
                source.seed_url, (("$select", "count(*) as total"), ("$where", where))
            )
            count_response = await self._response_or_error(client, source, count_url, "count")
            expected_total = _count_from_response(count_response, source.source_key)
            if expected_total > source.page_limit * _PAGE_SIZE:
                raise DataSfFetchError(
                    f"DataSF source {source.source_key} exceeds its reviewed {source.page_limit}-page cap"
                )
            records: list[dict[str, object]] = []
            seen_socrata_ids: set[str] = set()
            for offset in range(0, expected_total, _PAGE_SIZE):
                page_url = _request_url(source.seed_url, _page_parameters(where, offset))
                page_response = await self._response_or_error(
                    client, source, page_url, f"offset {offset}"
                )
                page = _records_from_response(page_response, source.source_key, offset)
                expected_page_size = min(_PAGE_SIZE, expected_total - offset)
                if len(page) != expected_page_size:
                    raise DataSfFetchError(
                        f"DataSF page at offset {offset} was short for {source.source_key}"
                    )
                for record in page:
                    socrata_id = _required_socrata_id(record)
                    if socrata_id in seen_socrata_ids:
                        raise DataSfFetchError(
                            f"DataSF page sequence repeated a Socrata row for {source.source_key}"
                        )
                    seen_socrata_ids.add(socrata_id)
                records.extend(page)
        return records

    async def _response_or_error(
        self, client: httpx.AsyncClient, source: CatalogSource, url: str, label: str
    ) -> httpx.Response:
        """Request only the exact approved dataset endpoint; redirects are source failures (FR-10.3)."""
        if not _is_approved_endpoint_url(source, url):
            raise DataSfFetchError(f"DataSF {label} URL left the approved endpoint")
        try:
            await self._wait_for_host_slot(url, source.min_interval_ms)
            response = await client.get(url, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise DataSfFetchError(
                f"DataSF {label} request failed for {source.source_key}: {exc}"
            ) from exc
        if response.is_redirect or not _is_approved_endpoint_url(source, str(response.url)):
            raise DataSfFetchError(f"DataSF {label} response left the approved endpoint")
        try:
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise DataSfFetchError(
                f"DataSF {label} request failed for {source.source_key}: {exc}"
            ) from exc
        return response

    async def _wait_for_host_slot(self, url: str, min_interval_ms: int) -> None:
        """Apply the reviewed per-host human-cadence floor before every Socrata GET (FR-10.4)."""
        host = urlsplit(url).netloc.lower()
        if not host:
            raise ValueError("DataSF URL must include a host")
        interval_s = min_interval_ms / 1000.0
        async with self._pacing_lock:
            previous = self._last_request_at.get(host)
            if previous is not None:
                remaining = interval_s - (self._clock() - previous)
                if remaining > 0.0:
                    await self._sleep(remaining)
            self._last_request_at[host] = self._clock()


def _is_supported_seed(seed_url: str) -> bool:
    """Constrain this typed adapter to the reviewed anonymous Our415 resource (FR-10.3)."""
    parsed = urlsplit(seed_url)
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() == "data.sfgov.org"
        and parsed.path == _DATASET_PATH
        and not parsed.query
        and not parsed.fragment
    )


def _is_approved_endpoint_url(source: CatalogSource, url: str) -> bool:
    """Require both registry approval and the exact Socrata resource path (FR-10.3)."""
    return source.allows_url(url) and urlsplit(url).path == _DATASET_PATH


def _request_url(seed_url: str, parameters: tuple[tuple[str, str], ...]) -> str:
    """Build a fixed internal Socrata query; source records never supply query text (FR-10.3)."""
    parsed = urlsplit(seed_url)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(parameters),
            "",
        )
    )


def _where_clause(start_day: date, end_exclusive: date) -> str:
    """Return the sole reviewed interval-overlap predicate for the 90-day catalog window (FR-3.1)."""
    start = _socrata_day(start_day)
    end = _socrata_day(end_exclusive)
    return (
        f"event_start_date < '{end}' AND "
        f"(event_end_date >= '{start}' OR "
        f"(event_end_date IS NULL AND event_start_date >= '{start}'))"
    )


def _socrata_day(value: date) -> str:
    return f"{value.isoformat()}T00:00:00.000"


def _page_parameters(where: str, offset: int) -> tuple[tuple[str, str], ...]:
    if offset < 0:
        raise ValueError("DataSF page offset must not be negative")
    return (
        ("$select", ",".join(_SELECT_COLUMNS)),
        ("$where", where),
        ("$order", ":id ASC"),
        ("$limit", str(_PAGE_SIZE)),
        ("$offset", str(offset)),
    )


def _count_from_response(response: httpx.Response, source_key: str) -> int:
    """Read Socrata's single aggregate count object, rejecting malformed source JSON (NFR-8)."""
    try:
        payload = _as_object_list(response.json())
    except ValueError as exc:
        raise DataSfFetchError(f"DataSF count returned invalid JSON for {source_key}") from exc
    if payload is None or len(payload) != 1:
        raise DataSfFetchError(f"DataSF count returned an invalid shape for {source_key}")
    total = _nonnegative_int(payload[0].get("total"))
    if total is None:
        raise DataSfFetchError(f"DataSF count returned no usable total for {source_key}")
    return total


def _records_from_response(
    response: httpx.Response, source_key: str, offset: int
) -> list[dict[str, object]]:
    """Read one fully valid page of row objects rather than silently dropping malformed rows (NFR-8)."""
    try:
        payload = _as_object_list(response.json())
    except ValueError as exc:
        raise DataSfFetchError(
            f"DataSF page at offset {offset} returned invalid JSON for {source_key}"
        ) from exc
    if payload is None:
        raise DataSfFetchError(
            f"DataSF page at offset {offset} returned an invalid shape for {source_key}"
        )
    return payload


def _as_object_list(value: object) -> list[dict[str, object]] | None:
    if not isinstance(value, list):
        return None
    objects: list[dict[str, object]] = []
    for item in value:
        record = _as_object_dict(item)
        if record is None:
            return None
        objects.append(record)
    return objects


def _as_object_dict(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    return None


def _required_socrata_id(record: dict[str, object]) -> str:
    value = _identifier_text(record.get(":id"))
    if value is None:
        raise DataSfFetchError("DataSF row omitted the required Socrata :id discriminator")
    return value


def _duplicate_publisher_ids(records: list[dict[str, object]]) -> frozenset[str]:
    """Identify publisher IDs that need Socrata's stable row discriminator (FR-3.8)."""
    counts = Counter(
        publisher_id
        for record in records
        if (publisher_id := _identifier_text(record.get("id"))) is not None
    )
    return frozenset(identifier for identifier, count in counts.items() if count > 1)


def _candidates_from_record(
    record: dict[str, object],
    source: CatalogSource,
    now: datetime,
    duplicate_publisher_ids: frozenset[str],
) -> list[CandidateEvent]:
    """Normalize an official activity row into concrete, future local occurrences (FR-3.7/3.8)."""
    title = _text(record.get("event_name"))
    start_day = _date_value(record.get("event_start_date"))
    end_day = _date_value(record.get("event_end_date"))
    start_clock = _time_value(record.get("start_time"))
    registration_url = _handoff_url(record.get("more_info"))
    weekdays = _weekdays(record.get("days_of_week"))
    if (
        title is None
        or start_day is None
        or start_clock is None
        or registration_url is None
        or weekdays is None
    ):
        _log.warning("datasf_event_incomplete", source_key=source.source_key)
        return []
    occurrence_days = _occurrence_days(
        start_day, end_day, weekdays, now.date(),
        source.collection_window.horizon_days if source.collection_window is not None else source.collection_horizon_days,
        end_exclusive=collection_end_day(source, now.date() + timedelta(days=source.collection_horizon_days), _SF_TIME_ZONE),
    )
    if occurrence_days is None:
        _log.warning("datasf_event_schedule_invalid", source_key=source.source_key)
        return []
    external_id = _external_id(record, duplicate_publisher_ids)
    description = _text(record.get("event_description")) or ""
    venue_name = _first_text(record.get("site_location_name"), record.get("site_address"))
    end_clock = _time_value(record.get("end_time"))
    candidates: list[CandidateEvent] = []
    for occurrence_day in occurrence_days:
        start_at = datetime.combine(occurrence_day, start_clock, tzinfo=_SF_TIME_ZONE)
        if start_at < now or (source.collection_window is not None and start_at >= source.collection_window.end_at):
            continue
        end_at = _end_at(occurrence_day, start_clock, end_clock)
        candidates.append(
            CandidateEvent(
                source=Source.PUBLIC_JSONLD,
                source_event_id=(
                    f"datasf-our415:{source.source_key}:{external_id}:{start_at.isoformat()}"
                ),
                title=title,
                start_at=start_at,
                registration_url=registration_url,
                end_at=end_at,
                venue_name=venue_name,
                geo=_geo(record),
                description=description,
                price_status=_price_status(record),
                raw=dict(record),
            )
        )
    return candidates


def _occurrence_days(
    start_day: date, end_day: date | None, weekdays: frozenset[int], today: date,
    horizon_days: int = _HORIZON_DAYS, *, end_exclusive: date | None = None,
) -> list[date] | None:
    """Materialize only exact source-declared sessions in the approved 90-day window (FR-3.7)."""
    if not weekdays:
        return [start_day] if end_day == start_day else None
    if end_day is None or end_day < start_day:
        return None
    window_start = max(start_day, today)
    window_end = min(end_day, (end_exclusive - timedelta(days=1)) if end_exclusive is not None else today + timedelta(days=horizon_days - 1))
    if window_end < window_start:
        return []
    span_days = (window_end - window_start).days
    return [
        window_start + timedelta(days=offset)
        for offset in range(span_days + 1)
        if (window_start + timedelta(days=offset)).weekday() in weekdays
    ]


def _weekdays(value: object) -> frozenset[int] | None:
    """Parse the finite publisher weekday grammar; ambiguous or partial strings are rejected (FR-3.7)."""
    if value is None:
        return frozenset()
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized:
        return frozenset()
    result: set[int] = set()
    for raw_term in normalized.replace("/", ",").split(","):
        term = raw_term.strip()
        if not term:
            return None
        if term.count("-") > 1:
            return None
        if "-" not in term:
            weekday = _WEEKDAYS.get(term.casefold())
            if weekday is None:
                return None
            result.add(weekday)
            continue
        first_raw, last_raw = (part.strip().casefold() for part in term.split("-", maxsplit=1))
        first = _WEEKDAYS.get(first_raw)
        last = _WEEKDAYS.get(last_raw)
        if first is None or last is None or first > last:
            return None
        result.update(range(first, last + 1))
    return frozenset(result)


def _external_id(record: dict[str, object], duplicate_publisher_ids: frozenset[str]) -> str:
    publisher_id = _identifier_text(record.get("id"))
    if publisher_id is not None:
        if publisher_id not in duplicate_publisher_ids:
            return publisher_id
        return f"{publisher_id}:{_required_socrata_id(record)}"
    return f"derived:{_record_fingerprint(record)}"


def _record_fingerprint(record: dict[str, object]) -> str:
    material = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(material.encode()).hexdigest()[:32]


def _identifier_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return None


def _handoff_url(value: object) -> str | None:
    """Keep only a safe, source-provided HTTPS handoff; the adapter never requests it (FR-3.1)."""
    if (
        not isinstance(value, str)
        or not value.strip()
        or any(ord(char) < _CONTROL_CHARACTER_LIMIT for char in value)
    ):
        return None
    raw = value.strip()
    try:
        parsed = urlsplit(raw)
        if not parsed.scheme:
            parsed = urlsplit(f"https://{raw}")
            if (
                parsed.hostname is None
                or parsed.hostname.casefold() not in _KNOWN_BARE_HANDOFF_HOSTS
            ):
                return None
        if (
            parsed.scheme.casefold() != "https"
            or parsed.hostname is None
            or parsed.username is not None
            or parsed.password is not None
        ):
            return None
        _ = parsed.port
    except ValueError:
        return None
    return urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, ""))


def _price_status(record: dict[str, object]) -> PriceStatus:
    """Use explicit public fee truth and only unambiguous admission text (FR-3.7/FR-4.6)."""
    fee = record.get("fee")
    fee_status = (
        PriceStatus.FREE
        if fee is False
        else PriceStatus.PAID
        if fee is True
        else PriceStatus.UNKNOWN
    )
    admission_status = _admission_price_status(record.get("admission_price"))
    if fee_status is PriceStatus.UNKNOWN:
        return admission_status
    if admission_status is PriceStatus.UNKNOWN or admission_status is fee_status:
        return fee_status
    return PriceStatus.UNKNOWN


def _admission_price_status(value: object) -> PriceStatus:
    if not isinstance(value, str) or not value.strip():
        return PriceStatus.UNKNOWN
    text = " ".join(value.split())
    if _FREE_ADMISSION.fullmatch(text) is not None:
        return PriceStatus.FREE
    if _PAID_ADMISSION.fullmatch(text) is not None:
        return PriceStatus.PAID
    return PriceStatus.UNKNOWN


def _geo(record: dict[str, object]) -> GeoPoint | None:
    latitude = _coordinate(record.get("latitude"), -90.0, 90.0)
    longitude = _coordinate(record.get("longitude"), -180.0, 180.0)
    if latitude is not None and longitude is not None:
        return GeoPoint(latitude, longitude)
    point = _as_object_dict(record.get("point"))
    coordinates = point.get("coordinates") if point is not None else None
    if not isinstance(coordinates, list) or len(coordinates) < _MIN_POINT_COORDINATES:
        return None
    point_longitude = _coordinate(coordinates[0], -180.0, 180.0)
    point_latitude = _coordinate(coordinates[1], -90.0, 90.0)
    if point_latitude is None or point_longitude is None:
        return None
    return GeoPoint(point_latitude, point_longitude)


def _coordinate(value: object, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float, str)):
        return None
    try:
        coordinate = float(value)
    except ValueError:
        return None
    if not math.isfinite(coordinate) or not minimum <= coordinate <= maximum:
        return None
    return coordinate


def _date_value(value: object) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _time_value(value: object) -> time | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = time.fromisoformat(text)
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%I:%M %p").time()
        except ValueError:
            return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is None else None


def _end_at(occurrence_day: date, start_clock: time, end_clock: time | None) -> datetime | None:
    """Retain only a same-day, chronologically valid published end time (FR-3.7)."""
    if end_clock is None or end_clock <= start_clock:
        return None
    return datetime.combine(occurrence_day, end_clock, tzinfo=_SF_TIME_ZONE)


def _as_sf_time(value: datetime) -> datetime:
    return (
        value.astimezone(_SF_TIME_ZONE)
        if value.tzinfo is not None
        else value.replace(tzinfo=_SF_TIME_ZONE)
    )


def _text(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = " ".join(HTMLParser(value).text(separator=" ", strip=True).split())
    return _SPACE_BEFORE_PUNCTUATION.sub(r"\1", text) or None


def _first_text(*values: object) -> str | None:
    for value in values:
        text = _text(value)
        if text is not None:
            return text
    return None


def _deduplicate(candidates: list[CandidateEvent]) -> list[CandidateEvent]:
    """Keep one concrete publisher occurrence if a Socrata row is repeated (FR-3.8)."""
    unique: dict[str, CandidateEvent] = {}
    for candidate in candidates:
        unique.setdefault(candidate.source_event_id, candidate)
    return list(unique.values())


class DataSfFetchError(RuntimeError):
    """A source-wide failure that leaves the durable refresh retryable (NFR-8)."""
