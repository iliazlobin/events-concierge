"""Source/run provenance for normalized catalog events.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Record source-specific event observations without making catalog rows tenant-scoped (FR-3.8/NFR-1)."""
    op.execute(
        """
        CREATE TABLE catalog_event_observations (
            source_key         text NOT NULL REFERENCES catalog_sources(source_key) ON DELETE RESTRICT,
            source             text NOT NULL,
            source_event_id    text NOT NULL,
            canonical_event_id uuid NOT NULL REFERENCES canonical_events(canonical_event_id) ON DELETE RESTRICT,
            registration_url   text NOT NULL,
            price_status       text NOT NULL,
            content_hash       text NOT NULL,
            first_seen_at      timestamptz NOT NULL DEFAULT now(),
            last_seen_at       timestamptz NOT NULL DEFAULT now(),
            last_run_key       text NOT NULL,
            PRIMARY KEY (source_key, source, source_event_id),
            FOREIGN KEY (source_key, last_run_key)
                REFERENCES catalog_refresh_runs(source_key, run_key) ON DELETE RESTRICT,
            CHECK (price_status IN ('free', 'paid', 'unknown')),
            CHECK (length(content_hash) = 64)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_catalog_event_observations_canonical
        ON catalog_event_observations (canonical_event_id)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS catalog_event_observations")
