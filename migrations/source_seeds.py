"""Remove migration-created source seeds only when configuration and history are untouched."""

from collections.abc import Mapping
from typing import Any

from alembic import op
from sqlalchemy import text


def remove_unused_seed(source_key: str, expected: Mapping[str, Any]) -> None:
    """Refuse changed/reviewed profiles or any FK-linked history, including cascaded records."""
    connection = op.get_bind()
    row = (
        connection.execute(
            text("""
        SELECT * FROM public.catalog_sources WHERE source_key=:key FOR UPDATE
    """),
            {"key": source_key},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return
    pristine = {
        "enabled": False,
        "reviewed_at": None,
        "review_expires_at": None,
        "source_revision": 1,
        "handoff_only": True,
        **expected,
    }
    if any(row[key] != value for key, value in pristine.items()):
        raise RuntimeError("retain activated or modified vendor source; use code rollback")
    references = (
        connection.execute(
            text("""
        SELECT pg_catalog.quote_ident(namespace.nspname) || '.' ||
               pg_catalog.quote_ident(child.relname) AS relation,
               pg_catalog.quote_ident(attribute.attname) AS column_name,
               child.relrowsecurity AS row_security
        FROM pg_catalog.pg_constraint AS fk
        JOIN pg_catalog.pg_class AS child ON child.oid=fk.conrelid
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid=child.relnamespace
        CROSS JOIN LATERAL generate_subscripts(fk.confkey,1) AS position(index)
        JOIN pg_catalog.pg_attribute AS parent_attribute
          ON parent_attribute.attrelid=fk.confrelid
         AND parent_attribute.attnum=fk.confkey[position.index]
        JOIN pg_catalog.pg_attribute AS attribute ON attribute.attrelid=fk.conrelid
         AND attribute.attnum=fk.conkey[position.index]
        WHERE fk.contype='f' AND fk.confrelid='public.catalog_sources'::regclass
          AND parent_attribute.attname='source_key'
    """)
        )
        .mappings()
        .all()
    )
    for reference in references:
        if reference["row_security"]:
            raise RuntimeError("cannot prove unused vendor source under row security")
        # Identifiers are quoted by PostgreSQL, never interpolated from a URL or source value.
        history = connection.execute(
            text(
                "SELECT EXISTS(SELECT 1 FROM "
                + reference["relation"]
                + " WHERE "
                + reference["column_name"]
                + "=:key)"
            ),
            {"key": source_key},
        ).scalar_one()
        if history:
            raise RuntimeError("retain vendor source history; use code rollback")
    connection.execute(
        text("DELETE FROM public.catalog_sources WHERE source_key=:key"), {"key": source_key}
    )
