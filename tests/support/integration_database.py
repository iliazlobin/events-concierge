"""Strict naming and URL helpers for disposable integration-test databases."""

from __future__ import annotations

import re
from collections.abc import Mapping

from sqlalchemy.engine import URL, make_url

TEST_DATABASE_PREFIX = "ec_test_"
_RUN_ID_PATTERN = re.compile(r"[0-9a-f]{32}")
_CONNECTION_IDENTITY_QUERY_KEYS = frozenset(
    {
        "database",
        "dbname",
        "host",
        "hostaddr",
        "passfile",
        "password",
        "port",
        "service",
        "servicefile",
        "user",
    }
)


def _reject_connection_identity_query_options(url: URL) -> None:
    """Prevent driver query arguments from overriding the visibly validated URL authority/path."""
    conflicting = sorted(
        key for key in url.query if key.casefold() in _CONNECTION_IDENTITY_QUERY_KEYS
    )
    if conflicting:
        rendered = ", ".join(conflicting)
        raise ValueError(
            "integration-test database URLs cannot override connection identity "
            f"through query options: {rendered}"
        )


def isolated_database_name(run_id: str) -> str:
    """Return the one database name a runner is authorized to create and delete."""
    if _RUN_ID_PATTERN.fullmatch(run_id) is None:
        raise ValueError("integration-test run id must be exactly 32 lowercase hexadecimal digits")
    return f"{TEST_DATABASE_PREFIX}{run_id}"


def replace_database(raw_url: str, database: str) -> str:
    """Replace only a PostgreSQL URL's database, preserving its role and connection settings."""
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("integration-test database URLs must use PostgreSQL")
    _reject_connection_identity_query_options(url)
    return url.set(database=database).render_as_string(hide_password=False)


def maintenance_url(raw_url: str) -> URL:
    """Build the owner connection used solely to create/drop one allowlisted test database."""
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("integration-test migration URL must use PostgreSQL")
    _reject_connection_identity_query_options(url)
    return url.set(database="postgres")


def validate_isolated_test_environment(environ: Mapping[str, str]) -> str:
    """Prove app and owner URLs both point at this runner's exact disposable database."""
    run_id = environ.get("EC_INTEGRATION_TEST_RUN_ID")
    if run_id is None:
        raise ValueError(
            "service-backed integration tests require the isolated runner; "
            "use `make test-integration`, `make quality`, or `make quality-load`"
        )
    expected = isolated_database_name(run_id)
    for variable in ("EC_DATABASE_URL", "EC_MIGRATION_URL"):
        raw_url = environ.get(variable)
        if raw_url is None:
            raise ValueError(f"{variable} is required by the isolated integration-test runner")
        url = make_url(raw_url)
        _reject_connection_identity_query_options(url)
        if url.get_backend_name() != "postgresql" or url.database != expected:
            raise ValueError(f"{variable} must point at the isolated database {expected!r}")
    return expected
