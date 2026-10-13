"""Match names before expanding the autocomplete event projection.

Revision ID: 0215
Revises: 0214

Autocomplete previously expanded every eligible event before looking for the typed name.
Its private candidate reader retains the browse admission and filter checks, but searches
only name fields and indexed company variants. Public text search keeps its existing behavior.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0215"
down_revision: str | None = "0214"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BROWSE_NAME = "public.fn_browse_filtered_current_catalog_events_unbounded_v1"
_CANDIDATE_NAME = "public.fn_catalog_name_candidates_v1"
_ARGUMENTS = (
    "(text[],timestamptz,timestamptz,text,text[],text[],text,integer,integer,text[],text,"
    "timestamptz,uuid,integer)"
)
_NAMES = (
    "public.fn_suggest_catalog_names_v1"
    "(text,text[],timestamptz[],timestamptz[],text[],text[],text,integer,integer,text[],text,integer)"
)

_TEXT_FIELDS = """                                      event.title,
                                      event.description,
                                      event.venue_name,
                                      event.city_norm,
                                      event.organizer_name,
                                      array_to_string(event.host_names, ' '),
                                      array_to_string(event.speaker_names, ' '),
                                      array_to_string(event.partner_names, ' '),
                                      observation.source_label,
                                      observation.publisher,
                                      observation.provider"""
_NAME_FIELDS = """                                      event.title,
                                      event.venue_name,
                                      event.organizer_name,
                                      array_to_string(event.host_names, ' '),
                                      array_to_string(event.speaker_names, ' '),
                                      array_to_string(event.partner_names, ' ')"""
_EXACT_COMPANY = "AND lower(btrim(company.display_name)) = lower(btrim(p_query))"
_PARTIAL_COMPANY = r"""AND lower(btrim(company.display_name)) LIKE
                      '%' || replace(replace(replace(lower(btrim(p_query)), E'\\', E'\\\\'),
                                                   '%', E'\\%'), '_', E'\\_') || '%'
                      ESCAPE E'\\'"""
_WINDOW_ARGUMENTS = (
    "p_source_keys, selected_window.start_at, selected_window.end_at, NULL, p_cities,"
)
_NAME_ARGUMENTS = (
    "p_source_keys, selected_window.start_at, selected_window.end_at, v_query, p_cities,"
)


def _definition(signature: str) -> str:
    return str(op.get_bind().execute(
        text("SELECT pg_catalog.pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    ).scalar_one())


def _replace_once(definition: str, before: str, after: str) -> str:
    if definition.count(before) != 1:
        raise RuntimeError("unexpected catalog name-search definition")
    return definition.replace(before, after)


def upgrade() -> None:
    # Copy the installed admission/validation contract rather than reconstructing its
    # policy, history, location and price rules. Fail before mutation if it has drifted.
    candidate = _definition(_BROWSE_NAME + _ARGUMENTS)
    candidate = _replace_once(candidate, _BROWSE_NAME + "(", _CANDIDATE_NAME + "(")
    candidate = _replace_once(candidate, _TEXT_FIELDS, _NAME_FIELDS)
    candidate = _replace_once(candidate, _EXACT_COMPANY, _PARTIAL_COMPANY)
    names = _definition(_NAMES)
    names = _replace_once(names, _BROWSE_NAME + "(", _CANDIDATE_NAME + "(")
    names = _replace_once(names, _WINDOW_ARGUMENTS, _NAME_ARGUMENTS)
    op.execute(candidate)
    op.execute(f"REVOKE ALL ON FUNCTION {_CANDIDATE_NAME}{_ARGUMENTS} FROM PUBLIC, ec_app")
    # Replacement preserves the bounded public function's owner, grants and search path.
    op.execute(names)


def downgrade() -> None:
    names = _definition(_NAMES)
    names = _replace_once(names, _CANDIDATE_NAME + "(", _BROWSE_NAME + "(")
    names = _replace_once(names, _NAME_ARGUMENTS, _WINDOW_ARGUMENTS)
    op.execute(names)
    op.execute(f"DROP FUNCTION {_CANDIDATE_NAME}{_ARGUMENTS}")
