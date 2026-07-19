"""Secret/PII redaction contracts for application logging (FR-2.6, ADR-011)."""

from __future__ import annotations

import pytest
import structlog

from events_concierge.infra.logging import configure_logging, get_logger, redact_sensitive_event


def test_redaction_processor_scrubs_sensitive_keys_nested_values_and_embedded_urls() -> None:
    """The defense-in-depth renderer boundary cannot emit common plaintext secret shapes."""
    magic_link = "https://login.example.test/verify?token=magic-link-secret"
    event = {
        "event": "login failure",
        "tenant_id": "tenant-correlation-id",
        "workflow_id": "workflow-correlation-id",
        "otp": "123456",
        "raw_text": "private event request",
        "nested": {"password": "password-secret"},
        "error": (
            "database postgresql+psycopg://ec_app:database-secret@db.example/ec "
            f"authorization=Bearer auth-secret link={magic_link}"
        ),
    }

    redacted = redact_sensitive_event(None, "warning", event)

    rendered = str(redacted)
    for secret in (
        "123456",
        "private event request",
        "password-secret",
        "database-secret",
        "auth-secret",
        "magic-link-secret",
    ):
        assert secret not in rendered
    assert redacted["tenant_id"] == "tenant-correlation-id"
    assert redacted["workflow_id"] == "workflow-correlation-id"
    assert redacted["otp"] == "[REDACTED]"


def test_configured_json_logger_applies_redaction_before_rendering(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Both local and deployed renderers receive the processor, rather than relying on callers."""
    structlog.reset_defaults()
    try:
        configure_logging(local=False)
        get_logger("redaction-test").warning(
            "relay fixture",
            magic_link="https://login.example.test/?code=do-not-log-me",
            tenant_id="tenant-correlation-id",
        )
        captured = capsys.readouterr().out
    finally:
        structlog.reset_defaults()

    assert "do-not-log-me" not in captured
    assert "[REDACTED]" in captured
    assert "tenant-correlation-id" in captured
