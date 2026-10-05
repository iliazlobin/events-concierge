"""A published application database keeps accounting when the operator branch joins."""

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection
from tests.support.integration_database import isolated_database_name, replace_database
from tests.support.run_isolated_integration import _create_database, _drop_database

pytestmark = pytest.mark.integration


def test_upgrade_from_published_head_preserves_measured_usage_and_budget_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_url = os.environ.get("EC_MIGRATION_URL")
    if not owner_url:
        pytest.skip("use the isolated integration runner")
    database = isolated_database_name(uuid4().hex)
    url = replace_database(owner_url, database)
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    created = False
    engine = create_engine(url, hide_parameters=True)
    try:
        _create_database(owner_url, database)
        created = True
        monkeypatch.setenv("EC_MIGRATION_URL", url)
        command.upgrade(config, "0201")
        call_id = uuid4()
        with engine.begin() as connection:
            connection.execute(
                text("""
                    INSERT INTO public.model_usage_calls
                        (call_id,requested_model,status,completed_at,cost_usd)
                    VALUES (:id,'fixture/model','ok',clock_timestamp(),0.125)
                """),
                {"id": call_id},
            )
            connection.execute(
                text(
                    "SELECT public.fn_update_model_budget_v1(CAST(:settings AS jsonb), 'fixture-reviewer')"
                ),
                {
                    "settings": json.dumps(
                        {
                            "expected_revision": 1,
                            "mode": "enforce",
                            "daily_limit_usd": "4",
                            "monthly_limit_usd": None,
                            "alert_percent": 80,
                        }
                    )
                },
            )
            before = _accounting_snapshot(connection)
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
                == "0202"
            )
            assert _accounting_snapshot(connection) == before
            assert (
                connection.execute(
                    text(
                        "SELECT has_function_privilege('ec_app','public.fn_get_operator_record_diagnostic_v1(text,text,text)','EXECUTE')"
                    )
                ).scalar_one()
                is False
            )
    finally:
        engine.dispose()
        if created:
            _drop_database(owner_url, database)


def _accounting_snapshot(connection: Connection) -> object:
    return connection.execute(
        text("""
            SELECT jsonb_build_object(
                'calls',(SELECT jsonb_agg(to_jsonb(c)) FROM public.model_usage_calls c),
                'budget',(SELECT jsonb_agg(to_jsonb(b)) FROM public.model_usage_budget b),
                'audit',(SELECT jsonb_agg(to_jsonb(a)) FROM public.model_usage_budget_audit a))
        """)
    ).scalar_one()
