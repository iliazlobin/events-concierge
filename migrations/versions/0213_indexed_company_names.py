"""Include indexed organization names and their recorded event-name variants in discovery.

Revision ID: 0213
Revises: 0212

The entity projection already follows source publication. Reuse its organization names without
inventing identities from prose, and keep the exact public observation behind each suggestion.
Selecting an indexed name must reach the same events in pages, topic facets and Calendar.
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "0213"
down_revision: str | None = "0212"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BROWSE = (
    "public.fn_browse_filtered_current_catalog_events_unbounded_v1"
    "(text[],timestamptz,timestamptz,text,text[],text[],text,integer,integer,text[],text,"
    "timestamptz,uuid,integer)"
)
_NAMES = (
    "public.fn_suggest_catalog_names_v1"
    "(text,text[],timestamptz[],timestamptz[],text[],text[],text,integer,integer,text[],text,integer)"
)
_INDEX = "ix_catalog_organizations_name_trgm"

# These additions are reversible exact replacements: unexpected definitions fail before mutation.
_BROWSE_CTE = "WITH browse_observations AS MATERIALIZED ("
_COMPANY_BROWSE_CTE = """WITH company_matches AS MATERIALIZED (
                SELECT mention.canonical_event_id, mention.source_key,
                       mention.source_event_id, mention.source_run_key
                FROM public.catalog_entities AS company
                JOIN public.catalog_entity_event_mentions AS mention USING (entity_id)
                WHERE company.kind = 'organization'
                  AND lower(btrim(company.display_name)) = lower(btrim(p_query))
            ), browse_observations AS MATERIALIZED ("""
_SEARCH_END = """                                      observation.provider
                                  )
                              )
                          ) > 0"""
_COMPANY_SEARCH_END = _SEARCH_END + """
                          OR EXISTS (
                              SELECT 1 FROM company_matches AS company
                              WHERE company.canonical_event_id = event.canonical_event_id
                                AND company.source_key = observation.source_key
                                AND company.source_event_id = observation.source_event_id
                                AND company.source_run_key = observation.refresh_run_key
                          )"""

_NAME_COLUMNS = "candidate.speaker_names, candidate.partner_names, candidate.entity_profiles"
_COMPANY_NAME_COLUMNS = _NAME_COLUMNS + """,
                       candidate.source_key, candidate.source_event_id, candidate.refresh_run_key"""
_TYPED_START = """            ), typed_mentions AS (
                SELECT mention.canonical_event_id, mention.display_name, mention.kind"""
_COMPANY_TYPED_START = r"""            ), company_names AS MATERIALIZED (
                SELECT company.entity_id, company.display_name
                FROM public.catalog_entities AS company
                WHERE company.kind = 'organization'
                  AND lower(btrim(company.display_name)) LIKE
                      '%' || replace(replace(replace(v_query, E'\\', E'\\\\'),
                                                   '%', E'\\%'), '_', E'\\_') || '%'
                      ESCAPE E'\\'
            ), company_mentions AS (
                SELECT event.canonical_event_id, company.display_name,
                       'organization'::text AS kind
                FROM company_names AS company
                JOIN public.catalog_entity_event_mentions AS mention USING (entity_id)
                JOIN eligible AS event
                  ON event.canonical_event_id = mention.canonical_event_id
                 AND event.source_key = mention.source_key
                 AND event.source_event_id = mention.source_event_id
                 AND event.refresh_run_key = mention.source_run_key
            ), typed_mentions AS (
                SELECT company.canonical_event_id, company.display_name, company.kind
                FROM company_mentions AS company
                UNION ALL
                SELECT mention.canonical_event_id, mention.display_name, mention.kind"""


def _definition(signature: str) -> str:
    return str(op.get_bind().execute(
        text("SELECT pg_catalog.pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    ).scalar_one())


def _replace_once(definition: str, before: str, after: str) -> str:
    if definition.count(before) != 1:
        raise RuntimeError("unexpected catalog company-search definition")
    return definition.replace(before, after)


def _browse_definition(definition: str, *, include_companies: bool) -> str:
    replacements = ((_BROWSE_CTE, _COMPANY_BROWSE_CTE), (_SEARCH_END, _COMPANY_SEARCH_END))
    for old, new in replacements:
        definition = _replace_once(definition, *(old, new) if include_companies else (new, old))
    return definition


def _names_definition(definition: str, *, include_companies: bool) -> str:
    replacements = ((_NAME_COLUMNS, _COMPANY_NAME_COLUMNS), (_TYPED_START, _COMPANY_TYPED_START))
    for old, new in replacements:
        definition = _replace_once(definition, *(old, new) if include_companies else (new, old))
    return definition


def upgrade() -> None:
    browse = _browse_definition(_definition(_BROWSE), include_companies=True)
    names = _names_definition(_definition(_NAMES), include_companies=True)
    op.execute(f"""
        CREATE INDEX {_INDEX} ON public.catalog_entities
        USING gin (lower(btrim(display_name)) public.gin_trgm_ops)
        WHERE kind = 'organization'
    """)
    # CREATE OR REPLACE preserves owners, grants, STABLE and the pinned security-definer path.
    op.execute(browse)
    op.execute(names)


def downgrade() -> None:
    browse = _browse_definition(_definition(_BROWSE), include_companies=False)
    names = _names_definition(_definition(_NAMES), include_companies=False)
    op.execute(names)
    op.execute(browse)
    op.execute(f"DROP INDEX public.{_INDEX}")
