"""Mandatory process-start checks, before listeners, pools, polling, or provider work.

These are configuration and callable-surface checks, not proof of encrypted connections or usable
credentials. Catalog executors and controllers have separate entrypoints and database authority;
they must not acquire consumer/BFF credentials merely to satisfy an application-shaped report.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..config import Settings
from ..infra.operator_database import validate_operator_database_url
from ..operations.config_validation import (
    ConfigCheck,
    validate_production_config,
    validate_runtime_ports,
)
from ..runtime import RuntimePorts, load_runtime_ports

_LOCAL_ENVIRONMENTS = frozenset({"", "dev", "development", "local", "test", "testing"})
_PROCESS_CHECKS = frozenset(
    {
        "environment",
        "mock_cloud_disabled",
        "migration_credential_absent",
        "release_revision",
        "image_digest",
    }
)
_CATALOG_CHECKS = _PROCESS_CHECKS | frozenset(
    {
        "shared_pacer",
        "redis_tls",
        "database_pool_budget",
        "temporal_tls",
        "temporal_credentials",
        "temporal_namespace",
        "temporal_target",
        "temporal_transport_configuration",
        "temporal_workload_isolation",
        "temporal_worker_versioning",
        "gcs_claim_check",
    }
)


class RuntimePreflightError(RuntimeError):
    """Sanitized startup rejection; diagnostics contain only fixed check names."""


def preflight_application_runtime(settings: Settings) -> RuntimePorts | None:
    """Validate every application check and return its single inspected provider bundle.

    API and consumer worker entrypoints pass this bundle to ``build_container``. There is no
    configurable role selector that could exempt an application process from its identity,
    transport, or provider requirements.
    """
    if _local_mock(settings):
        return None
    _require_checks(validate_production_config(settings, load_provider=False).checks)
    try:
        runtime = load_runtime_ports(settings)
    except Exception:
        raise RuntimePreflightError("runtime preflight failed: runtime_provider_loaded") from None
    _require_checks(validate_runtime_ports(runtime, settings=settings))
    return runtime


def preflight_catalog_runtime(settings: Settings) -> None:
    """Enforce executor DB, shared Redis, Temporal, storage and immutable release requirements."""
    if _local_mock(settings):
        return
    _require_process_checks(settings, _CATALOG_CHECKS)
    _require_checks(
        [
            ConfigCheck("catalog_executor_enabled", settings.ingestion_executor_enabled, ""),
            _database_check(settings, settings.ingestion_executor_database_url),
        ]
    )


def preflight_operator_runtime(settings: Settings) -> None:
    """Validate the controller process without consumer, Redis, Temporal or provider credentials.

    The controller only writes durable commands. Its database wrapper separately verifies the
    connected principal's isolated role before a query; API identity remains the IAP boundary.
    """
    if _local_mock(settings):
        return
    _require_process_checks(settings, _PROCESS_CHECKS)
    _require_checks([_database_check(settings, settings.operator_database_url)])


def _local_mock(settings: Settings) -> bool:
    return settings.mock_cloud and settings.env.strip().casefold() in _LOCAL_ENVIRONMENTS


def _require_process_checks(settings: Settings, names: frozenset[str]) -> None:
    checks = validate_production_config(settings, load_provider=False).checks
    selected = [check for check in checks if check.name in names]
    if {check.name for check in selected} != names:
        raise RuntimePreflightError("runtime preflight failed: process_contract")
    _require_checks(selected)


def _database_check(settings: Settings, url: str | None) -> ConfigCheck:
    try:
        validate_operator_database_url(settings, url)
    except ValueError:
        valid = False
    else:
        valid = True
    return ConfigCheck("isolated_database_transport", valid, "")


def _require_checks(checks: Iterable[ConfigCheck]) -> None:
    failed = [check.name for check in checks if not check.passed]
    if failed:
        raise RuntimePreflightError("runtime preflight failed: " + ", ".join(failed))
