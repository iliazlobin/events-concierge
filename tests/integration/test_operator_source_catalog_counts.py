"""Source counts agree with Catalog before paging and retain operator-only authority."""

from __future__ import annotations

from importlib import import_module
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError
from tests.integration.test_command_investigation import _scope
from tests.integration.test_operator_catalog_records import _read
from tests.integration.test_operator_management import _denied, _owner_transaction, _role, _source
from tests.integration.test_operator_run_catalog import _event, _run

from events_concierge.adapters.postgres.ingestion_admin import PostgresIngestionAdminRepository
from events_concierge.api.admin import IngestionSourceConfigurationOut, IngestionSourceOut

pytestmark = pytest.mark.integration
_SIGNATURE = "public.fn_list_ingestion_admin_sources_v7(text,text,text,text,text,text,boolean,text,text,integer,integer)"
_CALL = "SELECT * FROM public.fn_list_ingestion_admin_sources_v7(NULL,'all',NULL,NULL,NULL,NULL,false,'catalog','desc',10,0)"


async def test_counts_match_catalog_distinct_inventory_and_detail() -> None:
    async with _owner_transaction() as connection:
        source, other = await _source(connection), await _source(connection)
        run, fixture_run = f"manual:{uuid4()}", f"manual:{uuid4()}"
        await _run(connection, source, run)
        await _run(connection, other, run)
        await _run(connection, source, fixture_run, "fixture retained run")
        past = await _event(connection, source, run, "Past", at="2001-01-01Z")
        future = await _event(connection, source, run, "Future")
        ongoing = await _event(connection, source, run, "Ongoing", at="2001-01-01Z")
        cancelled = await _event(connection, source, run, "Cancelled")
        await _event(connection, source, run, "Same canonical duplicate", identifier=future)
        await _event(connection, other, run, "Shared across sources", identifier=past)
        await _event(connection, source, fixture_run, "Fixture provenance")
        await connection.execute(
            text(
                "UPDATE public.canonical_events SET end_at='2099-01-01Z' WHERE canonical_event_id=:id"
            ),
            {"id": ongoing},
        )
        await connection.execute(
            text(
                "UPDATE public.canonical_events SET event_status='cancelled' WHERE canonical_event_id=:id"
            ),
            {"id": cancelled},
        )
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        for key, expected in ((source, (4, 3)), (other, (1, 0))):
            for include_fixtures in (False, True):
                row = (
                    await repo.list_sources(source_key=key, include_fixtures=include_fixtures)
                ).items[0]
                catalog = await _read(repo, source_key=key)
                upcoming = await _read(repo, source_key=key, date_scope="upcoming")
                assert (row.total_event_count, row.upcoming_event_count) == expected
                assert row.total_event_count == catalog.total
                assert row.upcoming_event_count == catalog.upcoming_total == upcoming.total
                projected = IngestionSourceOut.model_validate(row)
                assert (projected.total_event_count, projected.upcoming_event_count) == expected
                detail = await repo.get_source_detail(
                    key, window_hours=24, bucket_hours=1, include_fixtures=include_fixtures
                )
                assert detail is not None
                detail_out = IngestionSourceConfigurationOut.model_validate(detail.source)
                assert (detail_out.total_event_count, detail_out.upcoming_event_count) == expected
                assert detail.source.event_count == row.event_count
        # Discovery event_count keeps its separate start-time/non-cancelled contract.
        row = (await repo.list_sources(source_key=source)).items[0]
        assert row.event_count == 1


