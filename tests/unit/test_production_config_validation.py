"""Production deployment preflight is explicit, fail-closed, and secret-free."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from events_concierge.adapters.mock.calendar import MockCalendar
from events_concierge.config import Settings
from events_concierge.domain.enums import Source
from events_concierge.operations import config_validation
from events_concierge.ports.sources import SourceCapability
from events_concierge.runtime import RuntimePorts


def _surface(*methods: str, **attributes: object) -> SimpleNamespace:
    values = dict(attributes)
    values.update({method: (lambda *args, **kwargs: None) for method in methods})
    return SimpleNamespace(**values)


def _source(source: Source) -> SimpleNamespace:
    return _surface(
        "discover",
        "read_membership_state",
        "read_registration_state",
        "register",
        capability=SourceCapability(
            source=source,
            supports_api=True,
            supports_browser_discovery=False,
            supports_autonomous_register=True,
        ),
    )


def _browser_session() -> SimpleNamespace:
    return _surface(
        "resolve_tenant_id",
        "verify_state_change",
        "start_login",
        "start_reauthentication",
        "complete_login",
        "issue_session",
        "revoke_session",
        "revoke_tenant_sessions",
        "verify_recent_auth",
        "is_ready",
        "aclose",
        login_cookie_name="__Host-login",
        session_cookie_name="__Host-session",
        csrf_cookie_name="__Host-csrf",
        csrf_header_name="X-CSRF",
        login_ttl_seconds=600,
        session_ttl_seconds=28_800,
    )


def _valid_runtime_ports() -> RuntimePorts:
    discovery = _source(Source.PUBLIC_JSONLD)
    registration = _source(Source.MEETUP)
    return RuntimePorts(
        object_store=_surface("put", "get", "delete_tenant"),
        notifier=_surface("send"),
        notification_secret_protector=_surface("protect_completion_url", "reveal_completion_url"),
        credential_vault=_surface("store", "get", "revoke", "delete_tenant"),
        calendar=_surface("free_busy", "upsert_event", "delete_event", "delete_tenant_events"),
        google_calendar_access=_surface("get_access"),
        google_calendar_bindings=_surface("get_binding", "upsert_binding"),
        discovery_sources=[discovery],
        register_sources={Source.MEETUP: registration},
        withdrawal_sources={Source.MEETUP: _surface("read_registration_state", "withdraw")},
        action_audit=_surface("append"),
        registration_consent=_surface("resolve", "validate"),
    )


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "env": "staging",
        "mock_cloud": False,
        "runtime_provider_factory": "deployment.runtime:provide",
        "public_base_url": "https://staging.concierge.example",
        "ui_auth_start_url": "/auth/login",
        "database_url": (
            "postgresql+psycopg://ec_app:fixture-secret@db.internal.example/"
            "events?sslmode=verify-full"
        ),
        "database_pool_size": 8,
        "database_max_overflow": 0,
        "redis_url": (
            "rediss://:fixture-secret@redis.internal.example:6380/0"
            "?ssl_cert_reqs=required&ssl_check_hostname=true"
        ),
        "temporal_target": "namespace.tmprl.cloud:7233",
        "temporal_namespace": "events-staging",
        "temporal_task_queue": "events-concierge-catalog",
        "temporal_transactional_task_queue": "events-concierge-transactional",
        "temporal_catalog_task_queue": "events-concierge-catalog",
        "temporal_worker_versioning_enabled": True,
        "temporal_tls_enabled": True,
        "temporal_api_key": "fixture-temporal-key",
        "gcp_project": "events-staging",
        "gcs_claim_check_bucket": "events-staging-claim-check",
        "media_backend": "gcs",
        "gcs_media_bucket": "events-staging-media",
        "oidc_bff_enabled": True,
        "oidc_issuer": "https://identity.example/",
        "oidc_authorization_url": "https://identity.example/authorize",
        "oidc_token_url": "https://identity.example/token",
        "oidc_jwks_url": "https://identity.example/jwks",
        "oidc_client_id": "events-concierge",
        "oidc_client_secret": "fixture-client-secret",
        "oidc_tenant_claim": "https://concierge.example/tenant_id",
        "release_revision": "0123456789abcdef0123456789abcdef01234567",
        "image_digest": "sha256:" + "a" * 64,
    }
    values.update(overrides)
    return Settings(**values)


def test_structural_production_config_accepts_a_remote_fail_closed_shape() -> None:
    report = config_validation.validate_production_config(
        _production_settings(),
        load_provider=False,
    )

    assert report.passed is True
    assert report.to_dict()["status"] == "passed"
    assert report.to_dict()["mode"] == "structural_only"
    assert report.to_dict()["evidence_class"] == "example_contract"
    assert report.to_dict()["release_eligible"] is False
    assert report.to_dict()["preflight_eligible"] is False
    assert {check.name for check in report.checks} >= {
        "environment",
        "mock_cloud_disabled",
        "public_origin",
        "built_in_bff_routes",
        "redis_tls",
        "application_database",
        "database_pool_budget",
        "temporal_tls",
        "temporal_workload_isolation",
        "temporal_worker_versioning",
        "gcs_claim_check",
        "runtime_provider_loaded",
    }


def test_structural_production_config_accepts_cloud_sql_auth_proxy_mode() -> None:
    report = config_validation.validate_production_config(
        _production_settings(
            database_connection_mode="cloud_sql_proxy",
            database_url=(
                "postgresql+psycopg://ec_app:fixture-secret@127.0.0.1:5432/events?sslmode=disable"
            ),
        ),
        load_provider=False,
    )

    assert report.passed is True
    database = next(check for check in report.checks if check.name == "application_database")
    assert "Cloud SQL Auth Proxy" in database.detail


@pytest.mark.parametrize(
    ("override", "check_name"),
    [
        ({"env": "local"}, "environment"),
        ({"mock_cloud": True, "oidc_bff_enabled": False}, "mock_cloud_disabled"),
        ({"runtime_provider_factory": None}, "runtime_provider_configured"),
        ({"public_base_url": "http://public.example"}, "public_origin"),
        ({"public_base_url": "https://localhost:8000"}, "public_origin"),
        ({"public_base_url": "https://app.foo.localhost"}, "public_origin"),
        ({"ui_auth_start_url": None}, "ui_auth_entrypoint"),
        ({"pacer_backend": "memory"}, "shared_pacer"),
        ({"redis_url": "redis://redis.internal.example/0"}, "redis_tls"),
        (
            {"redis_url": "rediss://redis.internal.example/0?ssl_cert_reqs=NONE"},
            "redis_tls",
        ),
        (
            {"redis_url": "rediss://redis.internal.example/0?ssl_check_hostname=0"},
            "redis_tls",
        ),
        (
            {"redis_url": "rediss://[::]/0?ssl_cert_reqs=required"},
            "redis_tls",
        ),
        (
            {"database_url": "postgresql+psycopg://app:secret@localhost/events"},
            "application_database",
        ),
        (
            {"database_url": "postgresql+psycopg://app:secret@db.example/events"},
            "application_database",
        ),
        (
            {"database_url": ("postgresql://app:secret@db.example/events?sslmode=verify-full")},
            "application_database",
        ),
        (
            {
                "database_url": (
                    "postgresql+psycopg://app:secret@db.example/events"
                    "?sslmode=VERIFY-FULL&sslmode=disable"
                )
            },
            "application_database",
        ),
        (
            {"database_url": "postgresql+psycopg://postgres:secret@db.example/events"},
            "application_database",
        ),
        ({"database_pool_size": 7}, "database_pool_budget"),
        ({"database_max_overflow": 1}, "database_pool_budget"),
        ({"gcp_project": None}, "gcs_claim_check"),
        ({"gcs_claim_check_bucket": None}, "gcs_claim_check"),
        ({"media_backend": "local"}, "durable_media"),
        ({"temporal_tls_enabled": False}, "temporal_tls"),
        ({"temporal_tls_domain": " \t"}, "temporal_transport_configuration"),
        ({"temporal_namespace": "default"}, "temporal_namespace"),
        ({"temporal_target": "localhost:7233"}, "temporal_target"),
        ({"temporal_target": "[::ffff:127.0.0.1]:7233"}, "temporal_target"),
        ({"temporal_target": "namespace.tmprl.cloud"}, "temporal_target"),
        ({"temporal_target": "namespace.tmprl.cloud:99999"}, "temporal_target"),
        ({"release_revision": "development"}, "release_revision"),
        ({"release_revision": "main"}, "release_revision"),
        ({"release_revision": "latest"}, "release_revision"),
        ({"image_digest": None}, "image_digest"),
    ],
)
def test_unsafe_production_shapes_fail_the_named_check(
    override: dict[str, object],
    check_name: str,
) -> None:
    report = config_validation.validate_production_config(
        _production_settings(**override),
        load_provider=False,
    )

    assert report.passed is False
    assert next(check for check in report.checks if check.name == check_name).passed is False


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+psycopg://ec_app:secret@localhost:5432/events?sslmode=disable",
        "postgresql+psycopg://ec_app:secret@127.0.0.1/events?sslmode=disable",
        "postgresql+psycopg://ec_app:secret@127.0.0.1:5432/events?sslmode=verify-full",
        "postgresql+psycopg://postgres:secret@127.0.0.1:5432/events?sslmode=disable",
    ],
)
def test_cloud_sql_proxy_mode_rejects_ambiguous_or_privileged_dsn(
    database_url: str,
) -> None:
    report = config_validation.validate_production_config(
        _production_settings(
            database_connection_mode="cloud_sql_proxy",
            database_url=database_url,
        ),
        load_provider=False,
    )

    database = next(check for check in report.checks if check.name == "application_database")
    assert database.passed is False


def test_runtime_provider_import_and_every_required_boundary_are_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _valid_runtime_ports()
    monkeypatch.setattr(config_validation, "load_runtime_ports", lambda settings: runtime)

    report = config_validation.validate_production_config(_production_settings())

    assert report.passed is True
    assert report.to_dict()["mode"] == "full"
    assert report.to_dict()["evidence_class"] == "wiring_preflight"
    assert report.to_dict()["preflight_eligible"] is True
    assert report.to_dict()["release_eligible"] is False
    assert next(
        check for check in report.checks if check.name == "runtime_identity_boundary"
    ).passed
    assert next(
        check for check in report.checks if check.name == "runtime_port_register_sources"
    ).passed


def test_runtime_provider_failure_is_reported_without_exception_or_secret_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(settings: Settings) -> RuntimePorts:
        del settings
        raise RuntimeError("provider_token=fixture-super-secret")

    monkeypatch.setattr(config_validation, "load_runtime_ports", fail)

    report = config_validation.validate_production_config(_production_settings())
    rendered = str(report.to_dict())

    assert report.passed is False
    assert "RuntimeError" in rendered
    assert "fixture-super-secret" not in rendered
    assert "fixture-secret" not in rendered


def test_application_runtime_rejects_presence_of_migration_owner_credential() -> None:
    report = config_validation.validate_production_config(
        _production_settings(),
        load_provider=False,
        migration_credential_present=True,
    )

    assert report.passed is False
    assert (
        next(check for check in report.checks if check.name == "migration_credential_absent").passed
        is False
    )


def test_builtin_oidc_bff_satisfies_identity_without_provider_auth_ports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = replace(
        _valid_runtime_ports(),
        auth_context=None,
        csrf_protection=None,
        browser_session=None,
    )
    monkeypatch.setattr(config_validation, "load_runtime_ports", lambda settings: runtime)
    settings = _production_settings(
        oidc_bff_enabled=True,
        ui_auth_start_url="/auth/login",
        oidc_issuer="https://identity.example/",
        oidc_authorization_url="https://identity.example/authorize",
        oidc_token_url="https://identity.example/token",
        oidc_jwks_url="https://identity.example/jwks",
        oidc_client_id="events-concierge",
        oidc_client_secret="fixture-client-secret",
        oidc_tenant_claim="https://concierge.example/tenant_id",
    )

    report = config_validation.validate_production_config(settings)

    assert report.passed is True
    identity = next(check for check in report.checks if check.name == "runtime_identity_boundary")
    assert identity.detail == (
        "repository OIDC BFF is the sole session authentication and CSRF authority"
    )


def test_provider_mode_requires_paired_authentication_and_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = replace(_valid_runtime_ports(), browser_session=None)
    monkeypatch.setattr(config_validation, "load_runtime_ports", lambda settings: runtime)

    report = config_validation.validate_production_config(
        _production_settings(oidc_bff_enabled=False)
    )

    assert report.passed is False
    assert (
        next(check for check in report.checks if check.name == "runtime_identity_boundary").passed
        is False
    )


def test_production_validator_rejects_repository_mock_in_provider_bundle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = replace(_valid_runtime_ports(), calendar=MockCalendar())
    monkeypatch.setattr(config_validation, "load_runtime_ports", lambda settings: runtime)

    report = config_validation.validate_production_config(_production_settings())

    assert report.passed is False
    calendar = next(check for check in report.checks if check.name == "runtime_port_calendar")
    assert calendar.passed is False
    assert "mock-provisioned" in calendar.detail


def test_production_validator_rejects_arbitrary_non_callable_port_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = replace(_valid_runtime_ports(), object_store=object())  # type: ignore[arg-type]
    monkeypatch.setattr(config_validation, "load_runtime_ports", lambda settings: runtime)

    report = config_validation.validate_production_config(_production_settings())

    check = next(check for check in report.checks if check.name == "runtime_port_object_store")
    assert check.passed is False


def test_production_validator_requires_calendar_tenant_erasure_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    incomplete_calendar = _surface("free_busy", "upsert_event", "delete_event")
    runtime = replace(_valid_runtime_ports(), calendar=incomplete_calendar)  # type: ignore[arg-type]
    monkeypatch.setattr(config_validation, "load_runtime_ports", lambda settings: runtime)

    report = config_validation.validate_production_config(_production_settings())

    check = next(check for check in report.checks if check.name == "runtime_port_calendar")
    assert check.passed is False


def test_builtin_identity_rejects_provider_identity_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = replace(
        _valid_runtime_ports(),
        browser_session=None,
        auth_context=_surface("resolve_tenant_id"),
    )
    monkeypatch.setattr(config_validation, "load_runtime_ports", lambda settings: runtime)
    settings = _production_settings(
        oidc_bff_enabled=True,
        ui_auth_start_url="/auth/login",
        oidc_issuer="https://identity.example/",
        oidc_authorization_url="https://identity.example/authorize",
        oidc_token_url="https://identity.example/token",
        oidc_jwks_url="https://identity.example/jwks",
        oidc_client_id="events-concierge",
        oidc_client_secret="fixture-client-secret",
        oidc_tenant_claim="https://concierge.example/tenant_id",
    )

    report = config_validation.validate_production_config(settings)

    identity = next(check for check in report.checks if check.name == "runtime_identity_boundary")
    assert identity.passed is False


def test_external_bff_is_not_a_supported_current_production_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = replace(_valid_runtime_ports(), browser_session=_browser_session())
    monkeypatch.setattr(config_validation, "load_runtime_ports", lambda settings: runtime)

    report = config_validation.validate_production_config(
        _production_settings(oidc_bff_enabled=False)
    )

    identity_profile = next(
        check for check in report.checks if check.name == "production_identity_profile"
    )
    runtime_identity = next(
        check for check in report.checks if check.name == "runtime_identity_boundary"
    )
    assert identity_profile.passed is False
    assert runtime_identity.passed is True


def test_external_browser_session_requires_tenant_wide_revocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    browser_session = _browser_session()
    browser_session.revoke_tenant_sessions = None
    runtime = replace(_valid_runtime_ports(), browser_session=browser_session)
    monkeypatch.setattr(config_validation, "load_runtime_ports", lambda settings: runtime)

    report = config_validation.validate_production_config(
        _production_settings(oidc_bff_enabled=False)
    )

    identity = next(check for check in report.checks if check.name == "runtime_identity_boundary")
    assert identity.passed is False


@pytest.mark.parametrize("missing_method", ["start_reauthentication", "verify_recent_auth"])
def test_external_browser_session_requires_destructive_action_step_up(
    monkeypatch: pytest.MonkeyPatch,
    missing_method: str,
) -> None:
    browser_session = _browser_session()
    setattr(browser_session, missing_method, None)
    runtime = replace(_valid_runtime_ports(), browser_session=browser_session)
    monkeypatch.setattr(config_validation, "load_runtime_ports", lambda settings: runtime)

    report = config_validation.validate_production_config(
        _production_settings(oidc_bff_enabled=False)
    )

    identity = next(check for check in report.checks if check.name == "runtime_identity_boundary")
    assert identity.passed is False


@pytest.mark.parametrize(
    "override",
    [
        {"oidc_token_url": "http://identity.example/token"},
        {"oidc_jwks_url": "https://localhost/jwks"},
        {"oidc_algorithms": "HS256"},
        {"oidc_algorithms": "RS256,RS256"},
    ],
)
def test_builtin_identity_configuration_fails_closed(
    override: dict[str, object],
) -> None:
    settings_values: dict[str, object] = {
        "oidc_bff_enabled": True,
        "ui_auth_start_url": "/auth/login",
        "oidc_issuer": "https://identity.example/",
        "oidc_authorization_url": "https://identity.example/authorize",
        "oidc_token_url": "https://identity.example/token",
        "oidc_jwks_url": "https://identity.example/jwks",
        "oidc_client_id": "events-concierge",
        "oidc_client_secret": "fixture-client-secret",
        "oidc_tenant_claim": "https://concierge.example/tenant_id",
    }
    settings_values.update(override)

    report = config_validation.validate_production_config(
        _production_settings(**settings_values),
        load_provider=False,
    )

    check = next(
        check for check in report.checks if check.name == "built_in_identity_configuration"
    )
    assert check.passed is False
