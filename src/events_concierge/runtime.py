"""Typed deployment-owned runtime provisioning.

The repository can construct its database-backed and offline-safe adapters directly. Credentials,
cloud transports, tenant authentication, and source mutation adapters instead arrive through one
explicit factory. Keeping the factory synchronous makes graph construction deterministic: adapters
may establish network connections lazily, but startup can validate the complete boundary first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib import import_module
from typing import Protocol, cast

from .config import Settings
from .domain.enums import Source
from .ports.audit import RegistrationActionAuditPort
from .ports.auth import AuthContextPort, BrowserSessionLifecyclePort, CsrfProtectionPort
from .ports.calendar import CalendarPort
from .ports.consent import RegistrationConsentEvidencePort
from .ports.credentials import CredentialVault
from .ports.google_calendar import GoogleCalendarAccessPort, GoogleCalendarBindingPort
from .ports.notification_secrets import NotificationSecretProtector
from .ports.notifications import NotificationPort
from .ports.object_store import ObjectStorePort
from .ports.sources import SourcePort
from .ports.withdrawal import RegistrationWithdrawalPort


@dataclass(frozen=True, slots=True)
class RuntimePorts:
    """Provisioned boundaries that cannot safely be inferred by the core composition root.

    ``None`` means "not provisioned." Production authentication and CSRF verification are a paired
    edge contract: the former resolves the deployment session and the latter binds state-changing
    requests to it. Source maps intentionally preserve the distinction from an explicitly empty
    mapping, which lets a deployment disable autonomous registration or withdrawal deliberately
    without silently inheriting an accidental empty default.
    """

    auth_context: AuthContextPort | None = None
    csrf_protection: CsrfProtectionPort | None = None
    browser_session: BrowserSessionLifecyclePort | None = None
    object_store: ObjectStorePort | None = None
    notifier: NotificationPort | None = None
    notification_secret_protector: NotificationSecretProtector | None = None
    credential_vault: CredentialVault | None = None
    calendar: CalendarPort | None = None
    google_calendar_access: GoogleCalendarAccessPort | None = None
    google_calendar_bindings: GoogleCalendarBindingPort | None = None
    discovery_sources: Sequence[SourcePort] | None = None
    register_sources: Mapping[Source, SourcePort] | None = None
    withdrawal_sources: Mapping[Source, RegistrationWithdrawalPort] | None = None
    action_audit: RegistrationActionAuditPort | None = None
    registration_consent: RegistrationConsentEvidencePort | None = None


class RuntimeProvider(Protocol):
    """A deployment factory loaded from ``Settings.runtime_provider_factory``."""

    def __call__(self, settings: Settings, /) -> RuntimePorts:
        """Return one fully provisioned, process-local runtime boundary bundle."""
        ...


def load_runtime_ports(settings: Settings) -> RuntimePorts:
    """Load and invoke the configured deployment factory, or return an empty mock bundle."""
    configured = settings.runtime_provider_factory
    if configured is None:
        return RuntimePorts()
    module_name, separator, attribute_name = configured.strip().partition(":")
    if not separator or not module_name or not attribute_name or ":" in attribute_name:
        raise ValueError("runtime_provider_factory must use the form 'module:callable'")
    try:
        module = import_module(module_name)
        provider = cast("object", getattr(module, attribute_name))
    except (AttributeError, ImportError) as error:
        raise ValueError(
            "runtime_provider_factory could not load its configured callable"
        ) from error
    if not callable(provider):
        raise ValueError("runtime_provider_factory must resolve to a callable")
    runtime = cast("RuntimeProvider", provider)(settings)
    if not isinstance(runtime, RuntimePorts):
        raise ValueError("runtime_provider_factory must return RuntimePorts")
    return runtime
