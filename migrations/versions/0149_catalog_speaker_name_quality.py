"""Tighten source-scoped speaker-name quality without touching verified profiles.

Revision ID: 0149
Revises: 0148
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0149"
down_revision: str | None = "0148"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRUNE = "public.fn_prune_catalog_entity_index_v1(text)"


def upgrade() -> None:
    _replace_prune_function(strict_names=True)
    op.execute("SELECT public.fn_prune_catalog_entity_index_v1(NULL)")


def downgrade() -> None:
    _replace_prune_function(strict_names=False)


def _replace_prune_function(*, strict_names: bool) -> None:
    strict_predicate = (
        """
                  OR lower(mention.observed_name) ~ '^best[[:space:]]'
                  OR NOT (
                      mention.observed_name ~ (
                          '^[[:upper:]][[:alpha:]''\u2019.-]+'
                          '([[:space:]]+(al|bin|da|de|del|der|di|la|van|von|'
                          '[[:upper:]][[:alpha:]''\u2019.-]+)){1,7}'
                          '([[:space:]]+[-\u2013\u2014][[:space:]].*)?$'
                      )
                      OR mention.observed_name ~ (
                          '^[[:upper:]][[:alpha:]''\u2019.-]+[[:space:]]+'
                          '[[:upper:]][[:alpha:]''\u2019.-]+,[[:space:]]+'
                          '(Founder|Co[- ]?Founder|CEO|CTO|COO|CFO|CMO|CPO|VP|'
                          'Head|Director|Manager|Partner|Principal|Engineer|Developer|'
                          'Designer|Researcher|Professor|Investor)(,|[[:space:]]|$)'
                      )
                  )
        """
        if strict_names
        else ""
    )
    op.execute(
        rf"""
        CREATE OR REPLACE FUNCTION public.fn_prune_catalog_entity_index_v1(p_source_key text)
        RETURNS integer
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_deleted integer;
        BEGIN
            IF p_source_key IS NOT NULL
               AND p_source_key !~ '^[a-z0-9][a-z0-9-]{{1,79}}$'
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'entity source is invalid';
            END IF;

            DELETE FROM public.catalog_entity_event_mentions AS mention
            USING public.catalog_entities AS entity
            WHERE entity.entity_id = mention.entity_id
              AND entity.identity_status = 'source_scoped'
              AND mention.role = 'speaker'
              AND (p_source_key IS NULL OR mention.source_key = p_source_key)
              AND (
                  mention.observed_name ~ '[[:digit:]]'
                  OR lower(mention.observed_name) ~ (
                      '(^|[^[:alpha:]])(lunch break|meet at|workshop round|prizes?|passes|'
                      'stroller|book to swap|bring a board)([^[:alpha:]]|$)'
                  )
                  {strict_predicate}
              );
            GET DIAGNOSTICS v_deleted = ROW_COUNT;

            DELETE FROM public.catalog_entities AS entity
            WHERE entity.identity_status = 'source_scoped'
              AND NOT EXISTS (
                  SELECT 1
                  FROM public.catalog_entity_event_mentions AS mention
                  WHERE mention.entity_id = entity.entity_id
              );

            RETURN v_deleted;
        END
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_PRUNE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_PRUNE} TO ec_app")
