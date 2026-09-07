"""Register the reviewed public Luma host calendars the city Discover feed cannot carry.

Revision ID: 0163
Revises: 0162
Create Date: 2026-08-26

``luma-sf`` is a *Discover* cursor, and Discover is a curated one-event-per-calendar shelf, not a
feed of any host's programme.  Measured against the live endpoint on 2026-08-26, the whole Bay Area
cursor ends naturally after 4 pages with **81 future events drawn from 75 distinct owner
calendars** -- about 1.08 events per host.  Its ``page_limit`` of 40 is therefore inert: no cadence,
page cap, or ``pagination_limit`` can raise that number, because the ceiling is Luma's, not ours.

That is why every host in the entity directory looks empty.  ``The SF Commons`` renders "2 events"
while ``https://luma.com/thecommons`` publishes **87 approved public future events**, 86 of which
name it as a host.  Catalog-wide, 1,995 of 2,506 entities carry exactly one event, and every entity
we hold comes from one of the two Discover cursors or the two Meetup city pages.

A host calendar is the only public cursor that carries a host's programme, and
``luma_calendar_json`` already walks one.  Walking the 75 calendars Discover named costs 86 paced
requests and yields **390** future events against Discover's 81 -- a 4.8x gain for comparable
egress.  This migration admits the subset whose programme Discover materially under-represents:
**3 or more future events** at review time.  Personal calendars are excluded (an individual's own
page is not an organizational programme), as are the 1-2 event calendars, which Discover already
represents adequately.

``luma-genai-sf`` is re-enabled in the same pass.  Migration 0117 disabled it on the assumption
that the Discover cursor superseded it; it did not.  That calendar publishes 69 future events
today.

Every row here is a reviewed registry entry rather than a code fence.  The adapter derives its
calendar identity from ``seed_url`` and refuses any seed that is not the exact reviewed cursor
shape, so admitting a calendar is an audited ``catalog_sources`` change -- revision-bumped by
``fn_bump_catalog_source_revision`` and written to ``catalog_source_configuration_audit`` -- rather
than an edit to a Python allowlist that no audit trail covers.

``page_limit`` is a **cliff, not a budget**: ``LumaCalendarCatalogFetcher.fetch`` raises and
publishes *nothing* when the last permitted page still reports ``has_more``, so a calendar that
outgrows its cap drops from full coverage to zero rather than degrading.  Every row therefore
carries ``page_limit = 30`` against a worst case of 9 pages measured here -- 3.3x headroom **in
pages**, which is the unit the cap is actually counted in.

In events that is a range, not a number: the cursor returns 10 to 20 entries per page and returns
*fewer* for exactly the largest calendars, so 30 pages is between about 300 and 600 events.  Take
the low end as the real ceiling.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0163"
down_revision: str | None = "0162"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Admit the reviewed host calendars and restore the one 0117 disabled."""
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit, source_revision)
        VALUES
            -- 87 future events over 9 page(s) at review
            ('luma-thecommons', 'The Commons', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-ahTi4ptrN9WCYkg&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 59 future events over 4 page(s) at review
            ('luma-cursorcommunity', 'Cursor Community', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-61Cv6COs4g9GKw7&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 17 future events over 1 page(s) at review
            ('luma-postman-dev-events', 'Postman Developer Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-TGqTNpY4iyl7XYe&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 15 future events over 1 page(s) at review
            ('luma-localeconomy', 'Local Economy', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-7e3UHJX4UgCZaoW&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 15 future events over 1 page(s) at review
            ('luma-thelovepotionlibrary', 'The Love Potion Library', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-9xlm8rlcNcsBdxW&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 10 future events over 1 page(s) at review
            ('luma-usecorgi', 'Corgi', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-AJpfQVFYtwnIIBy&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 9 future events over 1 page(s) at review
            ('luma-cal-z1tslebmjjch4fd', 'WorkOS Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-Z1tslEBMjjCh4fd&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 9 future events over 2 page(s) at review
            ('luma-frontiersyndicate', 'The Frontier Syndicate', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-ffZMpSc1tJiBS9b&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 8 future events over 1 page(s) at review
            ('luma-health-tech-nerds', 'Health Tech Nerds', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-KYMHk5wzM4CQR74&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 7 future events over 1 page(s) at review
            ('luma-stepevents', 'Step', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-DcJSjTzVhNCxcwK&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 7 future events over 1 page(s) at review
            ('luma-gumloop', 'Gumloop', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-kWCZBWXIrWKB518&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 7 future events over 1 page(s) at review
            ('luma-granola', 'Granola', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-ofXMwxW9NLr0RDU&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 7 future events over 1 page(s) at review
            ('luma-posthog', 'PostHog', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-qJCKF7ct5XX3pwB&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 7 future events over 1 page(s) at review
            ('luma-fal', 'fal Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-u3vVIuSFJd7RqNB&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 7 future events over 1 page(s) at review
            ('luma-arizeai', 'Arize AI', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-wjMwGmksR3Zpa83&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 5 future events over 1 page(s) at review
            ('luma-tokensand', 'tokens&', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-EVJ0XV6EJegxAT7&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 5 future events over 1 page(s) at review
            ('luma-wemakedevs', 'WeMakeDevs', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-G9rNvcoz3r4AGfv&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 5 future events over 1 page(s) at review
            ('luma-vercel-events', 'Vercel Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-hp9HP2UFTGNaMnY&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 4 future events over 1 page(s) at review
            ('luma-circe', 'Circe Calendar', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-2vq1HjUj9xZwtBx&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 4 future events over 1 page(s) at review
            ('luma-temporalio', 'Temporal Community', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-Cj9mMU4rSQva1HA&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 4 future events over 1 page(s) at review
            ('luma-readingrhythms-ca', 'Reading Rhythms California', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-Eo35suMbTdQnr20&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 4 future events over 1 page(s) at review
            ('luma-openrouter', 'OpenRouter Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-cv816PW6bxfYMVP&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 4 future events over 1 page(s) at review
            ('luma-tiat', 'tiat (the intersection of art & technology)', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-twiOosdGMMY66DI&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 3 future events over 1 page(s) at review
            ('luma-supabase-community-events', 'Supabase Community Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-8x55tE86VtAD07z&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 3 future events over 1 page(s) at review
            ('luma-agenticaiobservability', 'Agentic + AI Observability', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-EHvDh6vhawZP9k8&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 3 future events over 1 page(s) at review
            ('luma-reductoai', 'Reducto Community Calendar', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-UgzNHDmpkC0jeiz&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 3 future events over 1 page(s) at review
            ('luma-fdotinc', 'Founders, Inc. Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-iHkz5obZdong4ta&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 3 future events over 1 page(s) at review
            ('luma-baseten', 'Baseten Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-l76607AsonZ1F7F&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 3 future events over 1 page(s) at review
            ('luma-sf-hardware-meetup', 'SF Hardware Meetup', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-tFAzNGOZ9xn6kT2&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 3 future events over 1 page(s) at review
            ('luma-cal-zauf7gj3rtecxwr', 'Transit Books', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-zauF7Gj3RTECxwR&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'bay_area_9_county', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1)
        ON CONFLICT (source_key) DO UPDATE
        SET display_name = EXCLUDED.display_name,
            publisher = EXCLUDED.publisher,
            seed_url = EXCLUDED.seed_url,
            approved_origins = EXCLUDED.approved_origins,
            region = EXCLUDED.region,
            mode = EXCLUDED.mode,
            handoff_only = EXCLUDED.handoff_only,
            enabled = EXCLUDED.enabled,
            reviewed_at = EXCLUDED.reviewed_at,
            review_expires_at = EXCLUDED.review_expires_at,
            refresh_interval_minutes = EXCLUDED.refresh_interval_minutes,
            min_interval_ms = EXCLUDED.min_interval_ms,
            page_limit = EXCLUDED.page_limit,
            updated_at = now()
        """
    )
    # 0117 disabled this calendar believing Discover replaced it.  It publishes 69 future events
    # today across 4 pages, and its page_limit of 10 left only 2.5x headroom over that cliff.
    op.execute(
        """
        UPDATE catalog_sources
        SET enabled = true,
            page_limit = 30,
            reviewed_at = now(),
            updated_at = now()
        WHERE source_key = 'luma-genai-sf'
          AND retired_at IS NULL
        """
    )


def downgrade() -> None:
    """Disable the host calendars and restore 0117's Discover-only posture.

    The rows are kept rather than deleted: ``catalog_event_observations`` and
    ``catalog_refresh_runs`` reference ``source_key`` ON DELETE RESTRICT, so deleting them would
    either fail outright or require discarding collected evidence.
    """
    op.execute(
        """
        UPDATE catalog_sources
        SET enabled = false,
            updated_at = now()
        WHERE source_key IN (
            'luma-thecommons',
            'luma-cursorcommunity',
            'luma-postman-dev-events',
            'luma-localeconomy',
            'luma-thelovepotionlibrary',
            'luma-usecorgi',
            'luma-cal-z1tslebmjjch4fd',
            'luma-frontiersyndicate',
            'luma-health-tech-nerds',
            'luma-stepevents',
            'luma-gumloop',
            'luma-granola',
            'luma-posthog',
            'luma-fal',
            'luma-arizeai',
            'luma-tokensand',
            'luma-wemakedevs',
            'luma-vercel-events',
            'luma-circe',
            'luma-temporalio',
            'luma-readingrhythms-ca',
            'luma-openrouter',
            'luma-tiat',
            'luma-supabase-community-events',
            'luma-agenticaiobservability',
            'luma-reductoai',
            'luma-fdotinc',
            'luma-baseten',
            'luma-sf-hardware-meetup',
            'luma-cal-zauf7gj3rtecxwr'
        )
        """
    )
    op.execute(
        """
        UPDATE catalog_sources
        SET enabled = false,
            page_limit = 10,
            updated_at = now()
        WHERE source_key = 'luma-genai-sf'
        """
    )
