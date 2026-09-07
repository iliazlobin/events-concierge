"""Admit canonical social profile URLs into the entity enrichment plane.

Revision ID: 0178
Revises: 0177
Create Date: 2026-08-27

**Why social lives here and not in ``entity_profiles``.**  FR-19.13 governs the *event response
renderer*: for producer-verified entity-profile metadata it admits only "a validated LinkedIn
``/in/`` URL for a person, and a LinkedIn ``/company/`` URL or an explicit HTTPS website for an
organization".  An Instagram or X handle satisfies neither shape, so widening that path would mean
widening ``fn_event_entity_profiles_valid`` (``0128``) and putting an unverifiable third-party URL
into ``canonical_events.entity_profiles`` -- the column the renderer treats as producer-verified and
the column ``0156`` derives ``identity_key`` from.  A social handle is not identity evidence and
must never become an identity key.

``catalog_entity_external_sources`` (``0150``) is the separate enrichment plane and already
legitimately carries ``official_website`` and ``github_public`` rows for the same entities.  It is
keyed by ``entity_id``, read through its own ``SECURITY DEFINER`` functions, and never consulted by
the event renderer, so a social row enriches an entity page without touching the FR-19.13 path.
This revision therefore only widens that table's ``provider_key`` vocabulary; ``entity_profiles``,
``fn_event_entity_profiles_valid``, and ``fn_normalize_profile_url_v1`` are all untouched.

**What the validator accepts.**  ``fn_normalize_social_profile_url_v1`` is written in the style of
``fn_normalize_profile_url_v1``: pure, table-free, ``IMMUTABLE STRICT``, and host-scoped.  It
returns a canonical URL or NULL -- there is no partial repair.  ``twitter.com`` and ``x.com`` are
one platform and normalise to ``https://x.com/<handle>`` under the single ``x_profile`` key.  A
query string, a fragment, a port other than 443, userinfo, or any path beyond the profile segment is
rejected outright rather than trimmed, because trimming a search URL down to something that resolves
is exactly the "derive a profile from a name" failure the catalog rules forbid.

Reserved and system handles are rejected the way ``fn_catalog_profile_url_placeholder_v1``
(``0156``) rejects a placeholder LinkedIn slug: on slug shape alone, never by comparing a slug to a
display name.  Unlike that function this one carries **no** ``char_length(slug) <= 3`` clause.  That
clause is calibrated against LinkedIn, where it catches exactly two live rows with no false
positives; three-character handles are ordinary on X and Instagram.  It is also unnecessary here:
a social row hangs off ``entity_id`` in the enrichment plane and never feeds ``identity_key``, so a
shared handle cannot fuse two entities the way a shared LinkedIn slug can.

**How a row is written.**  ``fn_record_catalog_entity_social_source_v1`` resolves the entity from
``catalog_entity_event_mentions`` -- the row the catalog refresh already minted for this exact
``(source_key, source_event_id, role, observed_name)`` mention.  It therefore invents no identity
and merges nothing by name: it reuses the mapping the catalog itself published, and returns false
rather than creating anything when that mention does not exist.  Rows land as ``linked``, never
``fresh``: nothing here fetches a social platform, and a ``linked`` row is a link we were given, not
a page we read.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0178"
down_revision: str | None = "0177"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESERVED_HANDLE = "public.fn_catalog_social_handle_reserved_v1(text)"
_NORMALIZE_SOCIAL = "public.fn_normalize_social_profile_url_v1(text)"
_SOCIAL_PROVIDER = "public.fn_catalog_social_provider_key_v1(text)"
_RECORD_SOCIAL = (
    "public.fn_record_catalog_entity_social_source_v1(text,text,text,text,text,timestamptz)"
)
_REPLACE = (
    "public.fn_replace_catalog_entity_external_source_v1"
    "(uuid,text,text,text,text,text,timestamptz,timestamptz,text,jsonb)"
)
_PROVIDER_CHECK = "catalog_entity_external_sources_provider_key_check"
_PROVIDER_CHECK_V2 = "ck_catalog_entity_external_sources_provider_key_v2"
_MENTION_LOOKUP_INDEX = "ix_catalog_entity_mentions_social_lookup"

_LEGACY_PROVIDER_KEYS = "'official_website', 'github_public', 'wikidata_public', 'linkedin_profile'"
_ALL_PROVIDER_KEYS = (
    "'official_website', 'github_public', 'wikidata_public', 'linkedin_profile', "
    "'x_profile', 'instagram_profile', 'tiktok_profile', 'youtube_profile'"
)


def upgrade() -> None:
    _create_social_helpers()
    _widen_provider_vocabulary(_ALL_PROVIDER_KEYS, _PROVIDER_CHECK_V2)
    _create_mention_lookup_index()
    _create_record_social()
    for signature in (_RESERVED_HANDLE, _NORMALIZE_SOCIAL, _SOCIAL_PROVIDER, _RECORD_SOCIAL):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")


def downgrade() -> None:
    for signature in (_RECORD_SOCIAL, _SOCIAL_PROVIDER, _NORMALIZE_SOCIAL, _RESERVED_HANDLE):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute(f"DROP INDEX IF EXISTS public.{_MENTION_LOOKUP_INDEX}")
    # The rows have to go before the CHECK narrows again, or the constraint cannot be re-added.
    # Deleting them is safe and complete: a social row is a link, never a fetched fact, so nothing
    # observed only here is lost -- re-running the capture re-derives every one of them.
    op.execute(
        r"""
        DELETE FROM public.catalog_entity_external_sources
        WHERE provider_key IN (
            'x_profile', 'instagram_profile', 'tiktok_profile', 'youtube_profile'
        )
        """
    )
    # Restored under 0150's own constraint name, so a downgrade lands on exactly the
    # privilege and schema surface 0177 left behind.
    _widen_provider_vocabulary(_LEGACY_PROVIDER_KEYS, _PROVIDER_CHECK)


def _widen_provider_vocabulary(provider_keys: str, constraint_name: str) -> None:
    """Re-state the table CHECK and the writer's own guard from one vocabulary string.

    ``fn_replace_catalog_entity_external_source_v1`` repeats the vocabulary inside its validation
    block, so relaxing only the table constraint would leave every social write raising ``22023``.
    Both are re-stated together, from the same source string, so they cannot drift apart.
    """
    op.execute(
        f"ALTER TABLE public.catalog_entity_external_sources "
        f"DROP CONSTRAINT IF EXISTS {_PROVIDER_CHECK}"
    )
    op.execute(
        f"ALTER TABLE public.catalog_entity_external_sources "
        f"DROP CONSTRAINT IF EXISTS {_PROVIDER_CHECK_V2}"
    )
    op.execute(
        rf"""
        ALTER TABLE public.catalog_entity_external_sources
        ADD CONSTRAINT {constraint_name}
        CHECK (provider_key IN ({provider_keys}))
        """
    )
    op.execute(
        rf"""
        CREATE OR REPLACE FUNCTION public.fn_replace_catalog_entity_external_source_v1(
            p_entity_id uuid,
            p_provider_key text,
            p_external_id text,
            p_source_url text,
            p_display_name text,
            p_status text,
            p_fetched_at timestamptz,
            p_next_refresh_at timestamptz,
            p_error_code text,
            p_facts jsonb
        )
        RETURNS void
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_source_id uuid;
        BEGIN
            IF NOT EXISTS (
                    SELECT 1 FROM public.catalog_entities AS entity
                    WHERE entity.entity_id = p_entity_id
                )
               OR p_provider_key NOT IN ({provider_keys})
               OR char_length(coalesce(p_external_id, '')) NOT BETWEEN 1 AND 500
               OR p_external_id ~ '[\x00-\x1f\x7f]'
               OR char_length(coalesce(p_source_url, '')) NOT BETWEEN 9 AND 2048
               OR p_source_url !~ '^https://[^/?#@[:space:]]+(/|[?]|$)'
               OR p_source_url ~ '[\x00-\x1f\x7f[:space:]#]'
               OR char_length(coalesce(p_display_name, '')) NOT BETWEEN 1 AND 160
               OR p_display_name ~ '[\x00-\x1f\x7f]'
               OR p_status NOT IN ('linked', 'fresh', 'failed', 'blocked')
               OR p_next_refresh_at IS NULL
               OR (p_fetched_at IS NOT NULL AND p_next_refresh_at <= p_fetched_at)
               OR (p_status = 'fresh' AND (p_fetched_at IS NULL OR p_error_code IS NOT NULL))
               OR (p_status = 'linked' AND p_error_code IS NOT NULL)
               OR (p_status IN ('failed', 'blocked') AND p_error_code NOT IN (
                    'unavailable', 'rate_limited', 'invalid_response',
                    'unsupported_profile', 'network_policy'
               ))
               OR jsonb_typeof(coalesce(p_facts, '[]'::jsonb)) <> 'array'
               OR jsonb_array_length(coalesce(p_facts, '[]'::jsonb)) > 100
               OR EXISTS (
                    SELECT 1
                    FROM jsonb_array_elements(coalesce(p_facts, '[]'::jsonb)) AS item(value)
                    WHERE jsonb_typeof(item.value) <> 'object'
                       OR item.value->>'key' NOT IN (
                            'description', 'website', 'location', 'country', 'founded',
                            'entity_type', 'focus', 'industry', 'profile', 'job_title',
                            'organization', 'known_for', 'public_repositories', 'followers'
                          )
                       OR char_length(coalesce(item.value->>'value', '')) NOT BETWEEN 1 AND 1000
                       OR item.value->>'value' ~ '[\x00-\x1f\x7f]'
                       OR (
                            nullif(item.value->>'url', '') IS NOT NULL
                            AND (
                                char_length(item.value->>'url') NOT BETWEEN 9 AND 2048
                                OR item.value->>'url' !~ '^https://[^/?#@[:space:]]+(/|[?]|$)'
                                OR item.value->>'url' ~ '[\x00-\x1f\x7f[:space:]#]'
                            )
                       )
                       OR coalesce(item.value->>'order', '0') !~ '^[0-9]{{1,3}}$'
                       OR CASE
                            WHEN coalesce(item.value->>'order', '0') ~ '^[0-9]{{1,3}}$'
                            THEN (item.value->>'order')::integer NOT BETWEEN 0 AND 999
                            ELSE false
                          END
               )
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'entity source snapshot is invalid';
            END IF;

            INSERT INTO public.catalog_entity_external_sources (
                entity_id, provider_key, external_id, source_url, display_name,
                status, fetched_at, next_refresh_at, error_code
            ) VALUES (
                p_entity_id, p_provider_key, p_external_id, p_source_url, p_display_name,
                p_status, p_fetched_at, p_next_refresh_at, p_error_code
            )
            ON CONFLICT (entity_id, provider_key) DO UPDATE
            SET external_id = EXCLUDED.external_id,
                source_url = EXCLUDED.source_url,
                display_name = EXCLUDED.display_name,
                status = EXCLUDED.status,
                fetched_at = EXCLUDED.fetched_at,
                next_refresh_at = EXCLUDED.next_refresh_at,
                error_code = EXCLUDED.error_code,
                updated_at = clock_timestamp()
            RETURNING source_id INTO v_source_id;

            DELETE FROM public.catalog_entity_external_facts AS fact
            WHERE fact.source_id = v_source_id;

            INSERT INTO public.catalog_entity_external_facts (
                source_id, entity_id, fact_key, fact_value, fact_url, sort_order, observed_at
            )
            SELECT v_source_id,
                   p_entity_id,
                   item.value->>'key',
                   item.value->>'value',
                   nullif(item.value->>'url', ''),
                   coalesce((item.value->>'order')::integer, 0),
                   coalesce(p_fetched_at, clock_timestamp())
            FROM jsonb_array_elements(coalesce(p_facts, '[]'::jsonb)) AS item(value);
        END;
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION {_REPLACE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_REPLACE} TO ec_app")


