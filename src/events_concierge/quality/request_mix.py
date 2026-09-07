"""Sanitized G1 request-mix measurement against a deployed read-only feed.

This module deliberately calls ``POST /v1/feed`` rather than durable request intake.  It measures
the highest-priority eligible lane on the first registerable ranked candidate without creating a
workflow or an external provider effect.  Reports retain scenario labels and aggregates, never the
natural-language prompts, event details, identifiers, URLs, credentials, or response bodies.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import httpx

_LANES: Final = ("autonomous_sla", "browser_best_effort", "handoff")
_NO_CANDIDATE: Final = "no_candidate"
_MAX_CORPUS_ENTRIES: Final = 500
_MAX_PROMPT_LENGTH: Final = 4_000
_MAX_DEPLOYMENT_LABEL_LENGTH: Final = 120
_MAX_TIMEOUT_SECONDS: Final = 120.0
_MIN_INTERVAL_SECONDS: Final = 0.1
_MAX_INTERVAL_SECONDS: Final = 60.0
_MAX_CREDENTIAL_BYTES: Final = 8_192
_ASCII_SPACE: Final = 32
_HTTP_OK: Final = 200
_MAX_URL_PORT: Final = 65_535
_SCENARIO_ID: Final = re.compile(r"scenario-[0-9]{4,6}\Z")


class RequestMixError(ValueError):
    """The corpus or deployed feed response cannot support a truthful measurement."""


@dataclass(frozen=True, slots=True)
class CorpusEntry:
    scenario_id: str
    text: str


@dataclass(frozen=True, slots=True)
class MixObservation:
    scenario_id: str
    lane: str
    candidate_count: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class MixReport:
    schema_version: str
    measured_at: str
    deployment_label: str
    corpus_sha256: str
    corpus_size: int
    lane_counts: dict[str, int]
    lane_percentages: dict[str, float]
    observations: tuple[MixObservation, ...]
    measurement_boundary: str


FeedProbe = Callable[[str], Mapping[str, object]]


def _object(value: object, *, context: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise RequestMixError(f"{context} must be a JSON object")
    return value


def load_corpus(path: Path) -> tuple[tuple[CorpusEntry, ...], str]:
    """Load a bounded corpus and return entries plus a stable digest of the exact input bytes."""
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise RequestMixError(f"cannot read corpus: {error}") from error
    try:
        document: object = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RequestMixError("corpus must be valid UTF-8 JSON") from error

    root = _object(document, context="corpus")
    unknown_root = set(root) - {"schema_version", "requests"}
    if unknown_root:
        raise RequestMixError(f"corpus has unknown fields: {', '.join(sorted(unknown_root))}")
    if root.get("schema_version") != "g1-request-mix-v1":
        raise RequestMixError("corpus schema_version must be 'g1-request-mix-v1'")
    raw_entries = root.get("requests")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise RequestMixError("corpus requests must be a non-empty array")
    if len(raw_entries) > _MAX_CORPUS_ENTRIES:
        raise RequestMixError(f"corpus cannot exceed {_MAX_CORPUS_ENTRIES} requests")

    entries: list[CorpusEntry] = []
    seen_ids: set[str] = set()
    for index, raw_entry in enumerate(raw_entries):
        entry = _object(raw_entry, context=f"requests[{index}]")
        unknown = set(entry) - {"id", "text"}
        if unknown:
            raise RequestMixError(
                f"requests[{index}] has unknown fields: {', '.join(sorted(unknown))}"
            )
        scenario_id = entry.get("id")
        text = entry.get("text")
        if (
            not isinstance(scenario_id, str)
            or _SCENARIO_ID.fullmatch(scenario_id) is None
        ):
            raise RequestMixError(
                f"requests[{index}].id must be an opaque scenario- followed by 4-6 digits"
            )
        if scenario_id in seen_ids:
            raise RequestMixError(f"duplicate request id: {scenario_id}")
        if not isinstance(text, str) or not text.strip() or len(text) > _MAX_PROMPT_LENGTH:
            raise RequestMixError(
                f"requests[{index}].text must be 1-{_MAX_PROMPT_LENGTH} characters"
            )
        seen_ids.add(scenario_id)
        entries.append(CorpusEntry(scenario_id=scenario_id, text=text))

    return tuple(entries), hashlib.sha256(raw).hexdigest()


def classify_feed(payload: Mapping[str, object]) -> tuple[str, int]:
    """Classify the first registerable result by its ordered, server-resolved lane plan."""
    items = payload.get("items")
    if not isinstance(items, list):
        raise RequestMixError("feed response items must be an array")
    for index, raw_item in enumerate(items):
        item = _object(raw_item, context=f"feed.items[{index}]")
        registerable = item.get("registerable")
        if not isinstance(registerable, bool):
            raise RequestMixError(f"feed.items[{index}].registerable must be boolean")
        if not registerable:
            continue
        lanes = item.get("lanes")
        if not isinstance(lanes, list) or not lanes:
            raise RequestMixError(f"feed.items[{index}].lanes must be a non-empty array")
        if not all(isinstance(lane, str) for lane in lanes):
            raise RequestMixError(f"feed.items[{index}].lanes must contain strings")
        lane = lanes[0]
        if lane not in _LANES:
            raise RequestMixError(f"feed.items[{index}] returned unsupported lane {lane!r}")
        return lane, len(items)
    return _NO_CANDIDATE, len(items)


def measure_request_mix(
    entries: Sequence[CorpusEntry],
    *,
    corpus_sha256: str,
    deployment_label: str,
    probe: FeedProbe,
    clock: Callable[[], datetime] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    pace: Callable[[], None] | None = None,
) -> MixReport:
    """Probe each prompt and build a report whose observation surface is intentionally non-PII."""
    if not entries:
        raise RequestMixError("at least one corpus request is required")
    if (
        not deployment_label.strip()
        or len(deployment_label) > _MAX_DEPLOYMENT_LABEL_LENGTH
    ):
        raise RequestMixError("deployment_label must be 1-120 characters")

    observations: list[MixObservation] = []
    counts: Counter[str] = Counter()
    for index, entry in enumerate(entries):
        if index and pace is not None:
            pace()
        started = monotonic()
        payload = probe(entry.text)
        elapsed_ms = max(0, round((monotonic() - started) * 1_000))
        lane, candidate_count = classify_feed(payload)
        counts[lane] += 1
        observations.append(
            MixObservation(
                scenario_id=entry.scenario_id,
                lane=lane,
                candidate_count=candidate_count,
                latency_ms=elapsed_ms,
            )
        )

    ordered_counts = {lane: counts[lane] for lane in (*_LANES, _NO_CANDIDATE)}
    total = len(entries)
    percentages = {lane: round(count * 100 / total, 2) for lane, count in ordered_counts.items()}
    measured_at = (clock or (lambda: datetime.now(UTC)))().astimezone(UTC).isoformat()
    return MixReport(
        schema_version="g1-request-mix-report-v1",
        measured_at=measured_at,
        deployment_label=deployment_label,
        corpus_sha256=corpus_sha256,
        corpus_size=total,
        lane_counts=ordered_counts,
        lane_percentages=percentages,
        observations=tuple(observations),
        measurement_boundary=(
            "Read-only /v1/feed lane-hint estimate for the first registerable ranked candidate. "
            "It does not prove the execution-time tenant/event membership result and cannot close "
            "G1 without that cohort evidence; it is not a provider success-rate, completion-"
            "latency, SLA, or legal-approval claim."
        ),
    )


def write_report(report: MixReport, path: Path) -> None:
    """Create evidence without silently replacing a prior field run."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as output:
            json.dump(asdict(report), output, indent=2, sort_keys=True)
            output.write("\n")
    except FileExistsError as error:
        raise RequestMixError(f"refusing to overwrite existing report: {path}") from error
    except OSError as error:
        raise RequestMixError(f"cannot create report: {error}") from error


