"""Construction checks for a discovery release without deferred product providers."""

from __future__ import annotations

from ..adapters.disabled import is_disabled_discovery_port
from ..config import Settings
from ..runtime import RuntimePorts


def validate_discovery_settings(settings: Settings) -> None:
    """Reject credentials and opt-ins for capabilities excluded from this release."""
    if (
        settings.agent_enabled
        or settings.google_calendar_enabled
        or settings.google_calendar_access_factory is not None
        or settings.cohere_api_key is not None
        or settings.cohere_api_key_file is not None
    ):
        raise ValueError(
            "discovery requires chat, Calendar and model provider configuration to be disabled"
        )


def discovery_effects_disabled(runtime: RuntimePorts) -> bool:
    """Attest graph disablement, never external deletion or historical provider absence."""
    return (
        all(
            is_disabled_discovery_port(name, getattr(runtime, name))
            for name in (
                "notifier",
                "notification_secret_protector",
                "credential_vault",
                "calendar",
            )
        )
        and runtime.register_sources == {}
        and runtime.withdrawal_sources == {}
        and runtime.google_calendar_access is None
        and runtime.google_calendar_bindings is None
    )
