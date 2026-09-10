"""Real role, revision, immutable-window and retained-catalog membership contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.integration.test_command_investigation import _scope
from tests.integration.test_operator_management import _denied, _owner_transaction, _role, _source

from events_concierge.adapters.postgres.ingestion_admin import PostgresIngestionAdminRepository
from events_concierge.api.admin import IngestionRunOut, IngestionSourceDetailOut

pytestmark = pytest.mark.integration

_CONFIG = """
SELECT * FROM public.fn_update_ingestion_admin_source_configuration_v3(
 :source,:revision,:url,ARRAY['https://events.example.com'],'public_jsonld',true,true,NULL,
 1440,1500,1,'reviewed-operator',:days
)
"""
_PREPARE = "SELECT public.fn_prepare_catalog_collection_window_v1(:source,:run,:token,:revision)"


async def _configure(
    connection: AsyncConnection, source: str, revision: int, days: int
) -> RowMapping:
    await _role(connection, "ec_operator_controller")
    return (
        (
            await connection.execute(
                text(_CONFIG),
                {
                    "source": source,
                    "revision": revision,
                    "url": f"https://events.example.com/{source}",
                    "days": days,
                },
            )
        )
        .mappings()
        .one()
    )


async def _claim(connection: AsyncConnection, source: str, run: str) -> UUID:
    await _role(connection, "ec_ingestion_executor")
    token = uuid4()
    outcome = (
        await connection.execute(
            text("SELECT public.fn_claim_catalog_refresh(:source,:run,600,:token)"),
            {"source": source, "run": run, "token": token},
        )
    ).scalar_one()
    assert outcome == "acquired"
    return token


async def _prepare(
    connection: AsyncConnection, source: str, run: str, token: UUID | None, revision: int | None
) -> dict[str, Any] | None:
    await _role(connection, "ec_ingestion_executor")
    return cast(
        dict[str, Any] | None,
        (
            await connection.execute(
                text(_PREPARE), {"source": source, "run": run, "token": token, "revision": revision}
            )
        ).scalar_one(),
    )


async def test_horizon_configuration_is_audited_revision_fenced_and_legacy_writes_preserve_it() -> (
    None
):
    async with _owner_transaction() as connection:
        source = await _source(connection)
        for invalid in (0, 91, -1):
            assert (await _configure(connection, source, 1, invalid))["outcome"] == "invalid"
        assert (await _configure(connection, source, 2, 30))["outcome"] == "conflict"
        changed = await _configure(connection, source, 1, 30)
        assert changed["outcome"] == "updated" and changed["source_revision"] == 2
        viewer = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        detail = await viewer.get_source_detail(source, window_hours=168, bucket_hours=24)
        assert detail is not None and detail.source.collection_horizon_days == 30
        assert IngestionSourceDetailOut.model_validate(detail).source.collection_horizon_days == 30
        await _role(connection, "ec_operator_controller")
        legacy = _CONFIG.replace("_v3(", "_v2(").replace(",:days\n", "\n")
        result = (
            (
                await connection.execute(
                    text(legacy),
                    {
                        "source": source,
                        "revision": 2,
                        "url": f"https://events.example.com/{source}",
                    },
                )
            )
            .mappings()
            .one()
        )
        assert result["outcome"] == "updated" and result["source_revision"] == 3
        await _role(connection, None)
        audits = (
            (
                await connection.execute(
                    text("""
            SELECT prior_revision,new_revision,before_config->>'collection_horizon_days' AS before,
                   after_config->>'collection_horizon_days' AS after
            FROM public.catalog_source_configuration_audit WHERE source_key=:source ORDER BY audit_id
        """),
                    {"source": source},
                )
            )
            .mappings()
            .all()
        )
        assert [
            (row["prior_revision"], row["new_revision"], row["before"], row["after"])
            for row in audits
        ] == [(1, 2, "90", "30"), (2, 3, "30", "30")]
        await connection.execute(
            text("UPDATE catalog_sources SET collection_horizon_days=14 WHERE source_key=:source"),
            {"source": source},
        )
        assert (
            await connection.execute(
                text("SELECT source_revision FROM catalog_sources WHERE source_key=:source"),
                {"source": source},
            )
        ).scalar_one() == 4


async def test_window_requires_current_lease_and_revision_then_survives_retry_configuration_edits() -> (
    None
):
    async with _owner_transaction() as connection:
        source = await _source(connection)
        run = f"cadence:{source}:window"
        token = await _claim(connection, source, run)
        for bad_token, bad_revision in ((None, 1), (uuid4(), 1), (token, None), (token, 2)):
            assert await _prepare(connection, source, run, bad_token, bad_revision) is None
        await _role(connection, None)
        assert (
            await connection.execute(
                text(
                    "SELECT count(*) FROM catalog_refresh_collection_windows WHERE source_key=:source"
                ),
                {"source": source},
            )
        ).scalar_one() == 0
        first = await _prepare(connection, source, run, token, 1)
        assert first is not None
        assert (
            first["source_revision"] == 1
            and first["horizon_days"] == 90
            and first["attempt_count"] == 1
        )
        assert datetime.fromisoformat(first["end_at"]) - datetime.fromisoformat(
            first["start_at"]
        ) == timedelta(days=90)
        assert await _prepare(connection, source, run, token, 1) == first
        assert (await _configure(connection, source, 1, 30))["outcome"] == "updated"
        # Same attempt cannot be relabeled with the changed settings.
        assert await _prepare(connection, source, run, token, 2) is None
        await _role(connection, "ec_ingestion_executor")
        assert (
            await connection.execute(
                text("SELECT public.fn_fail_catalog_refresh(:source,:run,:token,'source timeout')"),
                {"source": source, "run": run, "token": token},
            )
        ).scalar_one()
        new_token = await _claim(connection, source, run)
        retry = await _prepare(connection, source, run, new_token, 2)
        assert retry is not None
        assert {
            key: retry[key] for key in ("start_at", "end_at", "horizon_days", "source_revision")
        } == {key: first[key] for key in ("start_at", "end_at", "horizon_days", "source_revision")}
        assert retry["attempt_count"] == 2
        viewer = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        found = await viewer.lookup_run(source, run)
        assert found is not None and found.source_revision == 2
        assert (
            found.collection_window is not None
            and found.collection_window.window_source_revision == 1
        )
        assert (
            found.execution_configuration is not None
            and found.execution_configuration.collection_horizon_days == 30
        )
        assert (
            found.source_configuration is not None
            and found.source_configuration.collection_horizon_days == 30
        )
        serialized = IngestionRunOut.model_validate(found)
        assert (
            serialized.collection_window is not None
            and serialized.collection_window.horizon_days == 90
        )
        detail = await viewer.get_source_detail(source, window_hours=168, bucket_hours=24)
        assert detail is not None and detail.source.latest_run is not None
        assert detail.source.latest_run.collection_window == found.collection_window
        assert detail.recent_runs[0].execution_configuration == found.execution_configuration
        await _role(connection, None)
        snapshots = (
            await connection.execute(
                text(
                    "SELECT attempt_count,source_revision FROM catalog_refresh_execution_configurations WHERE source_key=:source ORDER BY attempt_count"
                ),
                {"source": source},
            )
        ).all()
        assert [tuple(row) for row in snapshots] == [(1, 1), (2, 2)]


async def test_expired_lease_and_legacy_staged_cursor_cannot_create_window_evidence() -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        run = f"cadence:{source}:legacy"
        token = await _claim(connection, source, run)
        await _role(connection, None)
        await connection.execute(
            text("""
            INSERT INTO catalog_refresh_progress(source_key,run_key,source_revision,window_start_day,page_limit,next_page,staged_raw_count)
            VALUES(:source,:run,1,current_date,1,1,1)
        """),
            {"source": source, "run": run},
        )
        assert await _prepare(connection, source, run, token, 1) == {
            "outcome": "legacy_stage_requires_new_run"
        }
        await _role(connection, None)
        assert (
            await connection.execute(
                text("SELECT count(*) FROM catalog_refresh_progress WHERE source_key=:source"),
                {"source": source},
            )
        ).scalar_one() == 1
        await connection.execute(
            text(
                "UPDATE catalog_refresh_runs SET lease_expires_at=clock_timestamp()-interval '1 second' WHERE source_key=:source"
            ),
            {"source": source},
        )
        assert await _prepare(connection, source, run, token, 1) is None
        await _role(connection, None)
        assert (
            await connection.execute(
                text(
                    "SELECT count(*) FROM catalog_refresh_collection_windows WHERE source_key=:source"
                ),
                {"source": source},
            )
        ).scalar_one() == 0


async def test_new_window_capabilities_keep_runtime_roles_separate_and_legacy_runs_unknown() -> (
    None
):
    async with _owner_transaction() as connection:
        source = await _source(connection)
        for role in ("ec_app", "ec_operator_viewer", "ec_ingestion_executor"):
            await _role(connection, role)
            await _denied(
                connection,
                "SELECT * FROM public.fn_update_ingestion_admin_source_configuration_v3(NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL)",
            )
        for role in ("ec_app", "ec_operator_viewer", "ec_operator_controller"):
            await _role(connection, role)
            await _denied(
                connection,
                "SELECT public.fn_prepare_catalog_collection_window_v1(NULL,NULL,NULL,NULL)",
            )
        for role in (
            "ec_app",
            "ec_operator_viewer",
            "ec_operator_controller",
            "ec_ingestion_executor",
        ):
            await _role(connection, role)
            await _denied(connection, "SELECT * FROM public.catalog_refresh_collection_windows")
            await _denied(
                connection, "SELECT * FROM public.catalog_refresh_execution_configurations"
            )
        token = await _claim(connection, source, f"cadence:{source}:legacy-no-window")
        assert token
        viewer = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        run = await viewer.lookup_run(source, f"cadence:{source}:legacy-no-window")
        assert (
            run is not None
            and run.collection_window is None
            and run.execution_configuration is None
        )


async def test_expired_frozen_window_requires_fresh_run_and_keeps_existing_evidence() -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        defaults = (
            await connection.execute(
                text("""
            SELECT a.attname,pg_get_expr(d.adbin,d.adrelid) FROM pg_attribute a
            JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum
            WHERE a.attrelid='public.catalog_sources'::regclass
              AND a.attname IN ('refresh_interval_minutes','collection_horizon_days')
        """)
            )
        ).all()
        assert {str(row[0]): str(row[1]) for row in defaults} == {
            "refresh_interval_minutes": "1440",
            "collection_horizon_days": "90",
        }
        run = f"cadence:{source}:expired"
        token = await _claim(connection, source, run)
        assert await _prepare(connection, source, run, token, 1) is not None
        await _role(connection, None)
        await connection.execute(
            text("""
            UPDATE catalog_refresh_collection_windows SET start_at=start_at-interval '100 days',
                end_at=end_at-interval '100 days' WHERE source_key=:source
        """),
            {"source": source},
        )
        assert await _prepare(connection, source, run, uuid4(), 1) is None
        assert await _prepare(connection, source, run, token, 1) == {
            "outcome": "collection_window_expired"
        }
        await _role(connection, None)
        assert (
            await connection.execute(
                text(
                    "SELECT count(*) FROM catalog_refresh_execution_configurations WHERE source_key=:source"
                ),
                {"source": source},
            )
        ).scalar_one() == 1


async def _successful_run(
    connection: AsyncConnection,
    source: str,
    key: str,
    completed: datetime,
    start: datetime | None,
    end: datetime | None,
    *,
    status: str = "succeeded",
) -> None:
    await _role(connection, None)
    await connection.execute(
        text("""
        INSERT INTO catalog_refresh_runs(source_key,run_key,status,started_at,completed_at,attempt_count)
        VALUES(:source,:run,:status,:completed-interval '1 minute',:completed,1)
    """),
        {"source": source, "run": key, "status": status, "completed": completed},
    )
    if start is not None:
        await connection.execute(
            text("""
            INSERT INTO catalog_refresh_collection_windows(source_key,run_key,source_revision,horizon_days,start_at,end_at)
            VALUES(:source,:run,1,:days,:start,:end)
        """),
            {
                "source": source,
                "run": key,
                "start": start,
                "end": end,
                "days": (end - start).days if end is not None else 90,
            },
        )


async def test_narrow_window_retains_outside_events_without_resurrecting_covered_omissions() -> (
    None
):
    async with _owner_transaction() as connection:
        source = await _source(connection)
        now = datetime.now(UTC)
        original = f"cadence:{source}:original"
        await _successful_run(connection, source, original, now - timedelta(hours=4), None, None)
        event = uuid4()
        await connection.execute(
            text(
                "INSERT INTO canonical_events(canonical_event_id,title,start_at,description,price_status) VALUES(:id,'Future observed event',:at,'Public event','free')"
            ),
            {"id": event, "at": now + timedelta(days=60)},
        )
        await connection.execute(
            text("""
            INSERT INTO catalog_event_observations(source_key,source,source_event_id,canonical_event_id,registration_url,price_status,content_hash,last_run_key)
            VALUES(:source,'public_jsonld','window-test-event',:id,'https://events.example.com/event','free',:hash,:run)
        """),
            {"source": source, "id": event, "hash": "a" * 64, "run": original},
        )
        await _successful_run(
            connection,
            source,
            f"cadence:{source}:narrow",
            now - timedelta(hours=3),
            now,
            now + timedelta(days=30),
        )

        async def visible() -> tuple[int, int, int]:
            await _role(connection, "ec_operator_viewer")
            report = (
                await connection.execute(
                    text(
                        "SELECT live_future_events,retracted_future_events FROM public.fn_report_catalog_source_coverage_v1() WHERE source_key=:source"
                    ),
                    {"source": source},
                )
            ).one()
            await _role(connection, None)
            rows = (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM public.fn_list_retained_catalog_browse_observations_v2(ARRAY[:source]::text[],NULL,NULL)"
                    ),
                    {"source": source},
                )
            ).scalar_one()
            return report[0], report[1], rows

        assert await visible() == (1, 0, 1)
        # Newer fixture evidence cannot replace real catalog membership.
        await _successful_run(
            connection,
            source,
            "manual:p123-fixture-success",
            now - timedelta(hours=2, minutes=30),
            None,
            None,
        )
        assert await visible() == (1, 0, 1)
        # Failed full coverage never supersedes retained success.
        await _successful_run(
            connection,
            source,
            f"cadence:{source}:failed-wide",
            now - timedelta(hours=2),
            now,
            now + timedelta(days=90),
            status="failed",
        )
        assert await visible() == (1, 0, 1)
        # A successful covering omission retracts the event. A later narrow success
        # must not resurrect the older observation, even though it cannot cover day60.
        await _successful_run(
            connection,
            source,
            f"cadence:{source}:wide-omission",
            now - timedelta(hours=1),
            now,
            now + timedelta(days=90),
        )
        await _successful_run(
            connection,
            source,
            f"cadence:{source}:new-narrow",
            now - timedelta(minutes=30),
            now,
            now + timedelta(days=30),
        )
        assert await visible() == (0, 1, 0)
        # Equal completion timestamps resolve by run key, identically in browse
        # and coverage, rather than depending on heap insertion or query plans.
        tied_at = now - timedelta(minutes=5)
        for suffix in ("a-tied", "z-tied"):
            await _successful_run(
                connection, source, f"cadence:{source}:{suffix}", tied_at, None, None
            )
        await connection.execute(
            text(
                "UPDATE catalog_event_observations SET last_run_key=:run WHERE source_key=:source"
            ),
            {"run": f"cadence:{source}:z-tied", "source": source},
        )
        assert await visible() == (1, 0, 1)
        # Legacy success without a snapshot retains the previous whole-run membership rule.
        await _successful_run(
            connection,
            source,
            f"cadence:{source}:legacy-success",
            now - timedelta(minutes=1),
            None,
            None,
        )
        assert await visible() == (0, 1, 0)
