"""The entity-intelligence poll loop's failure reporting.

Two requirements pull against each other here. A permanent code-to-schema drift once hid behind
``error_type=ProgrammingError`` for hours because the missing function's name never reached the
log — so the diagnosis has to be visible. But a DBAPI error from this lane renders the statement it
was running, and those statements bind a third party's display name and public profile URL — so the
bound values must not be.
"""

from __future__ import annotations

import pytest

from events_concierge.workers.entity_intelligence import _diagnostic


def test_the_diagnosis_survives() -> None:
    error = RuntimeError(
        "(psycopg.errors.UndefinedFunction) function "
        "public.fn_get_catalog_entity_insights_v1(uuid) does not exist"
    )

    assert "fn_get_catalog_entity_insights_v1" in _diagnostic(error)


@pytest.mark.parametrize(
    "rendered",
    [
        # SQLAlchemy's DBAPIError rendering: diagnosis first, bound values below.
        "(psycopg.errors.NotNullViolation) null value in column \"external_id\"\n"
        "[SQL: SELECT public.fn_replace_catalog_entity_external_source_v1(%(entity_id)s, ...)]\n"
        "[parameters: {'external_id': 'linkedin:/in/clemensmbauer', "
        "'source_url': 'https://www.linkedin.com/in/clemensmbauer'}]",
        # Postgres puts its DETAIL — which renders the whole failing row — on the next line too.
        "(psycopg.errors.CheckViolation) new row for relation \"catalog_entities\" violates "
        "check constraint \"ck_catalog_entities_profile_key\"\n"
        "DETAIL:  Failing row contains (..., person, Matthew Mitsui, "
        "https://www.linkedin.com/in/matthewmitsui, ...).",
    ],
)
def test_no_bound_value_or_failing_row_rides_along(rendered: str) -> None:
    diagnostic = _diagnostic(RuntimeError(rendered))

    assert "\n" not in diagnostic
    for leaked in ("parameters:", "[SQL:", "DETAIL:", "linkedin", "Matthew Mitsui"):
        assert leaked not in diagnostic, f"{leaked!r} reached the log"


def test_a_single_line_exception_with_no_newline_is_still_bounded() -> None:
    diagnostic = _diagnostic(RuntimeError("x" * 5_000))

    assert len(diagnostic) <= 300
