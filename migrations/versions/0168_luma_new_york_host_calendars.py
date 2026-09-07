"""Register the reviewed public Luma host calendars for the New York metro.

Revision ID: 0168
Revises: 0167
Create Date: 2026-08-26

``luma-nyc`` has the identical shape 0163 documented for the Bay Area, measured on the same day:
its Discover cursor exhausts at **90 future events drawn from 89 distinct owner calendars**, about
1.01 events per host.  Walking those 89 calendars returns **462** future events -- a 5.1x gain for
comparable egress -- and every host that the New York directory currently renders with a single
appearance is under-represented by roughly that factor.

The admission rule is the one 0163 established and the coverage eval now enforces: a calendar
carrying **3 or more future events** at review time, excluding personal calendars.  That is 33
calendars and 372 of the 462 events.

``cal-`` identities already registered under a Bay Area key are skipped rather than duplicated --
a calendar is one reviewed cursor regardless of which city feed surfaced it, and two rows pointing
at one calendar would double-count every event it publishes.

``page_limit`` is 30 for the same reason as 0163: the cap is a cliff that discards the whole
refresh, observed page sizes run 10-20 entries, and the largest calendar admitted here needs 8
pages.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0168"
down_revision: str | None = "0167"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Admit the reviewed New York host calendars."""
    op.execute(
        """
        INSERT INTO catalog_sources
            (source_key, display_name, publisher, seed_url, approved_origins, region, mode,
             handoff_only, enabled, reviewed_at, review_expires_at,
             refresh_interval_minutes, min_interval_ms, page_limit, source_revision)
        VALUES
            -- 73 future events over 8 page(s) at review
            ('luma-nyc-claudecommunity', 'Claude Community Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-TOpA5LAFfuDeFpu&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 36 future events over 2 page(s) at review
            ('luma-nyc-accentaccent', 'Accent Is A Superpower (online & offline events)', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-Sc85BAZzQ7GZiZE&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 34 future events over 2 page(s) at review
            ('luma-nyc-brooklyngrange', 'Brooklyn Grange: Sunset Park and Brooklyn Navy Yard', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-pNFenlfEW71zGC5&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 21 future events over 2 page(s) at review
            ('luma-nyc-readingrhythms-manhattan', 'Reading Rhythms NYC', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-Q8l315UsVMWb6h6&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 19 future events over 2 page(s) at review
            ('luma-nyc-active-external', 'Activate Ecosystem Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-gTCvv5vKTHEyvgp&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 17 future events over 1 page(s) at review
            ('luma-nyc-nyaiengineers', 'AI Engineers - NY', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-UJKx9AKCHIKyu7R&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 14 future events over 1 page(s) at review
            ('luma-nyc-pudgypenguins', 'Pudgy Penguins', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-d54V4FW22lMwmB9&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 13 future events over 1 page(s) at review
            ('luma-nyc-b2bnyc', 'NYC B2B: Startup Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-1VimvHYSCVGhHuk&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 12 future events over 1 page(s) at review
            ('luma-nyc-philosophy', 'The New York Philosophy Club', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-dGP82mVkrg1FRmr&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 10 future events over 1 page(s) at review
            ('luma-nyc-mysha-happenings', 'mysha happenings', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-MS5jAaElmqxx6De&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 10 future events over 1 page(s) at review
            ('luma-nyc-kanso', 'Kanso: Phone-Free Experiences', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-fHHfS2rChIZ1IWx&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 9 future events over 1 page(s) at review
            ('luma-nyc-thecanvasnyc', 'The Canvas NYC', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-8pBzUbxu1rqFgsw&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 8 future events over 1 page(s) at review
            ('luma-nyc-andrewsmixers', 'Andrew''s Yeung''s Tech Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-kcWhMbIs2X0Y2Xb&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 7 future events over 1 page(s) at review
            ('luma-nyc-craftnook', 'welcome to the craftnook', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-BvuRd9eaZnDGBwy&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 7 future events over 1 page(s) at review
            ('luma-nyc-sleepawake-events', 'Sleepawake Events Calendar', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-CLXm28Nz1sKiGt8&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 7 future events over 1 page(s) at review
            ('luma-nyc-sosv-events', 'SOSV''s Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-SXakUfDFxvNsTuw&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 7 future events over 1 page(s) at review
            ('luma-nyc-counterspell', 'Counterspell Games', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-fT3eKMrCO9zUul2&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 6 future events over 1 page(s) at review
            ('luma-nyc-obviousai', 'Obvious Community Events', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-0JE8SiQKgmJoT5v&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 6 future events over 1 page(s) at review
            ('luma-nyc-heatmapnews', 'Heatmap News', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-3UdV6WiftPs1INi&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 6 future events over 1 page(s) at review
            ('luma-nyc-unmuted', 'UNMUTED', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-oB3sYoup1LDsPL3&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 5 future events over 1 page(s) at review
            ('luma-nyc-squarespacecircle', 'Squarespace Circle', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-0R3x1gOWKkO7MuH&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 5 future events over 1 page(s) at review
            ('luma-nyc-coworknchillcalendar', 'CoworkNChill Calendar', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-9QAo4pAQjtkRMKM&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 5 future events over 1 page(s) at review
            ('luma-nyc-asrccat', 'Center for Advanced Technology (CAT)', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-DukJHu11ElQhhxx&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 5 future events over 1 page(s) at review
            ('luma-nyc-girlmathcapital', 'Girl Math Capital', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-Puula3MAeqvdYgf&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 4 future events over 1 page(s) at review
            ('luma-nyc-usecorginyc', 'Corgi NYC', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-H1FY60pnZtchaf4&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 4 future events over 1 page(s) at review
            ('luma-nyc-sugarynyc', 'Sugary NYC Community', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-KYNaoLq3pS000fL&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 4 future events over 1 page(s) at review
            ('luma-nyc-cal-rfeqezxd4pnwbe5', 'The Retail Edit', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-rFEqEzxd4PNWBE5&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 3 future events over 1 page(s) at review
            ('luma-nyc-fireworksai', 'Fireworks', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-b0bByM1vbBukIX5&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 3 future events over 1 page(s) at review
            ('luma-nyc-cyberdecktour', 'Cyberdeck Tour', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-b6BifoQGm6tlUim&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 3 future events over 1 page(s) at review
            ('luma-nyc-tpn', 'Tech Power Network', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-bkO33DlatoB8YHE&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 3 future events over 1 page(s) at review
            ('luma-nyc-six-5', 'Six-5 Society', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-dJW8pHtgKfG2HK8&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 3 future events over 1 page(s) at review
            ('luma-nyc-anothertomorrow', 'Another Tomorrow', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-jX1B0ZAFOrl7UEw&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
             true, true, now(), NULL, 360, 1500, 30, 1),
            -- 3 future events over 1 page(s) at review
            ('luma-nyc-nycbackgammonclub', 'NYC Backgammon Club', 'Luma Calendar',
             'https://api.luma.com/calendar/get-items?calendar_api_id=cal-wGlwnmMsVzQQ8tr&pagination_limit=20&period=future',
             ARRAY['https://api.luma.com'], 'new_york_metro', 'luma_calendar_json',
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


def downgrade() -> None:
    """Disable the New York host calendars, keeping their collected evidence addressable."""
    op.execute(
        """
        UPDATE catalog_sources
        SET enabled = false,
            updated_at = now()
        WHERE source_key IN (
            'luma-nyc-claudecommunity',
            'luma-nyc-accentaccent',
            'luma-nyc-brooklyngrange',
            'luma-nyc-readingrhythms-manhattan',
            'luma-nyc-active-external',
            'luma-nyc-nyaiengineers',
            'luma-nyc-pudgypenguins',
            'luma-nyc-b2bnyc',
            'luma-nyc-philosophy',
            'luma-nyc-mysha-happenings',
            'luma-nyc-kanso',
            'luma-nyc-thecanvasnyc',
            'luma-nyc-andrewsmixers',
            'luma-nyc-craftnook',
            'luma-nyc-sleepawake-events',
            'luma-nyc-sosv-events',
            'luma-nyc-counterspell',
            'luma-nyc-obviousai',
            'luma-nyc-heatmapnews',
            'luma-nyc-unmuted',
            'luma-nyc-squarespacecircle',
            'luma-nyc-coworknchillcalendar',
            'luma-nyc-asrccat',
            'luma-nyc-girlmathcapital',
            'luma-nyc-usecorginyc',
            'luma-nyc-sugarynyc',
            'luma-nyc-cal-rfeqezxd4pnwbe5',
            'luma-nyc-fireworksai',
            'luma-nyc-cyberdecktour',
            'luma-nyc-tpn',
            'luma-nyc-six-5',
            'luma-nyc-anothertomorrow',
            'luma-nyc-nycbackgammonclub'
        )
        """
    )