def _http_probe(
    *,
    base_url: str,
    authorization: str | None,
    cookie: str | None,
    timeout_seconds: float,
    allow_http_local: bool,
    trusted_origin: str,
) -> tuple[httpx.Client, FeedProbe]:
    _validate_base_url(base_url, allow_http_local=allow_http_local)
    _validate_base_url(trusted_origin, allow_http_local=allow_http_local)
    if not _same_origin(base_url, trusted_origin):
        raise RequestMixError("base URL must exactly match G1_TRUSTED_ORIGIN")
    if (authorization is None) == (cookie is None):
        raise RequestMixError(
            "set exactly one of G1_AUTHORIZATION or G1_COOKIE for the measurement user"
        )
    headers: dict[str, str] = {"Accept": "application/json"}
    if authorization is not None:
        _validate_credential(authorization, "G1_AUTHORIZATION")
        headers["Authorization"] = authorization
    if cookie is not None:
        _validate_credential(cookie, "G1_COOKIE")
        headers["Cookie"] = cookie
    client = httpx.Client(
        base_url=base_url.rstrip("/"),
        headers=headers,
        follow_redirects=False,
        timeout=timeout_seconds,
    )

    def probe(text: str) -> Mapping[str, object]:
        response = client.post("/v1/feed", json={"text": text})
        if response.status_code != _HTTP_OK:
            raise RequestMixError(
                f"feed probe failed with HTTP {response.status_code}; response body is not retained"
            )
        try:
            value: Any = response.json()
        except json.JSONDecodeError as error:
            raise RequestMixError("feed probe returned invalid JSON") from error
        return _object(value, context="feed response")

    return client, probe


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--base-url", default=os.environ.get("G1_BASE_URL"))
    parser.add_argument("--deployment-label", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=float, default=20.0)
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=0.5,
        help="minimum delay between feed probes (0.1-60 seconds; default 0.5)",
    )
    parser.add_argument(
        "--allow-http-local",
        action="store_true",
        help="permit loopback HTTP for a non-field rehearsal; credentials stay on this host",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if not isinstance(args.base_url, str) or not args.base_url:
            raise RequestMixError("--base-url or G1_BASE_URL is required")
        trusted_origin = os.environ.get("G1_TRUSTED_ORIGIN")
        if not trusted_origin:
            raise RequestMixError("G1_TRUSTED_ORIGIN is required")
        if args.timeout_seconds <= 0 or args.timeout_seconds > _MAX_TIMEOUT_SECONDS:
            raise RequestMixError("--timeout-seconds must be greater than 0 and at most 120")
        if not _MIN_INTERVAL_SECONDS <= args.interval_seconds <= _MAX_INTERVAL_SECONDS:
            raise RequestMixError("--interval-seconds must be between 0.1 and 60")
        entries, digest = load_corpus(args.corpus)
        client, probe = _http_probe(
            base_url=args.base_url,
            authorization=os.environ.get("G1_AUTHORIZATION"),
            cookie=os.environ.get("G1_COOKIE"),
            timeout_seconds=args.timeout_seconds,
            allow_http_local=args.allow_http_local,
            trusted_origin=trusted_origin,
        )
        try:
            report = measure_request_mix(
                entries,
                corpus_sha256=digest,
                deployment_label=args.deployment_label,
                probe=probe,
                pace=lambda: time.sleep(args.interval_seconds),
            )
        finally:
            client.close()
        write_report(report, args.output)
    except (RequestMixError, httpx.HTTPError) as error:
        print(f"G1 measurement failed: {error}", file=sys.stderr)
        return 2

    print(json.dumps({"report": str(args.output), "lane_counts": report.lane_counts}))
    return 0


def _validate_base_url(value: str, *, allow_http_local: bool) -> None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as error:
        raise RequestMixError("base URL must be one unambiguous origin") from error
    is_loopback = _is_loopback(parsed.hostname)
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or (port is not None and not 1 <= port <= _MAX_URL_PORT)
        or (
            parsed.scheme != "https"
            and not (allow_http_local and parsed.scheme == "http" and is_loopback)
        )
    ):
        raise RequestMixError(
            "base URL must be an HTTPS origin (or loopback HTTP with --allow-http-local)"
        )


def _is_loopback(hostname: str | None) -> bool:
    if hostname is None:
        return False
    if hostname.rstrip(".").casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _same_origin(left: str, right: str) -> bool:
    """Compare scheme, IDNA-normalized host, and effective port; paths are rejected earlier."""
    left_url = urlsplit(left)
    right_url = urlsplit(right)
    return (
        left_url.scheme.casefold(),
        (left_url.hostname or "").encode("idna").lower(),
        left_url.port or (443 if left_url.scheme.casefold() == "https" else 80),
    ) == (
        right_url.scheme.casefold(),
        (right_url.hostname or "").encode("idna").lower(),
        right_url.port or (443 if right_url.scheme.casefold() == "https" else 80),
    )


def _validate_credential(value: str, label: str) -> None:
    encoded = value.encode("utf-8")
    if (
        not encoded
        or len(encoded) > _MAX_CREDENTIAL_BYTES
        or any(ord(char) < _ASCII_SPACE for char in value)
    ):
        raise RequestMixError(f"{label} must be a non-empty bounded single-line value")


if __name__ == "__main__":
    raise SystemExit(main())
