"""Prune schedule fragments from the public entity index.

Revision ID: 0148
Revises: 0147
Create Date: 2026-08-04

Structured provider roles are strong evidence, but historical Luma descriptions could promote
agenda bullets such as ``10:30 - Meet at ...`` into the speaker list.  This additive gate removes
only source-scoped speaker rows with unmistakable schedule/instruction syntax.  Direct public
profiles are never pruned by this heuristic.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0148"
down_revision: str | None = "0147"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PRUNE = "public.fn_prune_catalog_entity_index_v1(text)"


def upgrade() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_prune_catalog_entity_index_v1(p_source_key text)
        RETURNS integer
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_deleted integer;
        BEGIN
            IF p_source_key IS NOT NULL
               AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
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
    op.execute("SELECT public.fn_prune_catalog_entity_index_v1(NULL)")


def downgrade() -> None:
    op.execute(f"REVOKE ALL ON FUNCTION {_PRUNE} FROM ec_app")
    op.execute(f"DROP FUNCTION IF EXISTS {_PRUNE}")
