"""Match spaced company names without changing identities or selected-filter admission.

Revision ID: 0217
Revises: 0216

The new bounded reader folds whitespace for matching only. Original display names, grouping,
public observations and filter guards remain intact. Retaining v1 supports older serving images
and the existing migration/recovery contracts during a rolling release.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0217"
down_revision: str | None = "0216"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CANDIDATE_V1 = "public.fn_catalog_name_candidates_v1"
_CANDIDATE_V2 = "public.fn_catalog_name_candidates_v2"
_CANDIDATE_ARGS = (
    "(text[],timestamptz,timestamptz,text,text[],text[],text,integer,integer,text[],text,"
    "timestamptz,uuid,integer)"
)
_NAMES_V1 = "public.fn_suggest_catalog_names_v1"
_NAMES_V2 = "public.fn_suggest_catalog_names_v2"
_NAME_ARGS = "(text,text[],timestamptz[],timestamptz[],text[],text[],text,integer,integer,text[],text,integer)"
_NAME_FIELDS = """                                      event.title,
                                      event.venue_name,
                                      event.organizer_name,
                                      array_to_string(event.host_names, ' '),
                                      array_to_string(event.speaker_names, ' '),
                                      array_to_string(event.partner_names, ' ')"""
_INDEX = "ix_catalog_organization_compact_name_trgm"


def _fold(value: str) -> str:
    return f"regexp_replace({value}, '[[:space:]]+', '', 'g')"


def _definition(signature: str) -> str:
    return str(
        op.get_bind()
        .execute(
            text("SELECT pg_catalog.pg_get_functiondef(CAST(:signature AS regprocedure))"),
            {"signature": signature},
        )
        .scalar_one()
    )


def _once(definition: str, before: str, after: str) -> str:
    if definition.count(before) != 1:
        raise RuntimeError("unexpected compact catalog-name contract")
    return definition.replace(before, after)


def upgrade() -> None:
    candidate = _definition(_CANDIDATE_V1 + _CANDIDATE_ARGS)
    candidate = _once(candidate, _CANDIDATE_V1 + "(", _CANDIDATE_V2 + "(")
    candidate = _once(
        candidate, "lower(btrim(company.display_name))", _fold("lower(btrim(company.display_name))")
    )
    folded_fields = ",\n                                      ".join(
        _fold(field.strip()) for field in _NAME_FIELDS.split(",\n")
    )
    candidate = _once(
        candidate, _NAME_FIELDS, "                                      " + folded_fields
    )
    names = _definition(_NAMES_V1 + _NAME_ARGS)
    names = _once(names, _NAMES_V1 + "(", _NAMES_V2 + "(")
    names = _once(names, _CANDIDATE_V1 + "(", _CANDIDATE_V2 + "(")
    names = _once(
        names,
        "v_query text := lower(btrim(p_query));",
        "v_query text := " + _fold("lower(btrim(p_query))") + ";",
    )
    names = _once(
        names, "lower(btrim(company.display_name))", _fold("lower(btrim(company.display_name))")
    )
    names = _once(
        names,
        "strpos(lower(left(btrim(mention.name), 160)), v_query)",
        "strpos(" + _fold("lower(left(btrim(mention.name), 160))") + ", v_query)",
    )
    names = _once(names, "names.name_key = v_query", _fold("names.name_key") + " = v_query")
    names = _once(
        names, "strpos(names.name_key, v_query)", "strpos(" + _fold("names.name_key") + ", v_query)"
    )
    op.execute(f"""
        CREATE INDEX {_INDEX} ON public.catalog_entities
        USING gin ({_fold("lower(btrim(display_name))")} public.gin_trgm_ops)
        WHERE kind='organization'
    """)
    op.execute(candidate)
    op.execute(f"REVOKE ALL ON FUNCTION {_CANDIDATE_V2}{_CANDIDATE_ARGS} FROM PUBLIC,ec_app")
    op.execute(names)
    op.execute(f"REVOKE ALL ON FUNCTION {_NAMES_V2}{_NAME_ARGS} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_NAMES_V2}{_NAME_ARGS} TO ec_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION {_NAMES_V2}{_NAME_ARGS}")
    op.execute(f"DROP FUNCTION {_CANDIDATE_V2}{_CANDIDATE_ARGS}")
    op.execute(f"DROP INDEX public.{_INDEX}")
