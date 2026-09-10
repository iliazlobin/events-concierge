"""Filtered operator inventory, coherent deduplicated provenance and narrow DB authority."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from tests.integration.test_command_investigation import _scope
from tests.integration.test_operator_management import _denied, _owner_transaction, _role, _source
from tests.integration.test_operator_run_catalog import _event, _run

from events_concierge.adapters.postgres.ingestion_admin import PostgresIngestionAdminRepository
from events_concierge.api.admin import IngestionCatalogRecordPageOut

pytestmark = pytest.mark.integration
_SIGNATURE = (
    "public.fn_get_operator_catalog_records_v1(text,text,text,text,text,timestamptz,uuid,integer)"
)


async def _read(repo: PostgresIngestionAdminRepository, **overrides: object):
    options = {
        "source_key": None,
        "run_key": None,
        "query": None,
        "date_scope": "all",
        "price_status": "all",
        "after_start_at": None,
        "after_canonical_event_id": None,
        "limit": 20,
    }
    options.update(overrides)
    return await repo.browse_catalog_records(**options)


async def test_global_inventory_deduplicates_after_scope_and_keeps_provenance_coherent() -> None:
    async with _owner_transaction() as connection:
        a, b = await _source(connection), await _source(connection)
        first, second = f"manual:{uuid4()}", f"manual:{uuid4()}"
        prefix = uuid4().hex
        for source, run in ((a, first), (a, second), (b, second)):
            await _run(connection, source, run)
        shared = await _event(connection, a, first, f"{prefix} Shared", at="2001-01-01Z")
        await _event(connection, b, second, "duplicate", identifier=shared)
        later = await _event(connection, a, second, f"{prefix} Later")
        await _role(connection, None)
        await connection.execute(
            text("""
            UPDATE public.catalog_event_observations
            SET last_seen_at=CASE WHEN source_key=:source THEN '2026-01-01Z'::timestamptz
                                 ELSE '2026-02-01Z'::timestamptz END,
                registration_url=CASE WHEN source_key=:source THEN 'https://events.example.com/first'
                                      ELSE 'https://events.example.com/second' END
            WHERE canonical_event_id=:id
        """),
            {"source": a, "id": shared},
        )
        await connection.execute(
            text(
                "UPDATE public.catalog_sources SET display_name='Second source' WHERE source_key=:source"
            ),
            {"source": b},
        )
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        page = await _read(repo, query=prefix, limit=1)
        assert page.total == 2 and page.has_more
        assert page.upcoming_total == 1 and page.source_count == 2
        assert not page.source_counts_truncated
        assert {entry.source_key: entry.events for entry in page.source_counts} == {a: 2, b: 1}
        record = page.items[0]
        assert record.canonical_event_id == shared and record.source_key == b
        assert record.source_display_name == "Second source" and record.refresh_run_key == second
        assert record.registration_url == "https://events.example.com/second"
        assert page.source_key is None and page.run_key is None
        assert page.generated_at.utcoffset() is not None
        second_page = await _read(
            repo,
            query=prefix,
            limit=1,
            after_start_at=page.next_start_at,
            after_canonical_event_id=page.next_canonical_event_id,
        )
        assert second_page.total == 2 and not second_page.has_more
        assert second_page.source_counts == page.source_counts
        assert [row.canonical_event_id for row in second_page.items] == [later]
        scoped = await _read(repo, source_key=a, run_key=first, query=prefix)
        assert scoped.total == 1 and scoped.source_key == a and scoped.run_key == first
        assert scoped.upcoming_total == 0 and scoped.source_count == 1
        assert scoped.source_counts[0].source_key == a and scoped.source_counts[0].events == 1
        assert scoped.items[0].source_key == a and scoped.items[0].refresh_run_key == first
        assert scoped.items[0].registration_url == "https://events.example.com/first"
        assert (await _read(repo, source_key=b, run_key=first, query=prefix)).total == 0
        searched = await _read(repo, query=f"{prefix} Later")
        assert searched.total == 1 and searched.upcoming_total == 1
        assert searched.source_count == 1 and searched.source_counts[0].events == 1
        dumped = IngestionCatalogRecordPageOut.model_validate(page).model_dump_json()
        assert not any(private in dumped for private in ("content_hash", "lease_token", "error"))


async def test_date_and_price_filters_include_ongoing_and_retained_cancelled_records() -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        run = f"manual:{uuid4()}"
        await _run(connection, source, run)
        past = await _event(connection, source, run, "Past", at="2001-01-01Z")
        future = await _event(connection, source, run, "Future")
        ongoing = await _event(connection, source, run, "Ongoing", at="2001-01-01Z")
        await connection.execute(
            text("""
            UPDATE public.canonical_events SET price_status='paid',price_min_cents=1000,
                price_max_cents=1000,price_currency='USD',event_status='cancelled' WHERE canonical_event_id=:id
        """),
            {"id": future},
        )
        await connection.execute(
            text("""
            UPDATE public.canonical_events SET price_status='unknown',end_at='2099-01-01Z'
            WHERE canonical_event_id=:id
        """),
            {"id": ongoing},
        )
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )
        assert (await _read(repo, source_key=source)).total == 3
        assert (await _read(repo, source_key=source)).upcoming_total == 2
        assert {
            item.canonical_event_id
            for item in (await _read(repo, source_key=source, date_scope="upcoming")).items
        } == {ongoing, future}
        assert [
            item.canonical_event_id
            for item in (await _read(repo, source_key=source, date_scope="past")).items
        ] == [past]
        for price, identifier in (("free", past), ("paid", future), ("unknown", ongoing)):
            page = await _read(repo, source_key=source, price_status=price)
            assert page.total == 1 and page.price_status == price
            assert page.items[0].canonical_event_id == identifier
            assert page.upcoming_total == (0 if price == "free" else 1)
            assert page.source_counts[0].events == 1
        assert (
            await _read(repo, source_key=source, date_scope="past", price_status="paid")
        ).total == 0


async def test_inventory_fixture_exclusion_and_operator_only_authority() -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        run = f"manual:{uuid4()}"
        prefix = uuid4().hex
        await _run(connection, source, run, "fixture private provider details")
        await _event(connection, source, run, prefix)
        for role in ("ec_operator_viewer", "ec_operator_controller"):
            repo = PostgresIngestionAdminRepository(session_scope=_scope(connection, role))
            assert (await _read(repo, query=prefix)).total == 0
            await _role(connection, role)
            for table in (
                "canonical_events",
                "catalog_event_observations",
                "catalog_refresh_runs",
                "event_requests",
            ):
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
            await _denied(connection, "SELECT public.fn_get_operator_catalog_records_v1()")


@pytest.mark.parametrize(
    "args",
    [
        "NULL,'manual:one'",
        "'x'",
        "'city-events',''",
        "'city-events',repeat('x',257)",
        "NULL,NULL,NULL,'yesterday'",
        "NULL,NULL,NULL,'all','cheap'",
        "NULL,NULL,NULL,'all','all','2001-01-01Z',NULL,20",
        "NULL,NULL,NULL,'all','all',NULL,NULL,101",
    ],
)
async def test_sql_rejects_invalid_or_partial_scope(args: str) -> None:
    async with _owner_transaction() as connection:
        await _role(connection, "ec_operator_viewer")
        with pytest.raises(DBAPIError, match="invalid operator catalog query"):
            await connection.execute(
                text(f"SELECT public.fn_get_operator_catalog_records_v1({args})")
            )
