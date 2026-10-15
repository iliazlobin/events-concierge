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
_PRIVACY_MARKER = "/* observation-scoped location privacy */"


def _redact_venues(definition: str) -> str:
    if _PRIVACY_MARKER in definition:
        return definition
    definition = _once(
        definition, "candidate.venue_name, candidate.organizer_name,",
        """/* observation-scoped location privacy */
                       CASE WHEN bool_or(
                           coalesce(privacy.location_withheld, true)
                           OR coalesce(privacy.visibility IN ('private','unlisted'), false)
                       ) OVER (PARTITION BY candidate.canonical_event_id)
                       THEN NULL ELSE candidate.venue_name END AS venue_name,
                       candidate.organizer_name,""",
    )
    return _once(definition, """) AS candidate
                WHERE p_availability""", """) AS candidate
                LEFT JOIN public.catalog_event_observations AS privacy
                  ON privacy.source_key = candidate.source_key
                 AND privacy.source::text = candidate.observation_source
                 AND privacy.source_event_id = candidate.source_event_id
                 AND privacy.canonical_event_id = candidate.canonical_event_id
                 AND privacy.last_run_key = candidate.refresh_run_key
                WHERE p_availability""")


def _once(definition: str, before: str, after: str) -> str:
    if definition.count(before) != 1:
        raise RuntimeError("unexpected catalog name-index contract")
    return definition.replace(before, after)


def upgrade() -> None:
    # Reuse the installed, tested admission, privacy and mention projection. Only
    # the name predicate and bounded result size change; filters still select events.
    readers = {}
    for name in ("public.fn_suggest_catalog_names_v1", _SOURCE):
        readers[name] = _redact_venues(str(op.get_bind().execute(
            text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
            {"signature": name + _ARGS},
        ).scalar_one()))
    definition = readers[_SOURCE]
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
    for reader in readers.values():
        op.execute(reader)
    op.execute(definition)
    op.execute(f"REVOKE ALL ON FUNCTION {_INDEX}{_ARGS} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_INDEX}{_ARGS} TO ec_app")


def downgrade() -> None:
    # Keep the observation-level privacy fix in compatible per-term readers.
    # Reintroducing an earlier withheld venue is not a valid code rollback.
    op.execute(f"DROP FUNCTION {_INDEX}{_ARGS}")
