"""Small, dependency-free Prometheus surface with bounded, non-sensitive labels.

Only route templates are recorded.  Raw URLs, tenant identifiers, completion capabilities, query
strings, request bodies, and headers never enter this registry.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import MutableMapping
from threading import Lock
from time import monotonic, time
from typing import Final

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..ports.auth import ConsumerSignInFailureReason

PROMETHEUS_CONTENT_TYPE: Final = "text/plain; version=0.0.4; charset=utf-8"
_DURATION_BUCKETS: Final = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_KNOWN_METHODS: Final = frozenset(
    {"DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"}
)
_SAFE_ROUTE = re.compile(r"^/[A-Za-z0-9_{}./:-]{0,199}$")
_MIN_HTTP_STATUS = 100
_MAX_HTTP_STATUS = 599
_FALLBACK_HTTP_STATUS = 500
_MAX_BUILD_LABEL_LENGTH = 128
_MIN_PRINTABLE_CODEPOINT = 0x20

type HttpKey = tuple[str, str, str]


class ApplicationMetrics:
    """Process-local application telemetry suitable for per-replica scraping."""

    def __init__(self, *, release_revision: str, image_digest: str | None) -> None:
        self._release_revision = _bounded_build_label(release_revision)
        self._image_digest = _bounded_build_label(image_digest or "unavailable")
        self._started_at = time()
        self._lock = Lock()
        self._request_counts: MutableMapping[HttpKey, int] = defaultdict(int)
        self._duration_counts: MutableMapping[HttpKey, int] = defaultdict(int)
        self._duration_sums: MutableMapping[HttpKey, float] = defaultdict(float)
        self._duration_buckets: MutableMapping[tuple[HttpKey, float], int] = defaultdict(int)
        self._consumer_sign_in_rejections: MutableMapping[ConsumerSignInFailureReason, int] = (
            defaultdict(int)
        )
        self._dependency_ready: dict[str, float] = {
            "database": 0.0,
            "identity": 0.0,
            "temporal": 0.0,
        }

    def observe_http(self, method: str, route: str, status_code: int, duration: float) -> None:
        """Record one request using a finite method and router-owned path template."""
        safe_method = method.upper() if method.upper() in _KNOWN_METHODS else "OTHER"
        safe_route = route if _SAFE_ROUTE.fullmatch(route) else "unmatched"
        safe_status = str(
            status_code
            if _MIN_HTTP_STATUS <= status_code <= _MAX_HTTP_STATUS
            else _FALLBACK_HTTP_STATUS
        )
        safe_duration = duration if math.isfinite(duration) and duration >= 0 else 0.0
        key = (safe_method, safe_route, safe_status)
        with self._lock:
            self._request_counts[key] += 1
            self._duration_counts[key] += 1
            self._duration_sums[key] += safe_duration
            for bucket in _DURATION_BUCKETS:
                if safe_duration <= bucket:
                    self._duration_buckets[(key, bucket)] += 1

    def set_dependency_ready(self, dependency: str, ready: bool) -> None:
        """Update only the fixed dependency gauges emitted by ``/readyz``."""
        if dependency not in self._dependency_ready:
            raise ValueError("unknown readiness dependency")
        with self._lock:
            self._dependency_ready[dependency] = 1.0 if ready else 0.0

    def observe_consumer_sign_in_rejection(self, reason: ConsumerSignInFailureReason) -> None:
        """Count one failed sign-in POST; accept only authored categories, never request data."""
        if not isinstance(reason, ConsumerSignInFailureReason):
            raise ValueError("unknown consumer sign-in rejection reason")
        with self._lock:
            self._consumer_sign_in_rejections[reason] += 1

    def render(self) -> str:
        """Render a consistent Prometheus 0.0.4 snapshot."""
        with self._lock:
            request_counts = dict(self._request_counts)
            duration_counts = dict(self._duration_counts)
            duration_sums = dict(self._duration_sums)
            duration_buckets = dict(self._duration_buckets)
            dependencies = dict(self._dependency_ready)
            sign_in_rejections = dict(self._consumer_sign_in_rejections)

        lines = [
            "# HELP events_concierge_build_info Immutable serving release identity.",
            "# TYPE events_concierge_build_info gauge",
            (
                "events_concierge_build_info"
                f'{{release_revision="{_escape_label(self._release_revision)}",'
                f'image_digest="{_escape_label(self._image_digest)}"}} 1'
            ),
            "# HELP events_concierge_process_start_time_seconds Process start time.",
            "# TYPE events_concierge_process_start_time_seconds gauge",
            f"events_concierge_process_start_time_seconds {self._started_at:.3f}",
            "# HELP events_concierge_dependency_ready Last readiness result by dependency.",
            "# TYPE events_concierge_dependency_ready gauge",
        ]
        for dependency, value in sorted(dependencies.items()):
            lines.append(
                f'events_concierge_dependency_ready{{dependency="{dependency}"}} {value:.0f}'
            )

        lines.extend(
            [
                "# HELP events_concierge_http_requests_total HTTP requests by bounded route template.",
                "# TYPE events_concierge_http_requests_total counter",
            ]
        )
        for key, count in sorted(request_counts.items()):
            lines.append(f"events_concierge_http_requests_total{_http_labels(key)} {count}")

        lines.extend(
            [
                "# HELP events_concierge_consumer_sign_in_rejections_total Rejected consumer sign-in POSTs by fixed category.",
                "# TYPE events_concierge_consumer_sign_in_rejections_total counter",
            ]
        )
        for reason, count in sorted(sign_in_rejections.items()):
            lines.append(
                "events_concierge_consumer_sign_in_rejections_total"
                f'{{reason="{reason.value}"}} {count}'
            )

        lines.extend(
            [
                "# HELP events_concierge_http_request_duration_seconds HTTP request duration.",
                "# TYPE events_concierge_http_request_duration_seconds histogram",
            ]
        )
        for key, count in sorted(duration_counts.items()):
            for bucket in _DURATION_BUCKETS:
                bucket_count = duration_buckets.get((key, bucket), 0)
                lines.append(
                    "events_concierge_http_request_duration_seconds_bucket"
                    f'{_http_labels(key, extra=f"le=\"{bucket:g}\"")} {bucket_count}'
                )
            lines.append(
                "events_concierge_http_request_duration_seconds_bucket"
                f'{_http_labels(key, extra="le=\"+Inf\"")} {count}'
            )
            lines.append(
                "events_concierge_http_request_duration_seconds_sum"
                f"{_http_labels(key)} {duration_sums[key]:.9f}"
            )
            lines.append(
                "events_concierge_http_request_duration_seconds_count"
                f"{_http_labels(key)} {count}"
            )
        return "\n".join(lines) + "\n"


class HttpMetricsMiddleware:
    """Measure requests after routing so the raw path can never become a label."""

    def __init__(self, app: ASGIApp, *, metrics: ApplicationMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = monotonic()
        status_code = 500

        async def capture_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            route = scope.get("route")
            route_template = getattr(route, "path", "unmatched")
            self.metrics.observe_http(
                str(scope.get("method", "OTHER")),
                route_template if isinstance(route_template, str) else "unmatched",
                status_code,
                monotonic() - started,
            )


def _http_labels(key: HttpKey, *, extra: str | None = None) -> str:
    method, route, status = key
    labels = [
        f'method="{_escape_label(method)}"',
        f'route="{_escape_label(route)}"',
        f'status="{_escape_label(status)}"',
    ]
    if extra is not None:
        labels.append(extra)
    return "{" + ",".join(labels) + "}"


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _bounded_build_label(value: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_BUILD_LABEL_LENGTH
        or any(ord(character) < _MIN_PRINTABLE_CODEPOINT for character in normalized)
    ):
        return "invalid"
    return normalized
