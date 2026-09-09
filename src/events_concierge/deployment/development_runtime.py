"""Private test profile: real shared GCS; explicit mock external product integrations."""

from ..config import Settings
from ..runtime import RuntimePorts
from .gcp_runtime import build_gcs_object_store


def build_runtime_ports(settings: Settings) -> RuntimePorts:
    if settings.env != "development" or not settings.mock_cloud:
        raise ValueError(
            "development_runtime requires EC_ENV=development and explicit mock integrations"
        )
    if settings.google_calendar_enabled:
        raise ValueError("development_runtime does not enable real Calendar actions")
    return RuntimePorts(object_store=build_gcs_object_store(settings))