def _create_mention_lookup_index() -> None:
    """Cover the mention lookup ``fn_record_catalog_entity_social_source_v1`` issues per link.

    That lookup filters on ``(source_key, source_event_id, role, lower(btrim(observed_name)))``.
    None of the three indexes the table already carries is usable for it: the primary key leads with
    ``entity_id``, ``ix_catalog_entity_mentions_entity_time`` leads with ``entity_id``, and
    ``ix_catalog_entity_mentions_event`` leads with ``canonical_event_id`` and indexes
    ``lower(observed_name)`` rather than ``lower(btrim(observed_name))``.  Without this index the
    "single-row lookup" the function documents is a full scan of the pkey, and its cost grows with
    the mentions table on every link of every event in a refresh -- there is no statement_timeout to
    bound it, so the bound has to be the index.
    """
    op.execute(
        rf"""
        CREATE INDEX IF NOT EXISTS {_MENTION_LOOKUP_INDEX}
        ON public.catalog_entity_event_mentions (
            source_key, source_event_id, role, lower(btrim(observed_name))
        )
        """
    )


def _create_social_helpers() -> None:
    """Pure, table-free URL validators.  IMMUTABLE STRICT so the planner can inline and fold them."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_catalog_social_handle_reserved_v1(p_handle text)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        STRICT
        SET search_path = pg_catalog, public
        AS $$
            -- Handle shape only.  Never reads a display name: a name is display data, not identity.
            -- The list covers the platform routes that would otherwise read as a profile ('home',
            -- 'explore', 'search', 'i', 'intent', 'share', ...) plus the "no answer" tokens sources
            -- emit in a handle field.  There is deliberately no length clause here -- see the
            -- revision docstring.
            SELECT lower(btrim(p_handle)) IN (
                'about', 'account', 'accounts', 'admin', 'api', 'c', 'channel', 'channels',
                'contact', 'discover', 'directory', 'download', 'embed', 'example', 'explore',
                'feed', 'feeds', 'following', 'foryou', 'help', 'home', 'i', 'intent', 'legal',
                'live', 'login', 'logout', 'me', 'messages', 'music', 'na', 'n-a', 'new', 'nil',
                'none', 'notifications', 'null', 'p', 'playlist', 'policies', 'privacy', 'profile',
                'reel', 'reels', 'results', 'search', 'session', 'settings', 'share', 'shorts',
                'signup', 'stories', 'support', 'tag', 'terms', 'test', 'tos', 'trending', 'tv',
                'undefined', 'unknown', 'upload', 'user', 'username', 'video', 'watch', 'www'
            )
        $$
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION public.fn_normalize_social_profile_url_v1(p_url text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        STRICT
        SET search_path = pg_catalog, public
        AS $$
            -- Returns the one canonical URL for a platform profile, or NULL.  There is no partial
            -- repair: a value that is not already a profile URL is rejected, never trimmed into
            -- one.  Host-scoped like fn_normalize_profile_url_v1, and query/fragment bearing URLs
            -- are refused outright rather than stripped, so a search URL can never survive as a
            -- profile.
            WITH candidate AS (
                SELECT lower(btrim(p_url)) AS url
            ), shaped AS (
                SELECT url,
                       substring(url from '^https://([^/?#@[:space:]]+)') AS host,
                       coalesce(substring(url from '^https://[^/?#@[:space:]]+(/[^?#]*)'), '')
                           AS path
                FROM candidate
                WHERE url ~ '^https://[^/?#@[:space:]]+(/[^?#]*)?$'
                  AND url !~ '[[:cntrl:][:space:]]'
                  AND char_length(url) <= 2048
            ), parts AS (
                SELECT host,
                       -- Trailing slashes collapse; every other path character is significant.
                       regexp_replace(path, '/+$', '') AS path
                FROM shaped
                WHERE host IN (
                          'x.com', 'www.x.com', 'twitter.com', 'www.twitter.com',
                          'instagram.com', 'www.instagram.com',
                          'tiktok.com', 'www.tiktok.com',
                          'youtube.com', 'www.youtube.com', 'm.youtube.com'
                      )
            )
            SELECT CASE
                WHEN parts.host IN ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com')
                     AND parts.path ~ '^/[a-z0-9_]{1,15}$'
                     AND NOT public.fn_catalog_social_handle_reserved_v1(substring(parts.path from 2))
                    THEN 'https://x.com' || parts.path
                WHEN parts.host IN ('instagram.com', 'www.instagram.com')
                     AND parts.path ~ '^/[a-z0-9_][a-z0-9_.]{0,29}$'
                     AND parts.path !~ '[.][.]'
                     AND parts.path !~ '[.]$'
                     AND NOT public.fn_catalog_social_handle_reserved_v1(substring(parts.path from 2))
                    THEN 'https://www.instagram.com' || parts.path
                WHEN parts.host IN ('tiktok.com', 'www.tiktok.com')
                     AND parts.path ~ '^/@[a-z0-9_][a-z0-9_.]{0,23}$'
                     AND parts.path !~ '[.][.]'
                     AND parts.path !~ '[.]$'
                     AND NOT public.fn_catalog_social_handle_reserved_v1(substring(parts.path from 3))
                    THEN 'https://www.tiktok.com' || parts.path
                WHEN parts.host IN ('youtube.com', 'www.youtube.com', 'm.youtube.com')
                     AND parts.path ~ '^/@[a-z0-9][a-z0-9_.-]{2,29}$'
                     AND parts.path !~ '[.][.]'
                     AND parts.path !~ '[.]$'
                     AND NOT public.fn_catalog_social_handle_reserved_v1(substring(parts.path from 3))
                    THEN 'https://www.youtube.com' || parts.path
                -- A channel id is the one YouTube identifier that is case sensitive, so it is
                -- matched against the original spelling rather than the lowered one.
                WHEN parts.host IN ('youtube.com', 'www.youtube.com', 'm.youtube.com')
                     AND parts.path ~ '^/channel/uc[a-z0-9_-]{22}$'
                    THEN 'https://www.youtube.com/channel/' || substring(
                             regexp_replace(btrim(p_url), '/+$', '') from '(UC[A-Za-z0-9_-]{22})$'
                         )
                WHEN parts.host IN ('youtube.com', 'www.youtube.com', 'm.youtube.com')
                     AND parts.path ~ '^/(c|user)/[a-z0-9_-]{1,60}$'
                     AND NOT public.fn_catalog_social_handle_reserved_v1(
                             -- Matches any single path segment rather than spelling the two names
                             -- out again with a non-capturing group. Alembic sends this through
                             -- SQLAlchemy text(), whose bind-parameter scanner treats a colon
                             -- followed by a word inside a non-capturing group as a placeholder and
                             -- refuses the statement -- and it scans comments too, so the offending
                             -- form cannot even be named here. The branch above already asserts the
                             -- segment is one of the two allowed names, so this is equivalent and
                             -- keeps the handle as capture group 1. POSIX bracket classes are safe.
                             substring(parts.path from '^/[^/]+/(.*)$')
                         )
                    THEN 'https://www.youtube.com' || parts.path
                ELSE NULL
            END
            FROM parts
        $$
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION public.fn_catalog_social_provider_key_v1(p_url text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        STRICT
        SET search_path = pg_catalog, public
        AS $$
            -- Derived from the canonical form only, so twitter.com and x.com resolve to one key.
            SELECT CASE
                WHEN canonical.url LIKE 'https://x.com/%' THEN 'x_profile'
                WHEN canonical.url LIKE 'https://www.instagram.com/%' THEN 'instagram_profile'
                WHEN canonical.url LIKE 'https://www.tiktok.com/%' THEN 'tiktok_profile'
                WHEN canonical.url LIKE 'https://www.youtube.com/%' THEN 'youtube_profile'
                ELSE NULL
            END
            FROM (SELECT public.fn_normalize_social_profile_url_v1(p_url) AS url) AS canonical
        $$
        """
    )


