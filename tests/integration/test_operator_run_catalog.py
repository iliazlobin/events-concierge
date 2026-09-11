"""Current publication attribution, keyset scopes and role isolation in a disposable DB."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection
from tests.integration.test_command_investigation import _scope
from tests.integration.test_operator_management import _denied, _owner_transaction, _role, _source

from events_concierge.adapters.postgres.ingestion_admin import PostgresIngestionAdminRepository
from events_concierge.api.admin import IngestionCatalogEventPageOut

pytestmark = pytest.mark.integration
_SIGNATURE = (
    "public.fn_get_operator_run_catalog_records_v1(text,text,text,timestamptz,uuid,integer)"
)


async def _run(
    connection: AsyncConnection, source: str, key: str, error: str | None = None
) -> None:
    await connection.execute(
        text("""
        INSERT INTO public.catalog_refresh_runs(source_key,run_key,status,started_at,completed_at,
            candidate_count,canonical_count,error,attempt_count)
        VALUES(:source,:run,'succeeded','2026-01-01Z','2026-01-01T00:01:00Z',3,3,:error,1)
    """),
        {"source": source, "run": key, "error": error},
    )


async def _event(
    connection: AsyncConnection,
    source: str,
    run: str,
    title: str,
    *,
    at: str = "2099-01-01T00:00:00Z",
    identifier: UUID | None = None,
) -> UUID:
    identifier = identifier or uuid4()
    await connection.execute(
        text("""
        INSERT INTO public.canonical_events(canonical_event_id,title,start_at,description,price_status)
        VALUES(:id,:title,:at,'Public normalized description.','free')
        ON CONFLICT(canonical_event_id) DO NOTHING
    """),
        {"id": identifier, "title": title, "at": datetime.fromisoformat(at.replace("Z", "+00:00"))},
    )
    await connection.execute(
        text("""
        INSERT INTO public.catalog_event_observations(source_key,source,source_event_id,canonical_event_id,
            registration_url,price_status,content_hash,last_run_key)
        VALUES(:source,'public_jsonld',:event,:id,'https://events.example.com/public','free',:hash,:run)
    """),
        {"source": source, "event": uuid4().hex, "id": identifier, "run": run, "hash": "a" * 64},
    )
    return identifier


async def test_run_attribution_filters_before_count_and_paging_and_follows_current_provenance() -> (
    None
):
    async with _owner_transaction() as connection:
        source, other = await _source(connection), await _source(connection)
        first, second = f"manual:{uuid4()}", f"manual:{uuid4()}"
        for owner, run in [(source, first), (source, second), (other, first)]:
            await _run(connection, owner, run)
        a = await _event(connection, source, first, "Music A", at="2001-01-01T00:00:00Z")
        b = await _event(connection, source, first, "Music B")
        await _event(
            connection,
            source,
            first,
            "Music A duplicate observation",
            identifier=a,
            at="2001-01-01T00:00:00Z",
        )
        await _event(
            connection, source, second, "Different run, earlier", at="2000-01-01T00:00:00Z"
        )
        await _event(connection, other, first, "Different source, same run key")
        repo = PostgresIngestionAdminRepository(
            session_scope=_scope(connection, "ec_operator_viewer")
        )

        async def read(**overrides: object):
            return await repo.browse_run_events(
                source,
                first,
                query=None,
                after_start_at=None,
                after_canonical_event_id=None,
                limit=1,
                **overrides,
            )

        page = await read()
        assert page.source_total == 2 and page.has_more and page.run_key == first
        assert [item.canonical_event_id for item in page.items] == [a]
        projected = IngestionCatalogEventPageOut.model_validate(page).model_dump(mode="json")
        assert "lease_token" not in str(projected) and "content_hash" not in str(projected)
        next_page = await repo.browse_run_events(
            source,
            first,
            query=None,
            after_start_at=page.next_start_at,
            after_canonical_event_id=page.next_canonical_event_id,
            limit=1,
        )
        assert next_page.source_total == 2 and not next_page.has_more
        assert [item.canonical_event_id for item in next_page.items] == [b]
        search = await repo.browse_run_events(
            source,
            first,
            query="Music B",
            after_start_at=None,
            after_canonical_event_id=None,
            limit=1,
        )
        assert search.source_total == 1 and search.items[0].canonical_event_id == b
        await _role(connection, None)
        await connection.execute(
            text(
                "UPDATE public.catalog_event_observations SET last_run_key=:run WHERE source_key=:source AND canonical_event_id=:id"
            ),
            {"run": second, "source": source, "id": b},
        )
        after_refresh = await read()
        assert after_refresh.source_total == 1 and not after_refresh.has_more
        assert after_refresh.items[0].canonical_event_id == a
        assert asdict(after_refresh)["run_key"] == first


async def test_run_catalog_excludes_fixture_runs_and_does_not_grant_raw_operator_reads() -> None:
    async with _owner_transaction() as connection:
        source = await _source(connection)
        run = f"manual:{uuid4()}"
        await _run(connection, source, run, "fixture provider material must remain private")
        await _event(connection, source, run, "Fixture event")
        for role in ("ec_operator_viewer", "ec_operator_controller"):
            repo = PostgresIngestionAdminRepository(session_scope=_scope(connection, role))
            page = await repo.browse_run_events(
                source,
                run,
                query=None,
                after_start_at=None,
                after_canonical_event_id=None,
                limit=20,
            )
            assert page.source_total == 0 and not page.items
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
                text(
                    "SELECT r.rolname,p.prosecdef,p.proconfig FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner WHERE p.oid=CAST(:signature AS regprocedure)"
                ),
                {"signature": _SIGNATURE},
            )
        ).one()
        assert info.rolname == "ec_operator_aggregate_definer" and info.prosecdef
        assert "search_path=pg_catalog, public" in info.proconfig
        for role in ("ec_app", "ec_ingestion_executor"):
            await _role(connection, role)
            await _denied(
                connection,
                "SELECT public.fn_get_operator_run_catalog_records_v1('city-events','manual:one')",
            )


@pytest.mark.parametrize(
    "args",
    [
        "NULL,'run'",
        "'x','run'",
        "'city-events',''",
        "'city-events',repeat('r',257)",
        "'city-events','run',NULL,NULL,NULL,101",
        "'city-events','run',NULL,'2001-01-01Z',NULL,20",
    ],
)
async def test_run_catalog_sql_rejects_invalid_or_partial_scopes(args: str) -> None:
    async with _owner_transaction() as connection:
        await _role(connection, "ec_operator_viewer")
        with pytest.raises(DBAPIError, match="invalid operator run catalog query"):
            await connection.execute(
                text(f"SELECT public.fn_get_operator_run_catalog_records_v1({args})")
            )
