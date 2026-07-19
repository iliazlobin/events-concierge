"""structlog configuration -- JSON in non-local envs, console renderer locally."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

import structlog

_REDACTED = "[REDACTED]"
_SENSITIVE_KEY_MARKERS = frozenset(
    {
        "apikey",
        "authorization",
        "cookie",
        "email",
        "magiclink",
        "otp",
        "password",
        "rawtext",
        "secret",
        "token",
    }
)
_ASSIGNMENT_SECRET = re.compile(
    r"(?i)\b(authorization|api[_-]?key|cookie|magic[_-]?link|otp|password|secret|token)"
    r"\s*([:=])\s*(?:bearer\s+)?([^\s,;]+)"
)
_BEARER_SECRET = re.compile(r"(?i)\bbearer\s+[^\s,;]+")
_URL_USERINFO = re.compile(r"([a-z][a-z0-9+.-]*://)[^/@\s:]+:[^@/\s]+@", re.IGNORECASE)
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:access[_-]?token|api[_-]?key|code|magic[_-]?link|otp|password|"
    r"secret|token)=)([^&#\s]+)"
)


def configure_logging(level: str = "info", *, local: bool = True) -> None:
    logging.basicConfig(level=level.upper(), format="%(message)s")
    processors: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,
        redact_sensitive_event,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
    ]
    processors.append(
        structlog.dev.ConsoleRenderer() if local else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level.upper())),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger


def redact_sensitive_event(
    logger: structlog.typing.WrappedLogger,
    method_name: str,
    event_dict: structlog.typing.EventDict,
) -> structlog.typing.EventDict:
    """Remove plaintext secrets/PII before either local or JSON rendering (FR-2.6, ADR-011).

    This is a defense-in-depth final boundary, not permission to pass secrets through ordinary
    application types.  Opaque IDs such as workflow and tenant identifiers remain useful for
    correlation; values under sensitive keys and secret-shaped values in error text are replaced.
    """
    del logger, method_name
    return {key: _redact_value(key, value) for key, value in event_dict.items()}


def _redact_value(key: str, value: Any) -> Any:
    if _is_sensitive_key(key):
        return _REDACTED
    if isinstance(value, Mapping):
        return {
            nested_key: _redact_value(str(nested_key), nested_value)
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(key, item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(key, item) for item in value)
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
    return any(marker in normalized for marker in _SENSITIVE_KEY_MARKERS)


def _redact_text(value: str) -> str:
    """Scrub common secret encodings embedded in exception strings and URLs."""
    value = _URL_USERINFO.sub(rf"\1{_REDACTED}@", value)
    value = _QUERY_SECRET.sub(rf"\1{_REDACTED}", value)
    value = _ASSIGNMENT_SECRET.sub(rf"\1\2{_REDACTED}", value)
    return _BEARER_SECRET.sub(f"Bearer {_REDACTED}", value)
