"""Bound a filter-scoped name index for browser autocomplete prefetch.

Revision ID: 0221
Revises: 0220
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0221"
down_revision: str | None = "0220"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ARGS = "(text,text[],timestamptz[],timestamptz[],text[],text[],text,integer,integer,text[],text,integer)"
_SOURCE = "public.fn_suggest_catalog_names_v2"
_INDEX = "public.fn_catalog_name_index_v1"


def _once(definition: str, before: str, after: str) -> str:
    if definition.count(before) != 1:
        raise RuntimeError("unexpected catalog name-index contract")
    return definition.replace(before, after)


def upgrade() -> None:
    # Reuse the installed, tested admission, privacy and mention projection. Only
    # the name predicate and bounded result size change; filters still select events.
    definition = str(op.get_bind().execute(
        text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": _SOURCE + _ARGS},
    ).scalar_one())
    definition = _once(definition, _SOURCE + "(", _INDEX + "(")
    definition = _once(
        definition,
        "p_query IS NULL OR length(btrim(p_query)) NOT BETWEEN 2 AND 160",
        "p_query IS DISTINCT FROM ''",
    )
    definition = _once(definition, "p_limit NOT BETWEEN 1 AND 20", "p_limit NOT BETWEEN 1 AND 5001")
    definition = _once(
        definition,
        "p_source_keys, selected_window.start_at, selected_window.end_at, v_query, p_cities,",
        "p_source_keys, selected_window.start_at, selected_window.end_at, NULL, p_cities,",
    )
    # Keep organizations/people/places before event titles if a large scope is
    # truncated. The normal per-term reader remains available for every omitted name.
    definition = _once(
        definition,
        "ORDER BY CASE WHEN",
        "ORDER BY CASE WHEN names.kinds = ARRAY['event']::text[] THEN 1 ELSE 0 END, CASE WHEN",
    )
    op.execute(definition)
    op.execute(f"REVOKE ALL ON FUNCTION {_INDEX}{_ARGS} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_INDEX}{_ARGS} TO ec_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {_INDEX}{_ARGS}")