async def test_count_sorts_cover_all_matching_sources_before_page_boundary() -> None:
    async with _owner_transaction() as connection:
        publisher = f"Inventory {uuid4()}"
        sources = [await _source(connection) for _ in range(4)]
        await connection.execute(
            text(
                "UPDATE public.catalog_sources SET publisher=:publisher WHERE source_key=ANY(CAST(:keys AS text[]))"
            ),
            {"publisher": publisher, "keys": sources},
        )
        # Counts deliberately disagree with alphabetic order and with each other.
        counts = [(1, 1), (4, 0), (3, 2), (0, 0)]
        for index, (source, (total, upcoming)) in enumerate(zip(sources, counts, strict=True)):
            run = f"manual:{uuid4()}"
            await _run(connection, source, run)
            await connection.execute(
                text(
                    "UPDATE public.catalog_sources SET display_name=:name, enabled=:enabled WHERE source_key=:key"
                ),
                {"name": f"Inventory {index}", "enabled": index != 1, "key": source},
            )
            for event in range(total):
                await _event(
                    connection,
                    source,
                    run,
                    f"Event {event}",
                    at="2099-01-01Z" if event < upcoming else "2001-01-01Z",
                )
        await connection.execute(
            text(
                "UPDATE public.catalog_sources SET enabled=false, retired_at=statement_timestamp(), retired_reason='Replaced' WHERE source_key=:key"
            ),
            {"key": sources[2]},
        )
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        for sort_by, index in (("catalog_total", 0), ("catalog", 1)):
            for direction in ("asc", "desc"):
                ordered = sorted(
                    zip(sources, counts, strict=True),
                    key=lambda item: ((-1 if direction == "desc" else 1) * item[1][index], item[0]),
                )
                for offset, (source, _) in enumerate(ordered):
                    page = await repo.list_sources(
                        publisher=publisher,
                        sort_by=sort_by,
                        sort_direction=direction,
                        limit=1,
                        offset=offset,
                    )
                    assert page.total == 4 and len(page.items) == 1
                    assert page.items[0].source_key == source
                beyond = await repo.list_sources(
                    publisher=publisher, sort_by=sort_by, limit=1, offset=4
                )
                assert beyond.total == 4 and not beyond.items
        retired = (await repo.list_sources(source_key=sources[2])).items[0]
        assert (
            retired.effective_status.value == "retired" and not retired.due and not retired.enabled
        )
        assert retired.total_event_count == 3 and retired.upcoming_event_count == 2


async def test_fixture_sources_have_zero_catalog_inventory_and_operator_roles_stay_narrow() -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        run = f"manual:{uuid4()}"
        await _run(connection, source, run)
        await _event(connection, source, run, "Fixture source record")
        await connection.execute(
            text("UPDATE public.catalog_sources SET publisher='Tests' WHERE source_key=:key"),
            {"key": source},
        )
        for role in ("ec_operator_viewer", "ec_operator_controller"):
            repo = PostgresIngestionAdminRepository(session_scope=_scope(connection, role))
            assert not (await repo.list_sources(source_key=source)).items
            row = (await repo.list_sources(source_key=source, include_fixtures=True)).items[0]
            assert row.total_event_count == row.upcoming_event_count == 0
            detail = await repo.get_source_detail(
                source, window_hours=24, bucket_hours=1, include_fixtures=True
            )
            assert (
                detail is not None
                and detail.source.total_event_count == detail.source.upcoming_event_count == 0
            )
            await _role(connection, role)
            for table in ("canonical_events", "catalog_event_observations", "catalog_refresh_runs"):
                await _denied(connection, f"SELECT * FROM public.{table} LIMIT 1")
        await _role(connection, None)
        info = (
            await connection.execute(
                text("""
            SELECT r.rolname,p.prosecdef,p.proconfig FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner
            WHERE p.oid=CAST(:signature AS regprocedure)
        """),
                {"signature": _SIGNATURE},
            )
        ).one()
        assert info.rolname == "ec_operator_aggregate_definer" and info.prosecdef
        assert "search_path=pg_catalog, public" in info.proconfig
        for role in ("ec_app", "ec_ingestion_executor"):
            await _role(connection, role)
            await _denied(connection, _CALL)


@pytest.mark.parametrize("sort,limit", [("seed_url", 10), ("catalog_total", 0), ("catalog", 101)])
async def test_count_projection_validates_sort_and_page(sort: str, limit: int) -> None:
    async with _owner_transaction() as connection:
        await _role(connection, "ec_operator_viewer")
        with pytest.raises(DBAPIError, match="ingestion admin source query is invalid"):
            await connection.execute(
                text("""
                SELECT * FROM public.fn_list_ingestion_admin_sources_v7(
                    NULL,'all',NULL,NULL,NULL,NULL,false,:sort,'asc',:limit,0
                )
            """),
                {"sort": sort, "limit": limit},
            )


async def test_count_projection_downgrade_upgrade_restores_legacy_and_role_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = import_module("migrations.versions.0193_operator_source_catalog_counts")
    async with _owner_transaction() as connection:

        def apply(sync: Connection, action: str) -> None:
            monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(sync)))
            getattr(migration, action)()

        await connection.run_sync(apply, "downgrade")
        assert (
            await connection.execute(
                text("SELECT to_regprocedure(:signature)"), {"signature": _SIGNATURE}
            )
        ).scalar_one() is None
        await _role(connection, "ec_operator_viewer")
        await connection.execute(text(_CALL.replace("_v7", "_v6")))
        await _role(connection, None)
        await connection.run_sync(apply, "upgrade")
        for role in ("ec_operator_viewer", "ec_operator_controller"):
            await _role(connection, role)
            await connection.execute(text(_CALL))
        for role in ("ec_app", "ec_ingestion_executor"):
            await _role(connection, role)
            await _denied(connection, _CALL)
