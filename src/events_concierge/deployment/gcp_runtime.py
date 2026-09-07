"""Bounded production runtime provider for the GCP deployment profile.

This provider deliberately assembles only boundaries that the repository can currently construct
without inventing credentials or silently selecting a mock.  It provisions native GCS claim-check
storage, the reviewed public discovery source, Postgres audit/consent repositories, and (when
enabled) Google Calendar using the existing tenant-access factory.  Autonomous registration and
withdrawal remain explicitly disabled.

Notification delivery, Cloud KMS notification protection, and the encrypted credential vault are
left unprovisioned until their GCP adapters land.  Production preflight and non-mock composition
therefore continue to fail closed instead of mistaking this Phase-1 slice for a complete launch
graph.
"""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
from threading import Lock
from typing import Protocol, cast

from ..adapters.crawl.source import PublicJsonLdSource
from ..adapters.gcs import GcsBlob, GcsBucket, GcsObjectStore, GcsStorageClient
from ..adapters.google_calendar.calendar import GoogleCalendarAdapter
from ..adapters.postgres.audit import PostgresRegistrationActionAuditRepository
from ..adapters.postgres.calendar_bindings import PostgresGoogleCalendarBindings
from ..adapters.postgres.consent import PostgresRegistrationConsentEvidenceRepository
from ..config import Settings
from ..ports.google_calendar import GoogleCalendarAccessPort
from ..runtime import RuntimePorts

_DEFAULT_CLAIM_CHECK_PREFIX = "events-concierge/claim-check/v1"


class GcpRuntimeConfigurationError(ValueError):
    """The GCP runtime settings do not form a safe, explicit deployment profile."""


class GcpRuntimeDependencyError(RuntimeError):
    """A production-only SDK or deployment factory cannot be constructed."""


class _ClientConstructor(Protocol):
    def __call__(self, **kwargs: object) -> object: ...


class _LazyGcsStorageClient:
    """Delay ADC discovery until the first claim-check operation performs GCS I/O."""

    def __init__(self, constructor: _ClientConstructor, arguments: dict[str, object]) -> None:
        self._constructor = constructor
        self._arguments = dict(arguments)
        self._client: GcsStorageClient | None = None
        self._lock = Lock()

    def bucket(self, bucket_name: str) -> GcsBucket:
        return self._initialized_client().bucket(bucket_name)

    def list_blobs(
        self,
        bucket: GcsBucket,
        *,
        prefix: str,
        versions: bool,
        page_size: int,
    ) -> Iterable[GcsBlob]:
        return self._initialized_client().list_blobs(
            bucket,
            prefix=prefix,
            versions=versions,
            page_size=page_size,
        )

    def _initialized_client(self) -> GcsStorageClient:
        client = self._client
        if client is not None:
            return client
        with self._lock:
            if self._client is None:
                try:
                    candidate = self._constructor(**self._arguments)
                except Exception as error:
                    raise GcpRuntimeDependencyError(
                        "the GCS client could not initialize Application Default Credentials"
                    ) from error
                if not callable(getattr(candidate, "bucket", None)) or not callable(
                    getattr(candidate, "list_blobs", None)
                ):
                    raise GcpRuntimeDependencyError(
                        "google.cloud.storage.Client does not expose the required storage surface"
                    )
                self._client = cast("GcsStorageClient", candidate)
            return self._client


def build_runtime_ports(
    settings: Settings,
    /,
    *,
    storage_client: GcsStorageClient | None = None,
) -> RuntimePorts:
    """Construct the currently supported, network-lazy GCP runtime boundaries.

    ``storage_client`` is an explicit hermetic seam.  Normal process startup omits it and lazily
    imports ``google-cloud-storage``; client construction may discover Application Default
    Credentials, while bucket and object operations remain lazy until the port is used.
    """
    if settings.mock_cloud:
        raise GcpRuntimeConfigurationError("the GCP runtime provider requires EC_MOCK_CLOUD=false")

    bucket_name = _required_setting(settings, "gcs_claim_check_bucket")
    root_prefix = _optional_setting(
        settings,
        "gcs_claim_check_prefix",
        default=_DEFAULT_CLAIM_CHECK_PREFIX,
    )
    assert root_prefix is not None
    project = _optional_setting(settings, "gcp_project", default=None)
    client = storage_client or _default_storage_client(project=project)
    object_store = GcsObjectStore(
        client,
        bucket=bucket_name,
        root_prefix=root_prefix,
    )

    google_access, google_bindings, calendar = _google_calendar_ports(settings)
    discovery_sources = _discovery_sources(settings)
    return RuntimePorts(
        object_store=object_store,
        calendar=calendar,
        google_calendar_access=google_access,
        google_calendar_bindings=google_bindings,
        discovery_sources=discovery_sources,
        # These empty maps are an explicit production decision: mutation lanes remain disabled
        # until their provider evidence, credentials, withdrawal behavior, and review gates exist.
        register_sources={},
        withdrawal_sources={},
        action_audit=PostgresRegistrationActionAuditRepository(),
        registration_consent=PostgresRegistrationConsentEvidenceRepository(),
    )


