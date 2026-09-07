"""Managed PostgreSQL connections have an explicit per-process budget."""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from events_concierge.infra import db


def test_init_engine_passes_the_exact_bounded_pool_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = Mock()
    create = Mock(return_value=engine)
    sessionmaker = Mock(return_value=Mock())
    monkeypatch.setattr(db, "create_async_engine", create)
    monkeypatch.setattr(db, "async_sessionmaker", sessionmaker)

    result = db.init_engine(
        "postgresql+psycopg://app:secret@db.example/events",
        pool_size=7,
        max_overflow=1,
        pool_timeout_seconds=3.5,
        pool_recycle_seconds=900,
    )

    assert result is engine
    create.assert_called_once_with(
        "postgresql+psycopg://app:secret@db.example/events",
        echo=False,
        pool_pre_ping=True,
        pool_size=7,
        max_overflow=1,
        pool_timeout=3.5,
        pool_recycle=900,
        connect_args={},
    )
    sessionmaker.assert_called_once_with(engine, expire_on_commit=False)


def test_init_engine_sets_work_mem_at_connection_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The catalog rollups spill to temp files at PostgreSQL's 4MB default.

    The ceiling is applied as a startup parameter so it costs one setting when a
    connection is opened rather than a statement on every checkout.
    """
    create = Mock(return_value=Mock())
    monkeypatch.setattr(db, "create_async_engine", create)
    monkeypatch.setattr(db, "async_sessionmaker", Mock(return_value=Mock()))

    db.init_engine("postgresql+psycopg://app:secret@db.example/events", work_mem="64MB")

    assert create.call_args.kwargs["connect_args"] == {"options": "-c work_mem=64MB"}


def test_init_engine_rejects_a_work_mem_that_is_not_a_postgres_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The value reaches the server as a startup parameter, so it is never interpolated blind."""
    monkeypatch.setattr(db, "create_async_engine", Mock(return_value=Mock()))
    monkeypatch.setattr(db, "async_sessionmaker", Mock(return_value=Mock()))

    for invalid in ("64 MB", "lots", "64MB; DROP TABLE x", ""):
        with pytest.raises(ValueError):
            db.init_engine(
                "postgresql+psycopg://app:secret@db.example/events", work_mem=invalid
            )


@pytest.mark.parametrize(
    "overrides",
    [
        {"pool_size": 0},
        {"max_overflow": -1},
        {"pool_timeout_seconds": 0},
        {"pool_recycle_seconds": 29},
    ],
)
def test_init_engine_rejects_unbounded_or_invalid_pool_values(
    overrides: dict[str, int],
) -> None:
    with pytest.raises(ValueError, match="database"):
        db.init_engine("postgresql+psycopg://app:secret@db.example/events", **overrides)
