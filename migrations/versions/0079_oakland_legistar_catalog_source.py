"""Add the reviewed City of Oakland Legistar public-meetings source.

Revision ID: 0079
Revises: 0078
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0079"
down_revision: str | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add Oakland's factual, handoff-only civic Events seed (FR-3.1/FR-10.3)."""
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (
            mode IN (
                'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415',
                'bibliocommons_rss', 'san_jose_legistar', 'sunnyvale_legistar',
                'alameda_legistar', 'oakland_legistar', 'communico_json', 'tribe_events_json',
                'localist_json', 'libcal_ics', 'civic_engage_rss', 'midpen_html', 'usfca_html',
                'calperformances_json', 'berkeley_rep_html', 'ybca_html', 'oakland_html'
            )
        )
        """
    )
    # The closed adapter constructs the fixed local 90-day OData filter and 100-row pages itself.
    # Five pages cap a refresh below 500 public records / at most 7.5 seconds of paced GETs; a full
    # fifth page fails rather than claiming a partial window. It retains factual metadata and sends
    # users only to matching public Legistar detail handoffs; unsafe individual records are excluded
    # and the worker never requests a handoff.
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit)
        VALUES
            ('oakland-legistar-meetings', 'Oakland Public Meetings',
             'City of Oakland City Clerk',
             'https://webapi.legistar.com/v1/Oakland/Events',
             ARRAY['https://webapi.legistar.com'], 'bay_area_9_county',
             'oakland_legistar', true, true, now(), NULL, 360, 1500, 5)
        """
    )


def downgrade() -> None:
    """Remove Oakland provenance before restoring the prior catalog-mode constraint."""
    op.execute(
        """
        DELETE FROM catalog_event_observations
        WHERE source_key = 'oakland-legistar-meetings'
        """
    )
    op.execute(
        """
        DELETE FROM catalog_refresh_runs
        WHERE source_key = 'oakland-legistar-meetings'
        """
    )
    op.execute("DELETE FROM catalog_sources WHERE source_key = 'oakland-legistar-meetings'")
    op.execute("ALTER TABLE catalog_sources DROP CONSTRAINT ck_catalog_sources_mode")
    op.execute(
        """
        ALTER TABLE catalog_sources
        ADD CONSTRAINT ck_catalog_sources_mode
        CHECK (
            mode IN (
                'public_jsonld', 'livewhale_json', 'sf_gov_json', 'datasf_our415',
                'bibliocommons_rss', 'san_jose_legistar', 'sunnyvale_legistar',
                'alameda_legistar', 'communico_json', 'tribe_events_json', 'localist_json',
                'libcal_ics', 'civic_engage_rss', 'midpen_html', 'usfca_html',
                'calperformances_json', 'berkeley_rep_html', 'ybca_html', 'oakland_html'
            )
        )
        """
    )
