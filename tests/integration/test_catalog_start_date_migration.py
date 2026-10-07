"""The production filter repair can ship without enabling the held Muse schema."""

import os
import subprocess
import sys
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from tests.support.integration_database import (
    isolated_database_name,
    replace_database,
    validate_isolated_test_environment,
)
from tests.support.run_isolated_integration import _create_database, _drop_database

pytestmark = pytest.mark.integration


def _migrate(environ: dict[str, str], direction: str, revision: str) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "alembic", direction, revision],
        env=environ,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_filter_migration_is_independent_reversible_and_merges_at_head() -> None:
    owner_url = os.environ.get("EC_MIGRATION_URL")
    app_url = os.environ.get("EC_DATABASE_URL")
    if owner_url is None or app_url is None:
        pytest.skip("isolated PostgreSQL integration runner required")
    run_id = uuid4().hex
    database = isolated_database_name(run_id)
    child = {
        **os.environ,
        "EC_MIGRATION_URL": replace_database(owner_url, database),
        "EC_DATABASE_URL": replace_database(app_url, database),
        "EC_INTEGRATION_TEST_RUN_ID": run_id,
    }
    validate_isolated_test_environment(child)
    _create_database(owner_url, database)
    engine = create_engine(child["EC_MIGRATION_URL"])
    try:
        _migrate(child, "upgrade", "0208")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0208"
            )
        _migrate(child, "upgrade", "0210")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0210"
            )
            assert (
                connection.execute(
                    text("SELECT to_regclass('public.muse_connections')")
                ).scalar_one()
                is None
            )
            for signature in (
                "public.fn_list_retained_catalog_browse_observations_v1(text,timestamptz,timestamptz)",
                "public.fn_list_retained_catalog_browse_observations_v2(text[],timestamptz,timestamptz)",
            ):
                definition = connection.execute(
                    text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
                    {"signature": signature},
                ).scalar_one()
                assert "event.start_at >= p_window_start" in definition
                assert "event.start_at < p_window_end" in definition
            recommendation = "public.fn_list_retained_catalog_recommendation_observations_v1(text[],timestamptz,timestamptz)"
            definition = connection.execute(
                text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
                {"signature": recommendation},
            ).scalar_one()
            assert "> coalesce(p_window_start, statement_timestamp())" in definition
            assert "SECURITY DEFINER" in definition
            assert "SET search_path TO 'pg_catalog', 'public'" in definition
            assert connection.execute(
                text("SELECT has_function_privilege('ec_app', :signature, 'EXECUTE')"),
                {"signature": recommendation},
            ).scalar_one()
            assert not connection.execute(
                text("SELECT has_function_privilege('public', :signature, 'EXECUTE')"),
                {"signature": recommendation},
            ).scalar_one()
        _migrate(child, "downgrade", "0208")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT to_regprocedure(:signature)"), {"signature": recommendation}
                ).scalar_one()
                is None
            )
            for signature in (
                "public.fn_list_retained_catalog_browse_observations_v1(text,timestamptz,timestamptz)",
                "public.fn_list_retained_catalog_browse_observations_v2(text[],timestamptz,timestamptz)",
            ):
                definition = connection.execute(
                    text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
                    {"signature": signature},
                ).scalar_one()
                assert "> coalesce(p_window_start, statement_timestamp())" in definition
        _migrate(child, "upgrade", "0209")
        _migrate(child, "upgrade", "0210")
        with engine.connect() as connection:
            assert set(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalars()
            ) == {"0209", "0210"}
        _migrate(child, "upgrade", "head")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0211"
            )
            assert (
                connection.execute(
                    text("SELECT to_regclass('public.muse_connections')")
                ).scalar_one()
                is not None
            )
    finally:
        engine.dispose()
        _drop_database(owner_url, database)
