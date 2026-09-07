from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from events_concierge.quality.request_mix import (
    CorpusEntry,
    RequestMixError,
    _http_probe,
    _validate_base_url,
    classify_feed,
    load_corpus,
    measure_request_mix,
    write_report,
)


def test_corpus_is_bounded_strict_and_hashed_from_exact_input(tmp_path: Path) -> None:
    path = tmp_path / "corpus.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "g1-request-mix-v1",
                "requests": [{"id": "scenario-0001", "text": "Something social after work"}],
            }
        ),
        encoding="utf-8",
    )

    entries, digest = load_corpus(path)

    assert entries == (CorpusEntry("scenario-0001", "Something social after work"),)
    assert len(digest) == 64


@pytest.mark.parametrize(
    "document, expected",
    [
        (
            {"schema_version": "wrong", "requests": [{"id": "scenario-0001", "text": "hello"}]},
            "schema_version",
        ),
        (
            {
                "schema_version": "g1-request-mix-v1",
                "requests": [
                    {"id": "scenario-0001", "text": "one"},
                    {"id": "scenario-0001", "text": "two"},
                ],
            },
            "duplicate",
        ),
        (
            {
                "schema_version": "g1-request-mix-v1",
                "requests": [
                    {"id": "scenario-0001", "text": "hello", "tenant": "forbidden"}
                ],
            },
            "unknown fields",
        ),
    ],
)
def test_invalid_corpus_is_rejected(tmp_path: Path, document: object, expected: str) -> None:
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(RequestMixError, match=expected):
        load_corpus(path)


def test_classifier_uses_first_registerable_ranked_candidate() -> None:
    lane, count = classify_feed(
        {
            "items": [
                {"registerable": False, "lanes": ["autonomous_sla"]},
                {"registerable": True, "lanes": ["browser_best_effort", "handoff"]},
                {"registerable": True, "lanes": ["autonomous_sla"]},
            ]
        }
    )

    assert lane == "browser_best_effort"
    assert count == 3


def test_classifier_returns_no_candidate_when_every_result_is_blocked() -> None:
    assert classify_feed({"items": [{"registerable": False, "lanes": ["handoff"]}]}) == (
        "no_candidate",
        1,
    )


def test_measurement_report_excludes_request_and_response_content() -> None:
    ticks = iter((1.0, 1.025, 2.0, 2.050))
    responses = iter(
        (
            {"items": [{"registerable": True, "lanes": ["autonomous_sla"]}]},
            {"items": []},
        )
    )

    report = measure_request_mix(
        (
            CorpusEntry("scenario-0001", "secret realistic prompt one"),
            CorpusEntry("scenario-0002", "secret realistic prompt two"),
        ),
        corpus_sha256="a" * 64,
        deployment_label="staging-west",
        probe=lambda _text: next(responses),
        clock=lambda: datetime(2026, 7, 22, tzinfo=UTC),
        monotonic=lambda: next(ticks),
    )

    encoded = json.dumps(asdict(report))
    assert report.lane_counts == {
        "autonomous_sla": 1,
        "browser_best_effort": 0,
        "handoff": 0,
        "no_candidate": 1,
    }
    assert report.lane_percentages["autonomous_sla"] == 50.0
    assert [item.latency_ms for item in report.observations] == [25, 50]
    assert "secret realistic" not in encoded
    assert "canonical_event_id" not in encoded


def test_report_writer_is_create_only(tmp_path: Path) -> None:
    ticks = iter((1.0, 1.0))
    report = measure_request_mix(
        (CorpusEntry("scenario-0001", "prompt"),),
        corpus_sha256="b" * 64,
        deployment_label="staging",
        probe=lambda _text: {"items": []},
        clock=lambda: datetime(2026, 7, 22, tzinfo=UTC),
        monotonic=lambda: next(ticks),
    )
    path = tmp_path / "report.json"

    write_report(report, path)

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["observations"] == [
        {
            "candidate_count": 0,
            "lane": "no_candidate",
            "latency_ms": 0,
            "scenario_id": "scenario-0001",
        }
    ]
    with pytest.raises(RequestMixError, match="refusing to overwrite"):
        write_report(report, path)


def test_field_probe_requires_https_and_local_http_is_explicit() -> None:
    _validate_base_url("https://staging.example.com", allow_http_local=False)
    _validate_base_url("http://127.0.0.1:8000", allow_http_local=True)

    with pytest.raises(RequestMixError, match="HTTPS origin"):
        _validate_base_url("http://staging.example.com", allow_http_local=True)
    with pytest.raises(RequestMixError, match="HTTPS origin"):
        _validate_base_url("http://127.0.0.1:8000", allow_http_local=False)
    with pytest.raises(RequestMixError, match="HTTPS origin"):
        _validate_base_url("https://user:secret@staging.example.com", allow_http_local=False)


def test_field_probe_accepts_exactly_one_credential_source() -> None:
    with pytest.raises(RequestMixError, match="exactly one"):
        _http_probe(
            base_url="https://staging.example.com",
            authorization="Bearer fixture",
            cookie="session=fixture",
            timeout_seconds=1.0,
            allow_http_local=False,
            trusted_origin="https://staging.example.com",
        )
    with pytest.raises(RequestMixError, match="exactly one"):
        _http_probe(
            base_url="https://staging.example.com",
            authorization=None,
            cookie=None,
            timeout_seconds=1.0,
            allow_http_local=False,
            trusted_origin="https://staging.example.com",
        )


def test_field_probe_requires_explicit_exact_trusted_origin() -> None:
    with pytest.raises(RequestMixError, match="exactly match"):
        _http_probe(
            base_url="https://attacker.example",
            authorization="Bearer fixture",
            cookie=None,
            timeout_seconds=1.0,
            allow_http_local=False,
            trusted_origin="https://staging.example.com",
        )


def test_corpus_requires_opaque_scenario_ids(tmp_path: Path) -> None:
    path = tmp_path / "corpus.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "g1-request-mix-v1",
                "requests": [{"id": "alice-after-work", "text": "hello"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(RequestMixError, match="opaque"):
        load_corpus(path)