def _default_storage_client(*, project: str | None) -> GcsStorageClient:
    try:
        module = import_module("google.cloud.storage")
        constructor = cast("_ClientConstructor", module.Client)
    except (AttributeError, ImportError) as error:
        raise GcpRuntimeDependencyError(
            "the GCP runtime provider requires the google-cloud-storage package"
        ) from error
    if not callable(constructor):
        raise GcpRuntimeDependencyError("google.cloud.storage.Client is not callable")
    arguments: dict[str, object] = {}
    if project is not None:
        arguments["project"] = project
    return _LazyGcsStorageClient(constructor, arguments)


def _discovery_sources(settings: Settings) -> tuple[PublicJsonLdSource, ...]:
    enabled = {value.strip() for value in settings.discovery_sources.split(",") if value.strip()}
    if "public_jsonld" not in enabled:
        return ()
    return (
        PublicJsonLdSource(
            user_agent=settings.crawl_user_agent,
            min_interval_ms=settings.crawl_min_interval_ms,
            seed_urls=[
                value.strip() for value in settings.crawl_seed_urls.split(",") if value.strip()
            ],
        ),
    )


def _google_calendar_ports(
    settings: Settings,
) -> tuple[
    GoogleCalendarAccessPort | None,
    PostgresGoogleCalendarBindings | None,
    GoogleCalendarAdapter | None,
]:
    if not settings.google_calendar_enabled:
        return None, None, None
    configured = settings.google_calendar_access_factory
    if configured is None or not configured.strip():
        raise GcpRuntimeConfigurationError(
            "Google Calendar requires EC_GOOGLE_CALENDAR_ACCESS_FACTORY"
        )
    access = _load_google_calendar_access(configured)
    bindings = PostgresGoogleCalendarBindings()
    calendar = GoogleCalendarAdapter(
        access,
        bindings,
        timeout_s=settings.google_calendar_timeout_seconds,
    )
    return access, bindings, calendar


def _load_google_calendar_access(configured: str) -> GoogleCalendarAccessPort:
    module_name, separator, attribute_name = configured.strip().partition(":")
    if not separator or not module_name or not attribute_name or ":" in attribute_name:
        raise GcpRuntimeConfigurationError(
            "EC_GOOGLE_CALENDAR_ACCESS_FACTORY must use the form 'module:callable'"
        )
    try:
        module = import_module(module_name)
        factory = getattr(module, attribute_name)
    except (AttributeError, ImportError) as error:
        raise GcpRuntimeDependencyError(
            "the Google Calendar access factory could not be loaded"
        ) from error
    if not callable(factory):
        raise GcpRuntimeConfigurationError(
            "EC_GOOGLE_CALENDAR_ACCESS_FACTORY must resolve to a callable"
        )
    try:
        access = factory()
    except Exception as error:
        raise GcpRuntimeDependencyError(
            "the Google Calendar access factory could not initialize"
        ) from error
    if not callable(getattr(access, "get_access", None)):
        raise GcpRuntimeDependencyError(
            "the Google Calendar access factory did not return GoogleCalendarAccessPort"
        )
    return cast("GoogleCalendarAccessPort", access)


def _required_setting(settings: Settings, name: str) -> str:
    value = getattr(settings, name, None)
    if not isinstance(value, str) or not value.strip():
        environment_name = f"EC_{name.upper()}"
        raise GcpRuntimeConfigurationError(f"the GCP runtime provider requires {environment_name}")
    if value != value.strip():
        raise GcpRuntimeConfigurationError(f"EC_{name.upper()} must not contain outer whitespace")
    return value


def _optional_setting(
    settings: Settings,
    name: str,
    *,
    default: str | None,
) -> str | None:
    value = getattr(settings, name, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise GcpRuntimeConfigurationError(
            f"EC_{name.upper()} must be a non-empty value without outer whitespace"
        )
    return value