def _create_record_social() -> None:
    op.execute(
        r"""
        CREATE FUNCTION public.fn_record_catalog_entity_social_source_v1(
            p_source_key text,
            p_source_event_id text,
            p_role text,
            p_observed_name text,
            p_social_url text,
            p_next_refresh_at timestamptz
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_url text;
            v_provider_key text;
            v_entity_id uuid;
        BEGIN
            v_url := public.fn_normalize_social_profile_url_v1(coalesce(p_social_url, ''));
            v_provider_key := public.fn_catalog_social_provider_key_v1(coalesce(p_social_url, ''));
            IF v_url IS NULL
               OR v_provider_key IS NULL
               OR p_next_refresh_at IS NULL
               OR p_role NOT IN ('organizer', 'host', 'speaker', 'partner')
               OR char_length(coalesce(p_observed_name, '')) NOT BETWEEN 1 AND 160
               OR char_length(coalesce(p_source_key, '')) NOT BETWEEN 1 AND 200
               OR char_length(coalesce(p_source_event_id, '')) NOT BETWEEN 1 AND 500
            THEN
                -- A value that does not validate is dropped, never stored raw.
                RETURN false;
            END IF;

            -- The entity comes from the mention the catalog refresh already minted for this exact
            -- (source, event, role, name).  Nothing here merges by name or mints an entity: if the
            -- catalog has not resolved this mention yet, the caller simply gets false and retries
            -- after the next refresh.  The cap is the single-row lookup itself; there is no scan --
            -- ix_catalog_entity_mentions_social_lookup below covers exactly this predicate,
            -- expression included, so the cost does not grow with the mentions table.
            SELECT mention.entity_id
            INTO v_entity_id
            FROM public.catalog_entity_event_mentions AS mention
            WHERE mention.source_key = p_source_key
              AND mention.source_event_id = p_source_event_id
              AND mention.role = p_role
              AND lower(btrim(mention.observed_name)) = lower(btrim(p_observed_name))
            ORDER BY mention.observed_at DESC
            LIMIT 1;

            IF v_entity_id IS NULL THEN
                RETURN false;
            END IF;

            -- status is always 'linked': nothing fetches a social platform, so there is no
            -- fetched_at to report and no facts to store beside the link.
            INSERT INTO public.catalog_entity_external_sources (
                entity_id, provider_key, external_id, source_url, display_name,
                status, fetched_at, next_refresh_at, error_code
            ) VALUES (
                v_entity_id,
                v_provider_key,
                -- One spelling for a social identity across both write paths.  The Python
                -- enrichment path (``social_profiles._profile``) mints ``<platform>:<path>``, the
                -- same shape ``linkedin:``/``github:``/``website:`` already use, and both paths
                -- upsert on (entity_id, provider_key) with DO UPDATE SET external_id -- so a
                -- second spelling here would make the column flip between refreshes.
                left(
                    replace(v_provider_key, '_profile', '')
                        || ':'
                        || coalesce(substring(v_url from '^https://[^/]+/(.*)$'), ''),
                    500
                ),
                v_url,
                CASE v_provider_key
                    WHEN 'x_profile' THEN 'X'
                    WHEN 'instagram_profile' THEN 'Instagram'
                    WHEN 'tiktok_profile' THEN 'TikTok'
                    ELSE 'YouTube'
                END,
                'linked',
                NULL,
                p_next_refresh_at,
                NULL
            )
            ON CONFLICT (entity_id, provider_key) DO UPDATE
            SET external_id = EXCLUDED.external_id,
                source_url = EXCLUDED.source_url,
                display_name = EXCLUDED.display_name,
                status = EXCLUDED.status,
                fetched_at = EXCLUDED.fetched_at,
                next_refresh_at = EXCLUDED.next_refresh_at,
                error_code = EXCLUDED.error_code,
                updated_at = clock_timestamp();
            RETURN true;
        END;
        $$
        """
    )
