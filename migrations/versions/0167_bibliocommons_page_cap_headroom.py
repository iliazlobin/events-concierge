"""Restore page-cap headroom on the two BiblioCommons feeds that have grown past their caps.

Revision ID: 0167
Revises: 0166
Create Date: 2026-08-26

``page_limit`` is a cliff, not a budget.  The BiblioCommons adapter pages 25 items at a time and
raises when the last permitted page still has more, discarding every page it already fetched.  A
feed that outgrows its cap therefore does not degrade to partial coverage -- it publishes nothing,
and because ``fn_list_retained_catalog_browse_observations_v1`` keeps only what the newest
*successful* run returned, its live set then ages out behind a frozen success.

Both feeds here have simply grown past caps sized when they were smaller:

* ``san-jose-public-library-events`` -- 160 x 25 = 4,000 against a best-ever haul of 3,820 (4.5%
  headroom).  Failing since 2026-08-13, attempt_count 5,166.
* ``sccld-all-physical-branches-events`` -- 50 x 25 = 1,250 against 1,207 (3.4%).  Failing since
  2026-08-20, attempt_count 2,252.

Neither ever advances ``due_at``, which is derived from the last *success*, so their due time
recedes further every hour and they sort to the head of every cadence plan -- consuming the fleet's
single ``refresh_due`` slot while publishing nothing.

Doubling each restores roughly 50% headroom.  Raising a cap costs no egress unless the feed
actually grows into it: the walk still stops when the feed ends.  The new caps stay inside the
one-hour lease ceiling that ``_lease_seconds_for`` enforces -- San José needs 2,540s of the 3,600s
budget and SCCLD 1,410s -- so neither row starts failing closed on lease admission instead.

NOT CHANGED HERE, and worth an owner decision:
``alameda-county-library-all-physical-branches-events`` is at 40 x 25 = 1,000 against a best haul of
917, i.e. **8.3% headroom**, and is the next feed that will hit this same cliff.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0167"
down_revision: str | None = "0166"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Double both reviewed caps and re-stamp the review that approved the wider bound."""
    op.execute(
        """
        UPDATE catalog_sources
        SET page_limit = 320,
            reviewed_at = now(),
            updated_at = now()
        WHERE source_key = 'san-jose-public-library-events'
        """
    )
    op.execute(
        """
        UPDATE catalog_sources
        SET page_limit = 120,
            reviewed_at = now(),
            updated_at = now()
        WHERE source_key = 'sccld-all-physical-branches-events'
        """
    )


def downgrade() -> None:
    """Restore the narrower reviewed caps, which both feeds currently exceed."""
    op.execute(
        """
        UPDATE catalog_sources
        SET page_limit = 160,
            updated_at = now()
        WHERE source_key = 'san-jose-public-library-events'
        """
    )
    op.execute(
        """
        UPDATE catalog_sources
        SET page_limit = 50,
            updated_at = now()
        WHERE source_key = 'sccld-all-physical-branches-events'
        """
    )
