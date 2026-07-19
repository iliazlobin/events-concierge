"""Reviewed public-source registry and durable catalog-refresh ledger.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-16
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the tenant-neutral reviewed-source control plane (FR-3.1/FR-10.3/NFR-8)."""
    op.execute(
        """
        CREATE TABLE catalog_sources (
            source_key               text PRIMARY KEY,
            display_name             text NOT NULL,
            publisher                text NOT NULL,
            seed_url                 text NOT NULL,
            approved_origins         text[] NOT NULL,
            region                   text NOT NULL,
            mode                     text NOT NULL,
            handoff_only             boolean NOT NULL DEFAULT true,
            enabled                  boolean NOT NULL DEFAULT false,
            reviewed_at              timestamptz,
            review_expires_at        timestamptz,
            refresh_interval_minutes integer NOT NULL,
            min_interval_ms          integer NOT NULL,
            created_at               timestamptz NOT NULL DEFAULT now(),
            updated_at               timestamptz NOT NULL DEFAULT now(),
            CHECK (mode IN ('public_jsonld')),
            CHECK (refresh_interval_minutes > 0),
            CHECK (min_interval_ms > 0)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE catalog_refresh_runs (
            source_key       text NOT NULL REFERENCES catalog_sources(source_key) ON DELETE RESTRICT,
            run_key          text NOT NULL,
            status           text NOT NULL,
            lease_token      uuid,
            started_at       timestamptz NOT NULL,
            lease_expires_at timestamptz,
            completed_at     timestamptz,
            candidate_count  integer,
            canonical_count  integer,
            error            text,
            attempt_count    integer NOT NULL DEFAULT 0,
            PRIMARY KEY (source_key, run_key),
            CHECK (status IN ('running', 'succeeded', 'failed')),
            CHECK (attempt_count >= 0),
            CHECK (candidate_count IS NULL OR candidate_count >= 0),
            CHECK (canonical_count IS NULL OR canonical_count >= 0)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_catalog_refresh_runs_pending
        ON catalog_refresh_runs (lease_expires_at)
        WHERE status = 'running'
        """
    )
    # The user explicitly approved this public Luma calendar. It is only eligible for an explicit
    # worker/manual refresh; no migration, request, or feed path performs an HTTP call.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms)
        VALUES
            ('luma-genai-sf', 'Generative AI SF', 'Generative AI SF',
             'https://luma.com/genai-sf', ARRAY['https://luma.com'], 'bay_area_9_county',
             'public_jsonld', true, true, now(), NULL, 360, 1500)
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS catalog_refresh_runs")
    op.execute("DROP TABLE IF EXISTS catalog_sources")
