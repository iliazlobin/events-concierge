"""Select event starts inside explicit catalog date ranges.

Revision ID: 0211
Revises: 0208
Create Date: 2026-10-07

Explicit catalog filters describe start dates, while unbounded live discovery continues to admit
ongoing events. Change the shared observation predicates used by pages and facets, preserving
their collection-window admission, retained-history rules, security settings and existing grants.
Personalized retrieval keeps its independent interval-overlap contract in a dedicated capability.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0211"
down_revision: str | None = "0208"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OBSERVATIONS = (
    "public.fn_list_retained_catalog_browse_observations_v1(text,timestamptz,timestamptz)",
    "public.fn_list_retained_catalog_browse_observations_v2(text[],timestamptz,timestamptz)",
)
_RECOMMENDATIONS = (
    "public.fn_list_retained_catalog_recommendation_observations_v1(text[],timestamptz,timestamptz)"
)
_OVERLAP = """coalesce(event.end_at, event.start_at)
                    > coalesce(p_window_start, statement_timestamp())"""
_START_DATES = """(
                  (p_window_start IS NULL
                   AND coalesce(event.end_at, event.start_at) > statement_timestamp())
                  OR (p_window_start IS NOT NULL AND event.start_at >= p_window_start)
              )"""


def _definition(signature: str) -> str:
    return str(
        op.get_bind()
        .execute(
            text("SELECT pg_catalog.pg_get_functiondef(CAST(:signature AS regprocedure))"),
            {"signature": signature},
        )
        .scalar_one()
    )


def _replace_date_predicate(*, explicit_starts: bool) -> None:
    before, after = (_OVERLAP, _START_DATES) if explicit_starts else (_START_DATES, _OVERLAP)
    for signature in _OBSERVATIONS:
        definition = _definition(signature)
        if definition.count(before) != 1:
            raise RuntimeError("unexpected catalog observation date predicate")
        op.execute(definition.replace(before, after))


def upgrade() -> None:
    definition = _definition(_OBSERVATIONS[1])
    name = "fn_list_retained_catalog_browse_observations_v2"
    if definition.count(name) != 1 or definition.count(_OVERLAP) != 1:
        raise RuntimeError("unexpected catalog recommendation admission definition")
    op.execute(definition.replace(name, "fn_list_retained_catalog_recommendation_observations_v1"))
    op.execute(f"REVOKE ALL ON FUNCTION {_RECOMMENDATIONS} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_RECOMMENDATIONS} TO ec_app")
    _replace_date_predicate(explicit_starts=True)


def downgrade() -> None:
    _replace_date_predicate(explicit_starts=False)
    op.execute(f"DROP FUNCTION {_RECOMMENDATIONS}")
