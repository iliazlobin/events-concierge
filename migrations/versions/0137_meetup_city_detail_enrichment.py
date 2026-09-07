"""Approve bounded public Meetup city event-detail enrichment.

Revision ID: 0137
Revises: 0136
Create Date: 2026-07-31

``page_limit`` is the generic refresh request-unit budget.  The reviewed Meetup contract spends
one unit on the exact city page and at most forty units on canonical same-origin event pages
asserted by that page's root Event JSON-LD.  Changing this bound is a source-contract revision;
the adapter independently requires the exact value and exact city identities before egress.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0137"
down_revision: str | None = "0136"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Approve one city request plus no more than forty exact event-detail requests."""
    op.execute(
        """
        UPDATE public.catalog_sources
        SET page_limit = 41,
            source_revision = source_revision + 1,
            reviewed_at = now(),
            updated_at = now()
        WHERE mode = 'meetup_city_jsonld'
          AND handoff_only
          AND page_limit = 1
          AND approved_origins = ARRAY['https://www.meetup.com']::text[]
          AND (
              (source_key = 'meetup-sf'
               AND seed_url = 'https://www.meetup.com/find/us--ca--san-francisco/')
              OR
              (source_key = 'meetup-nyc'
               AND seed_url = 'https://www.meetup.com/find/us--ny--new-york/')
          )
        """
    )


def downgrade() -> None:
    """Return the exact reviewed rows to their city-page-only request contract."""
    op.execute(
        """
        UPDATE public.catalog_sources
        SET page_limit = 1,
            source_revision = source_revision + 1,
            reviewed_at = now(),
            updated_at = now()
        WHERE mode = 'meetup_city_jsonld'
          AND handoff_only
          AND page_limit = 41
          AND approved_origins = ARRAY['https://www.meetup.com']::text[]
          AND (
              (source_key = 'meetup-sf'
               AND seed_url = 'https://www.meetup.com/find/us--ca--san-francisco/')
              OR
              (source_key = 'meetup-nyc'
               AND seed_url = 'https://www.meetup.com/find/us--ny--new-york/')
          )
        """
    )
