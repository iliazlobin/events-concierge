"""Add a capability-only ingestion-admin read model and durable command queue.

Revision ID: 0109
Revises: 0108
Create Date: 2026-07-23

The runtime role receives no table access. Fixed SECURITY DEFINER projections expose reviewed
public-source operations without raw provider errors, while a leased command queue admits only a
single-source refresh or one fixture-filtered due-source pass. Source review, source enablement,
and fleet policy remain owner-controlled outside this capability.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0109"
down_revision: str | None = "0108"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SOURCE_BASE_SIGNATURE = "(boolean)"
_SOURCE_LIST_SIGNATURE = "(text,text,text,boolean,integer,integer)"
_SOURCE_COUNT_SIGNATURE = "(text,text,text,boolean)"
_DUE_SIGNATURE = "(timestamptz,integer)"
_RUN_LIST_SIGNATURE = "(text,text,boolean,integer,integer)"
_RUN_COUNT_SIGNATURE = "(text,text,boolean)"
_RUN_GET_SIGNATURE = "(text,text)"
_COMMAND_LIST_SIGNATURE = "(integer)"
_COMMAND_GET_SIGNATURE = "(uuid)"
_ENQUEUE_SIGNATURE = "(uuid,text,text,text)"
_CLAIM_SIGNATURE = "(integer,integer)"
_COMPLETE_SIGNATURE = "(uuid,uuid,jsonb)"
_FAIL_SIGNATURE = "(uuid,uuid,text)"
_RUN_FIXTURE_SIGNATURE = "(text,text)"


def upgrade() -> None:
    """Install bounded read projections and lease-fenced ingestion commands."""
    _create_private_helpers()
    _create_command_table()
    _create_source_read_capabilities()
    _create_run_read_capabilities()
    _create_command_read_capabilities()
    _create_command_write_capabilities()
    _grant_capabilities()


def downgrade() -> None:
    """Remove only the admin projection/queue, leaving catalog authority untouched."""
    for name, signature in (
        ("fn_fail_ingestion_admin_command", _FAIL_SIGNATURE),
        ("fn_complete_ingestion_admin_command", _COMPLETE_SIGNATURE),
        ("fn_claim_ingestion_admin_commands", _CLAIM_SIGNATURE),
        ("fn_enqueue_ingestion_admin_command", _ENQUEUE_SIGNATURE),
        ("fn_get_ingestion_admin_command", _COMMAND_GET_SIGNATURE),
        ("fn_list_ingestion_admin_commands", _COMMAND_LIST_SIGNATURE),
        ("fn_get_ingestion_admin_run", _RUN_GET_SIGNATURE),
        ("fn_count_ingestion_admin_runs", _RUN_COUNT_SIGNATURE),
        ("fn_list_ingestion_admin_runs", _RUN_LIST_SIGNATURE),
        ("fn_list_ingestion_admin_due_sources", _DUE_SIGNATURE),
        ("fn_count_ingestion_admin_sources", _SOURCE_COUNT_SIGNATURE),
        ("fn_list_ingestion_admin_sources", _SOURCE_LIST_SIGNATURE),
        ("fn_get_ingestion_admin_overview", "()"),
        ("fn_ingestion_admin_sources_base", _SOURCE_BASE_SIGNATURE),
    ):
        op.execute(f"DROP FUNCTION IF EXISTS public.{name}{signature}")
    op.execute("DROP TABLE IF EXISTS public.ingestion_admin_commands")
    op.execute("DROP FUNCTION IF EXISTS public.fn_ingestion_admin_result_is_valid(jsonb)")
    op.execute(
        f"DROP FUNCTION IF EXISTS public.fn_ingestion_admin_run_is_fixture{_RUN_FIXTURE_SIGNATURE}"
    )
    op.execute("DROP FUNCTION IF EXISTS public.fn_normalize_catalog_refresh_error(text)")
    op.execute(
        "DROP FUNCTION IF EXISTS public.fn_ingestion_admin_source_is_fixture(text,text,text)"
    )


def _create_private_helpers() -> None:
    """Create closed helpers used only from owner-defined projections and transitions."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_ingestion_admin_source_is_fixture(
            p_source_key text,
            p_publisher text,
            p_seed_url text
        )
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
        AS $$
            SELECT COALESCE(p_source_key LIKE 'test-%', false)
                OR lower(btrim(COALESCE(p_publisher, ''))) = 'tests'
                OR lower(COALESCE(p_seed_url, ''))
                    ~ '^https://([^/]+\.)?example\.test(?::[0-9]+)?(?:/|$)'
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_normalize_catalog_refresh_error(p_error text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
        AS $$
            SELECT CASE
                WHEN p_error IS NULL OR btrim(p_error) = '' THEN NULL
                WHEN lower(p_error) LIKE '%fixture%' THEN 'test_fixture'
                WHEN lower(p_error) LIKE '%rate limit%'
                  OR lower(p_error) LIKE '%retry-after%' THEN 'source_rate_limited'
                WHEN lower(p_error) LIKE '%access denied%'
                  OR lower(p_error) LIKE '%forbidden%'
                  OR lower(p_error) LIKE '%quarantine%' THEN 'source_access_denied'
                WHEN lower(p_error) LIKE '%policy denied%'
                  OR lower(p_error) LIKE '%automation not allowed%' THEN 'policy_blocked'
                WHEN lower(p_error) LIKE 'pacer %'
                  OR lower(p_error) LIKE '%pacer unavailable%' THEN 'pacer_deferred'
                WHEN lower(p_error) LIKE '%lease%' THEN 'lease_lost'
                WHEN lower(p_error) LIKE '%source revision%'
                  OR lower(p_error) LIKE '%source changed%'
                  OR lower(p_error) LIKE '%request contract changed%' THEN 'source_changed'
                WHEN lower(p_error) LIKE '%page cap%'
                  OR lower(p_error) LIKE '%cap exceeded%' THEN 'page_cap_exceeded'
                WHEN lower(p_error) LIKE '%parse%'
                  OR lower(p_error) LIKE '%schema%' THEN 'source_schema_invalid'
                WHEN lower(p_error) LIKE '%timeout%'
                  OR lower(p_error) LIKE '%timed out%' THEN 'source_timeout'
                ELSE 'refresh_failed'
            END
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_ingestion_admin_result_is_valid(p_result jsonb)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
        AS $$
            SELECT p_result IS NOT NULL
               AND jsonb_typeof(p_result) = 'object'
               AND octet_length(p_result::text) <= 4096
               AND p_result ? 'action'
               AND jsonb_typeof(p_result -> 'action') = 'string'
               AND NOT EXISTS (
                   SELECT 1
                   FROM jsonb_object_keys(p_result) AS key(name)
                   WHERE key.name NOT IN (
                       'action', 'source_key', 'run_key', 'outcome',
                       'candidate_count', 'canonical_count', 'retry_after_seconds',
                       'due_sources', 'attempted', 'succeeded', 'queued', 'skipped',
                       'deferred', 'already_succeeded', 'busy', 'progressed', 'failed'
                   )
               )
               AND NOT EXISTS (
                   SELECT 1
                   FROM jsonb_each(p_result) AS entry(name, value)
                   WHERE jsonb_typeof(entry.value) NOT IN ('string', 'number', 'boolean', 'null')
                      OR (
                          jsonb_typeof(entry.value) = 'string'
                          AND octet_length(entry.value #>> '{}') > 512
                      )
                      OR (
                          jsonb_typeof(entry.value) = 'number'
                          AND entry.value::text !~ '^[0-9]+([.][0-9]+)?$'
                      )
               )
               AND NOT EXISTS (
                   SELECT 1
                   FROM jsonb_each(p_result) AS entry(name, value)
                   WHERE entry.name IN (
                           'candidate_count', 'canonical_count', 'retry_after_seconds',
                           'due_sources', 'attempted', 'succeeded', 'queued', 'skipped',
                           'deferred', 'already_succeeded', 'busy', 'progressed', 'failed'
                       )
                     AND (
                         jsonb_typeof(entry.value) <> 'number'
                         OR entry.value::text !~ '^[0-9]+$'
                     )
               )
               AND (
                   NOT (p_result ? 'source_key')
                   OR jsonb_typeof(p_result -> 'source_key') = 'string'
               )
               AND (
                   NOT (p_result ? 'run_key')
                   OR jsonb_typeof(p_result -> 'run_key') = 'string'
               )
               AND (
                   NOT (p_result ? 'outcome')
                   OR jsonb_typeof(p_result -> 'outcome') = 'string'
               )
        $$
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION public.fn_ingestion_admin_run_is_fixture(
            p_run_key text,
            p_error text
        )
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog
        AS $$
            SELECT COALESCE(
                       p_run_key
                           ~ '^manual:(p[0-9]+|paged-(stage|promotion)-contract-race-)',
                       false
                   )
                OR COALESCE(
                    public.fn_normalize_catalog_refresh_error(p_error) = 'test_fixture',
                    false
                )
        $$
        """
    )
    for signature in (
        "public.fn_ingestion_admin_source_is_fixture(text,text,text)",
        "public.fn_normalize_catalog_refresh_error(text)",
        "public.fn_ingestion_admin_result_is_valid(jsonb)",
        f"public.fn_ingestion_admin_run_is_fixture{_RUN_FIXTURE_SIGNATURE}",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")


def _create_command_table() -> None:
    """Persist only fixed command identity, lease state, and bounded safe results."""
    op.execute(
        """
        CREATE TABLE public.ingestion_admin_commands (
            command_id       uuid PRIMARY KEY,
            action           text NOT NULL
                             CHECK (action IN ('refresh_source', 'refresh_due')),
            source_key       text REFERENCES public.catalog_sources(source_key)
                             ON DELETE RESTRICT,
            requested_by     text NOT NULL
                             CHECK (
                                 char_length(requested_by) BETWEEN 1 AND 256
                                 AND requested_by !~ '[[:cntrl:]]'
                             ),
            status           text NOT NULL DEFAULT 'queued'
                             CHECK (status IN ('queued', 'running', 'completed', 'failed')),
            requested_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
            started_at       timestamptz,
            completed_at     timestamptz,
            attempt_count    integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
            lease_token      uuid,
            lease_expires_at timestamptz,
            result           jsonb,
            error_code       text,
            CHECK (
                (action = 'refresh_source' AND source_key IS NOT NULL)
                OR (action = 'refresh_due' AND source_key IS NULL)
            ),
            CHECK ((lease_token IS NULL) = (lease_expires_at IS NULL)),
            CHECK (result IS NULL OR public.fn_ingestion_admin_result_is_valid(result)),
            CHECK (
                error_code IS NULL
                OR error_code IN (
                    'source_refresh_failed',
                    'due_refresh_failed',
                    'invalid_result',
                    'worker_unavailable'
                )
            ),
            CHECK (
                (status = 'queued'
                    AND started_at IS NULL
                    AND completed_at IS NULL
                    AND lease_token IS NULL
                    AND result IS NULL
                    AND error_code IS NULL)
                OR
                (status = 'running'
                    AND started_at IS NOT NULL
                    AND completed_at IS NULL
                    AND lease_token IS NOT NULL
                    AND result IS NULL
                    AND error_code IS NULL)
                OR
                (status = 'completed'
                    AND started_at IS NOT NULL
                    AND completed_at IS NOT NULL
                    AND lease_token IS NULL
                    AND result IS NOT NULL
                    AND error_code IS NULL)
                OR
                (status = 'failed'
                    AND started_at IS NOT NULL
                    AND completed_at IS NOT NULL
                    AND lease_token IS NULL
                    AND result IS NULL
                    AND error_code IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_ingestion_admin_commands_claim
        ON public.ingestion_admin_commands (requested_at, command_id)
        WHERE status IN ('queued', 'running')
        """
    )
    op.execute("REVOKE ALL ON TABLE public.ingestion_admin_commands FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.ingestion_admin_commands FROM ec_app")


def _create_source_read_capabilities() -> None:
    """Project fixture-aware source health, overview aggregates, and safe due slots."""
    op.execute(
        """
        CREATE FUNCTION public.fn_ingestion_admin_sources_base(p_include_fixtures boolean)
        RETURNS TABLE (
            source_key text,
            display_name text,
            publisher text,
            mode text,
            region text,
            seed_url text,
            enabled boolean,
            review_status text,
            effective_status text,
            policy_blocked boolean,
            due boolean,
            last_succeeded_at timestamptz,
            next_due_at timestamptz,
            event_count bigint,
            latest_run_key text,
            latest_run_status text,
            latest_run_started_at timestamptz,
            latest_run_completed_at timestamptz,
            latest_run_candidate_count integer,
            latest_run_canonical_count integer,
            latest_run_error text,
            latest_run_attempt_count integer,
            is_fixture boolean
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH observed AS (
                SELECT pg_catalog.statement_timestamp() AS observed_at
            ), policy AS (
                SELECT policy_row.source IS NOT NULL AS present,
                       COALESCE(policy_row.quarantined, false) AS quarantined,
                       COALESCE(
                           (policy_row.automation_allowed ->> 'browser')::boolean,
                           false
                       ) AS browser_allowed
                FROM (SELECT 1) AS singleton
                LEFT JOIN public.source_policy AS policy_row
                  ON policy_row.source = 'public_jsonld'
            ), source_facts AS MATERIALIZED (
                SELECT source.*,
                       public.fn_ingestion_admin_source_is_fixture(
                           source.source_key, source.publisher, source.seed_url
                       ) AS fixture
                FROM public.catalog_sources AS source
            ), selected_sources AS MATERIALIZED (
                SELECT source.*
                FROM source_facts AS source
                WHERE p_include_fixtures OR NOT source.fixture
            ), latest_success AS (
                SELECT DISTINCT ON (refresh.source_key)
                       refresh.source_key,
                       refresh.completed_at
                FROM public.catalog_refresh_runs AS refresh
                JOIN selected_sources AS source
                  ON source.source_key = refresh.source_key
                WHERE refresh.status = 'succeeded'
                  AND refresh.completed_at IS NOT NULL
                  AND (
                      p_include_fixtures
                      OR NOT public.fn_ingestion_admin_run_is_fixture(
                          refresh.run_key, refresh.error
                      )
                  )
                ORDER BY refresh.source_key, refresh.completed_at DESC, refresh.run_key DESC
            ), latest_run AS (
                SELECT DISTINCT ON (refresh.source_key)
                       refresh.source_key,
                       refresh.run_key,
                       refresh.status,
                       refresh.started_at,
                       refresh.lease_expires_at,
                       refresh.completed_at,
                       refresh.candidate_count,
                       refresh.canonical_count,
                       refresh.error,
                       refresh.attempt_count
                FROM public.catalog_refresh_runs AS refresh
                JOIN selected_sources AS source
                  ON source.source_key = refresh.source_key
                WHERE p_include_fixtures
                   OR NOT public.fn_ingestion_admin_run_is_fixture(
                       refresh.run_key, refresh.error
                   )
                ORDER BY refresh.source_key, refresh.started_at DESC, refresh.run_key DESC
            ), observation_counts AS (
                SELECT observation.source_key,
                       count(DISTINCT observation.canonical_event_id) AS event_count
                FROM public.catalog_event_observations AS observation
                JOIN selected_sources AS source
                  ON source.source_key = observation.source_key
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = observation.canonical_event_id
                JOIN public.catalog_refresh_runs AS observation_run
                  ON observation_run.source_key = observation.source_key
                 AND observation_run.run_key = observation.last_run_key
                CROSS JOIN observed
                WHERE event.start_at >= observed.observed_at
                  AND event.event_status <> 'cancelled'
                  AND (
                      p_include_fixtures
                      OR NOT public.fn_ingestion_admin_run_is_fixture(
                          observation_run.run_key, observation_run.error
                      )
                  )
                GROUP BY observation.source_key
            ), facts AS (
                SELECT source.*,
                       success.completed_at AS succeeded_at,
                       COALESCE(
                           success.completed_at
                               + source.refresh_interval_minutes * INTERVAL '1 minute',
                           source.reviewed_at
                       ) AS due_at,
                       latest.run_key AS run_key,
                       CASE
                           WHEN latest.status = 'running'
                            AND (
                                latest.lease_expires_at IS NULL
                                OR latest.lease_expires_at <= observed.observed_at
                            )
                               THEN 'failed'
                           ELSE latest.status
                       END AS run_status,
                       latest.started_at AS run_started_at,
                       CASE
                           WHEN latest.status = 'running'
                            AND (
                                latest.lease_expires_at IS NULL
                                OR latest.lease_expires_at <= observed.observed_at
                            )
                               THEN COALESCE(latest.lease_expires_at, latest.started_at)
                           ELSE latest.completed_at
                       END AS run_completed_at,
                       latest.candidate_count AS run_candidate_count,
                       latest.canonical_count AS run_canonical_count,
                       CASE
                           WHEN latest.status = 'running'
                            AND (
                                latest.lease_expires_at IS NULL
                                OR latest.lease_expires_at <= observed.observed_at
                            )
                               THEN 'lease expired'
                           ELSE latest.error
                       END AS run_error,
                       latest.attempt_count AS run_attempt_count,
                       policy.present,
                       policy.quarantined,
                       policy.browser_allowed,
                       COALESCE(observations.event_count, 0) AS source_event_count,
                       observed.observed_at
                FROM selected_sources AS source
                CROSS JOIN policy
                CROSS JOIN observed
                LEFT JOIN latest_success AS success
                  ON success.source_key = source.source_key
                LEFT JOIN latest_run AS latest
                  ON latest.source_key = source.source_key
                LEFT JOIN observation_counts AS observations
                  ON observations.source_key = source.source_key
            )
            SELECT facts.source_key,
                   facts.display_name,
                   facts.publisher,
                   facts.mode,
                   facts.region,
                   facts.seed_url,
                   facts.enabled,
                   CASE
                       WHEN facts.reviewed_at IS NULL THEN 'unreviewed'
                       WHEN facts.review_expires_at IS NOT NULL
                        AND facts.review_expires_at <= facts.observed_at THEN 'expired'
                       ELSE 'reviewed'
                   END AS review_status,
                   CASE
                       WHEN NOT facts.enabled THEN 'disabled'
                       WHEN facts.reviewed_at IS NULL THEN 'unreviewed'
                       WHEN facts.review_expires_at IS NOT NULL
                        AND facts.review_expires_at <= facts.observed_at THEN 'review_expired'
                       WHEN NOT facts.present OR facts.quarantined OR NOT facts.browser_allowed
                           THEN 'policy_blocked'
                       WHEN facts.run_status = 'running' THEN 'running'
                       WHEN facts.due_at IS NOT NULL AND facts.due_at <= facts.observed_at THEN 'due'
                       ELSE 'active'
                   END AS effective_status,
                   NOT facts.present OR facts.quarantined OR NOT facts.browser_allowed
                       AS policy_blocked,
                   (
                       facts.enabled
                       AND facts.handoff_only
                       AND facts.reviewed_at IS NOT NULL
                       AND (
                           facts.review_expires_at IS NULL
                           OR facts.review_expires_at > facts.observed_at
                       )
                       AND facts.due_at IS NOT NULL
                       AND facts.due_at <= facts.observed_at
                   ) AS due,
                   facts.succeeded_at,
                   facts.due_at,
                   facts.source_event_count,
                   facts.run_key,
                   facts.run_status,
                   facts.run_started_at,
                   facts.run_completed_at,
                   facts.run_candidate_count,
                   facts.run_canonical_count,
                   public.fn_normalize_catalog_refresh_error(facts.run_error),
                   facts.run_attempt_count,
                   facts.fixture
            FROM facts
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_sources(
            p_query text,
            p_state text,
            p_source_key text,
            p_include_fixtures boolean,
            p_limit integer,
            p_offset integer
        )
        RETURNS TABLE (
            source_key text,
            display_name text,
            publisher text,
            mode text,
            region text,
            seed_url text,
            enabled boolean,
            review_status text,
            effective_status text,
            policy_blocked boolean,
            due boolean,
            last_succeeded_at timestamptz,
            next_due_at timestamptz,
            event_count bigint,
            latest_run_key text,
            latest_run_status text,
            latest_run_started_at timestamptz,
            latest_run_completed_at timestamptz,
            latest_run_candidate_count integer,
            latest_run_canonical_count integer,
            latest_run_error text,
            latest_run_attempt_count integer,
            total_count bigint
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_include_fixtures IS NULL
               OR p_limit IS NULL OR p_limit < 1 OR p_limit > 100
               OR p_offset IS NULL OR p_offset < 0 OR p_offset > 100000
               OR (p_query IS NOT NULL AND char_length(p_query) > 200)
               OR p_state IS NULL
               OR p_state NOT IN ('all', 'active', 'due', 'blocked', 'failed')
               OR (
                   p_source_key IS NOT NULL
                   AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin source query is invalid';
            END IF;

            RETURN QUERY
            WITH filtered AS (
                SELECT source.*
                FROM public.fn_ingestion_admin_sources_base(p_include_fixtures) AS source
                WHERE (
                        p_query IS NULL
                        OR strpos(
                            lower(
                                source.source_key || ' ' || source.display_name || ' '
                                || source.publisher || ' ' || source.region
                            ),
                            lower(p_query)
                        ) > 0
                    )
                  AND (p_source_key IS NULL OR source.source_key = p_source_key)
                  AND (
                      p_state = 'all'
                      OR (
                          p_state = 'active'
                          AND source.effective_status IN ('active', 'due', 'running')
                      )
                      OR (p_state = 'due' AND source.due)
                      OR (
                          p_state = 'blocked'
                          AND source.effective_status IN (
                              'disabled', 'unreviewed', 'review_expired', 'policy_blocked'
                          )
                      )
                      OR (p_state = 'failed' AND source.latest_run_status = 'failed')
                  )
            )
            SELECT filtered.source_key,
                   filtered.display_name,
                   filtered.publisher,
                   filtered.mode,
                   filtered.region,
                   filtered.seed_url,
                   filtered.enabled,
                   filtered.review_status,
                   filtered.effective_status,
                   filtered.policy_blocked,
                   filtered.due,
                   filtered.last_succeeded_at,
                   filtered.next_due_at,
                   filtered.event_count,
                   filtered.latest_run_key,
                   filtered.latest_run_status,
                   filtered.latest_run_started_at,
                   filtered.latest_run_completed_at,
                   filtered.latest_run_candidate_count,
                   filtered.latest_run_canonical_count,
                   filtered.latest_run_error,
                   filtered.latest_run_attempt_count,
                   count(*) OVER () AS total_count
            FROM filtered
            ORDER BY filtered.source_key
            LIMIT p_limit
            OFFSET p_offset;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_count_ingestion_admin_sources(
            p_query text,
            p_state text,
            p_source_key text,
            p_include_fixtures boolean
        )
        RETURNS bigint
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT count(*)
            FROM public.fn_ingestion_admin_sources_base(p_include_fixtures) AS source
            WHERE (
                    p_query IS NULL
                    OR strpos(
                        lower(
                            source.source_key || ' ' || source.display_name || ' '
                            || source.publisher || ' ' || source.region
                        ),
                        lower(p_query)
                    ) > 0
                )
              AND (p_source_key IS NULL OR source.source_key = p_source_key)
              AND (
                  p_state = 'all'
                  OR (
                      p_state = 'active'
                      AND source.effective_status IN ('active', 'due', 'running')
                  )
                  OR (p_state = 'due' AND source.due)
                  OR (
                      p_state = 'blocked'
                      AND source.effective_status IN (
                          'disabled', 'unreviewed', 'review_expired', 'policy_blocked'
                      )
                  )
                  OR (p_state = 'failed' AND source.latest_run_status = 'failed')
              )
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_get_ingestion_admin_overview()
        RETURNS TABLE (
            generated_at timestamptz,
            policy_allowed boolean,
            policy_reason text,
            policy_code text,
            sources bigint,
            active_sources bigint,
            due_sources bigint,
            running_runs bigint,
            failed_runs_24h bigint,
            catalog_events bigint,
            pending_commands bigint,
            fixture_sources bigint,
            latest_success_at timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            WITH observed AS (
                SELECT pg_catalog.statement_timestamp() AS generated_at
            ), policy AS (
                SELECT CASE
                           WHEN policy_row.source IS NULL THEN false
                           WHEN policy_row.quarantined THEN false
                           ELSE COALESCE(
                               (policy_row.automation_allowed ->> 'browser')::boolean,
                               false
                           )
                       END AS allowed,
                       CASE
                           WHEN policy_row.source IS NULL
                               THEN 'public catalog policy is missing'
                           WHEN policy_row.quarantined
                               THEN 'public catalog ingestion is quarantined'
                           WHEN NOT COALESCE(
                               (policy_row.automation_allowed ->> 'browser')::boolean,
                               false
                           )
                               THEN 'public catalog browser discovery is disabled'
                           ELSE 'public catalog browser discovery is allowed'
                       END AS reason,
                       CASE
                           WHEN policy_row.source IS NULL THEN 'unknown_source'
                           WHEN policy_row.quarantined THEN 'source_quarantined'
                           WHEN NOT COALESCE(
                               (policy_row.automation_allowed ->> 'browser')::boolean,
                               false
                           ) THEN 'modality_disabled'
                           ELSE 'allowed'
                       END AS code
                FROM (SELECT 1) AS singleton
                LEFT JOIN public.source_policy AS policy_row
                  ON policy_row.source = 'public_jsonld'
            ), source_facts AS MATERIALIZED (
                SELECT source.*,
                       public.fn_ingestion_admin_source_is_fixture(
                           source.source_key, source.publisher, source.seed_url
                       ) AS fixture
                FROM public.catalog_sources AS source
            ), latest_success AS (
                SELECT DISTINCT ON (refresh.source_key)
                       refresh.source_key,
                       refresh.completed_at
                FROM public.catalog_refresh_runs AS refresh
                JOIN source_facts AS source
                  ON source.source_key = refresh.source_key
                 AND NOT source.fixture
                WHERE refresh.status = 'succeeded'
                  AND refresh.completed_at IS NOT NULL
                  AND NOT public.fn_ingestion_admin_run_is_fixture(
                      refresh.run_key, refresh.error
                  )
                ORDER BY refresh.source_key, refresh.completed_at DESC, refresh.run_key DESC
            ), source_summary AS (
                SELECT count(*) FILTER (WHERE NOT source.fixture) AS source_count,
                       count(*) FILTER (
                           WHERE NOT source.fixture
                             AND source.enabled
                             AND source.reviewed_at IS NOT NULL
                             AND (
                                 source.review_expires_at IS NULL
                                 OR source.review_expires_at > observed.generated_at
                             )
                             AND policy.allowed
                       ) AS active_count,
                       count(*) FILTER (
                           WHERE NOT source.fixture
                             AND source.enabled
                             AND source.handoff_only
                             AND source.reviewed_at IS NOT NULL
                             AND (
                                 source.review_expires_at IS NULL
                                 OR source.review_expires_at > observed.generated_at
                             )
                             AND COALESCE(
                                 success.completed_at
                                     + source.refresh_interval_minutes * INTERVAL '1 minute',
                                 source.reviewed_at
                             ) <= observed.generated_at
                       ) AS due_count,
                       max(success.completed_at) AS latest_success,
                       count(*) FILTER (WHERE source.fixture) AS fixture_count
                FROM source_facts AS source
                CROSS JOIN observed
                CROSS JOIN policy
                LEFT JOIN latest_success AS success
                  ON success.source_key = source.source_key
            ), run_summary AS (
                SELECT count(*) FILTER (
                           WHERE refresh.status = 'running'
                             AND refresh.lease_expires_at > observed.generated_at
                       ) AS running_count,
                       count(*) FILTER (
                           WHERE (
                                   refresh.status = 'failed'
                                   AND refresh.completed_at
                                       >= observed.generated_at - INTERVAL '24 hours'
                               )
                              OR (
                                   refresh.status = 'running'
                                   AND (
                                       refresh.lease_expires_at IS NULL
                                       OR refresh.lease_expires_at <= observed.generated_at
                                   )
                                   AND COALESCE(refresh.lease_expires_at, refresh.started_at)
                                       >= observed.generated_at - INTERVAL '24 hours'
                               )
                       ) AS failed_count
                FROM public.catalog_refresh_runs AS refresh
                JOIN source_facts AS source
                  ON source.source_key = refresh.source_key
                 AND NOT source.fixture
                CROSS JOIN observed
                WHERE NOT public.fn_ingestion_admin_run_is_fixture(
                    refresh.run_key, refresh.error
                )
            ), catalog AS (
                SELECT count(DISTINCT event.canonical_event_id) AS event_count
                FROM public.canonical_events AS event
                JOIN public.catalog_event_observations AS observation
                  ON observation.canonical_event_id = event.canonical_event_id
                JOIN source_facts AS source
                  ON source.source_key = observation.source_key
                 AND NOT source.fixture
                JOIN public.catalog_refresh_runs AS observation_run
                  ON observation_run.source_key = observation.source_key
                 AND observation_run.run_key = observation.last_run_key
                CROSS JOIN observed
                WHERE event.start_at >= observed.generated_at
                  AND event.event_status <> 'cancelled'
                  AND NOT public.fn_ingestion_admin_run_is_fixture(
                      observation_run.run_key, observation_run.error
                  )
            ), commands AS (
                SELECT count(*) FILTER (
                    WHERE command.status IN ('queued', 'running')
                ) AS pending_count
                FROM public.ingestion_admin_commands AS command
            )
            SELECT observed.generated_at,
                   policy.allowed,
                   policy.reason,
                   policy.code,
                   source_summary.source_count,
                   source_summary.active_count,
                   source_summary.due_count,
                   run_summary.running_count,
                   run_summary.failed_count,
                   catalog.event_count,
                   commands.pending_count,
                   source_summary.fixture_count,
                   source_summary.latest_success
            FROM observed
            CROSS JOIN policy
            CROSS JOIN source_summary
            CROSS JOIN run_summary
            CROSS JOIN catalog
            CROSS JOIN commands
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_due_sources(
            p_now timestamptz,
            p_limit integer
        )
        RETURNS TABLE (
            source_key text,
            display_name text,
            publisher text,
            seed_url text,
            approved_origins text[],
            region text,
            mode text,
            handoff_only boolean,
            enabled boolean,
            reviewed_at timestamptz,
            review_expires_at timestamptz,
            refresh_interval_minutes integer,
            min_interval_ms integer,
            page_limit integer,
            source_revision integer,
            due_at timestamptz,
            last_succeeded_at timestamptz
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_now IS NULL OR p_limit IS NULL OR p_limit < 1 OR p_limit > 500 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin due-source query is invalid';
            END IF;

            RETURN QUERY
            WITH latest_success AS (
                SELECT DISTINCT ON (refresh.source_key)
                       refresh.source_key,
                       refresh.completed_at AS succeeded_at
                FROM public.catalog_refresh_runs AS refresh
                WHERE refresh.status = 'succeeded'
                  AND refresh.completed_at IS NOT NULL
                  AND NOT public.fn_ingestion_admin_run_is_fixture(
                      refresh.run_key, refresh.error
                  )
                ORDER BY refresh.source_key, refresh.completed_at DESC, refresh.run_key DESC
            ), eligible AS (
                SELECT source.*,
                       success.succeeded_at,
                       COALESCE(
                           success.succeeded_at
                               + source.refresh_interval_minutes * INTERVAL '1 minute',
                           source.reviewed_at
                       ) AS source_due_at
                FROM public.catalog_sources AS source
                LEFT JOIN latest_success AS success
                  ON success.source_key = source.source_key
                WHERE source.enabled
                  AND source.handoff_only
                  AND source.reviewed_at IS NOT NULL
                  AND source.reviewed_at <= p_now
                  AND (
                      source.review_expires_at IS NULL
                      OR source.review_expires_at > p_now
                  )
                  AND NOT public.fn_ingestion_admin_source_is_fixture(
                      source.source_key, source.publisher, source.seed_url
                  )
            )
            SELECT eligible.source_key,
                   eligible.display_name,
                   eligible.publisher,
                   eligible.seed_url,
                   eligible.approved_origins,
                   eligible.region,
                   eligible.mode,
                   eligible.handoff_only,
                   eligible.enabled,
                   eligible.reviewed_at,
                   eligible.review_expires_at,
                   eligible.refresh_interval_minutes,
                   eligible.min_interval_ms,
                   eligible.page_limit,
                   eligible.source_revision,
                   eligible.source_due_at,
                   eligible.succeeded_at
            FROM eligible
            WHERE eligible.source_due_at <= p_now
            ORDER BY eligible.source_due_at, eligible.source_key
            LIMIT p_limit;
        END;
        $$
        """
    )


def _create_run_read_capabilities() -> None:
    """Expose bounded run history with normalized errors only."""
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_runs(
            p_status text,
            p_source_key text,
            p_include_fixtures boolean,
            p_limit integer,
            p_offset integer
        )
        RETURNS TABLE (
            source_key text,
            display_name text,
            run_key text,
            status text,
            started_at timestamptz,
            completed_at timestamptz,
            candidate_count integer,
            canonical_count integer,
            error text,
            attempt_count integer,
            total_count bigint
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_include_fixtures IS NULL
               OR p_limit IS NULL OR p_limit < 1 OR p_limit > 100
               OR p_offset IS NULL OR p_offset < 0 OR p_offset > 100000
               OR (
                   p_status IS NOT NULL
                   AND p_status NOT IN ('running', 'paused', 'succeeded', 'failed')
               )
               OR (
                   p_source_key IS NOT NULL
                   AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin run query is invalid';
            END IF;

            RETURN QUERY
            WITH facts AS (
                SELECT refresh.source_key,
                       source.display_name,
                       refresh.run_key,
                       CASE
                           WHEN refresh.status = 'running'
                            AND (
                                refresh.lease_expires_at IS NULL
                                OR refresh.lease_expires_at
                                    <= pg_catalog.statement_timestamp()
                            )
                               THEN 'failed'
                           ELSE refresh.status
                       END AS projected_status,
                       refresh.started_at,
                       CASE
                           WHEN refresh.status = 'running'
                            AND (
                                refresh.lease_expires_at IS NULL
                                OR refresh.lease_expires_at
                                    <= pg_catalog.statement_timestamp()
                            )
                               THEN COALESCE(refresh.lease_expires_at, refresh.started_at)
                           ELSE refresh.completed_at
                       END AS projected_completed_at,
                       refresh.candidate_count,
                       refresh.canonical_count,
                       public.fn_normalize_catalog_refresh_error(
                           CASE
                               WHEN refresh.status = 'running'
                                AND (
                                    refresh.lease_expires_at IS NULL
                                    OR refresh.lease_expires_at
                                        <= pg_catalog.statement_timestamp()
                                )
                                   THEN 'lease expired'
                               ELSE refresh.error
                           END
                       ) AS safe_error,
                       refresh.attempt_count
                FROM public.catalog_refresh_runs AS refresh
                JOIN public.catalog_sources AS source
                  ON source.source_key = refresh.source_key
                WHERE (
                        p_include_fixtures
                        OR NOT public.fn_ingestion_admin_source_is_fixture(
                            source.source_key, source.publisher, source.seed_url
                        )
                    )
                  AND (
                      p_include_fixtures
                      OR NOT public.fn_ingestion_admin_run_is_fixture(
                          refresh.run_key, refresh.error
                      )
                  )
                  AND (p_source_key IS NULL OR refresh.source_key = p_source_key)
            ), filtered AS (
                SELECT facts.*
                FROM facts
                WHERE p_status IS NULL OR facts.projected_status = p_status
            )
            SELECT filtered.source_key,
                   filtered.display_name,
                   filtered.run_key,
                   filtered.projected_status,
                   filtered.started_at,
                   filtered.projected_completed_at,
                   filtered.candidate_count,
                   filtered.canonical_count,
                   filtered.safe_error,
                   filtered.attempt_count,
                   count(*) OVER () AS total_count
            FROM filtered
            ORDER BY filtered.started_at DESC, filtered.source_key, filtered.run_key
            LIMIT p_limit
            OFFSET p_offset;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_count_ingestion_admin_runs(
            p_status text,
            p_source_key text,
            p_include_fixtures boolean
        )
        RETURNS bigint
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT count(*)
            FROM public.catalog_refresh_runs AS refresh
            JOIN public.catalog_sources AS source
              ON source.source_key = refresh.source_key
            WHERE (
                    p_include_fixtures
                    OR NOT public.fn_ingestion_admin_source_is_fixture(
                        source.source_key, source.publisher, source.seed_url
                    )
                )
              AND (
                  p_include_fixtures
                  OR NOT public.fn_ingestion_admin_run_is_fixture(
                      refresh.run_key, refresh.error
                  )
              )
              AND (
                  p_status IS NULL
                  OR CASE
                         WHEN refresh.status = 'running'
                          AND (
                              refresh.lease_expires_at IS NULL
                              OR refresh.lease_expires_at <= pg_catalog.statement_timestamp()
                          )
                             THEN 'failed'
                         ELSE refresh.status
                     END = p_status
              )
              AND (p_source_key IS NULL OR refresh.source_key = p_source_key)
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_get_ingestion_admin_run(
            p_source_key text,
            p_run_key text
        )
        RETURNS TABLE (
            source_key text,
            display_name text,
            run_key text,
            status text,
            started_at timestamptz,
            completed_at timestamptz,
            candidate_count integer,
            canonical_count integer,
            error text,
            attempt_count integer
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_run_key IS NULL
               OR p_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$'
            THEN
                RETURN;
            END IF;
            RETURN QUERY
            SELECT refresh.source_key,
                   source.display_name,
                   refresh.run_key,
                   CASE
                       WHEN refresh.status = 'running'
                        AND (
                            refresh.lease_expires_at IS NULL
                            OR refresh.lease_expires_at <= pg_catalog.statement_timestamp()
                        )
                           THEN 'failed'
                       ELSE refresh.status
                   END,
                   refresh.started_at,
                   CASE
                       WHEN refresh.status = 'running'
                        AND (
                            refresh.lease_expires_at IS NULL
                            OR refresh.lease_expires_at <= pg_catalog.statement_timestamp()
                        )
                           THEN COALESCE(refresh.lease_expires_at, refresh.started_at)
                       ELSE refresh.completed_at
                   END,
                   refresh.candidate_count,
                   refresh.canonical_count,
                   public.fn_normalize_catalog_refresh_error(
                       CASE
                           WHEN refresh.status = 'running'
                            AND (
                                refresh.lease_expires_at IS NULL
                                OR refresh.lease_expires_at
                                    <= pg_catalog.statement_timestamp()
                            )
                               THEN 'lease expired'
                           ELSE refresh.error
                       END
                   ),
                   refresh.attempt_count
            FROM public.catalog_refresh_runs AS refresh
            JOIN public.catalog_sources AS source
              ON source.source_key = refresh.source_key
            WHERE refresh.source_key = p_source_key
              AND refresh.run_key = p_run_key
              AND NOT public.fn_ingestion_admin_source_is_fixture(
                  source.source_key, source.publisher, source.seed_url
              )
              AND NOT public.fn_ingestion_admin_run_is_fixture(
                  refresh.run_key, refresh.error
              );
        END;
        $$
        """
    )


def _create_command_read_capabilities() -> None:
    """Expose commands without actor identity, leases, or unbounded result data."""
    op.execute(
        """
        CREATE FUNCTION public.fn_list_ingestion_admin_commands(p_limit integer)
        RETURNS TABLE (
            command_id uuid,
            action text,
            source_key text,
            status text,
            requested_at timestamptz,
            started_at timestamptz,
            completed_at timestamptz,
            result jsonb,
            error_code text
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100 THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin command limit is invalid';
            END IF;
            RETURN QUERY
            SELECT command.command_id,
                   command.action,
                   command.source_key,
                   command.status,
                   command.requested_at,
                   command.started_at,
                   command.completed_at,
                   command.result,
                   command.error_code
            FROM public.ingestion_admin_commands AS command
            ORDER BY command.requested_at DESC, command.command_id
            LIMIT p_limit;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_get_ingestion_admin_command(p_command_id uuid)
        RETURNS TABLE (
            command_id uuid,
            action text,
            source_key text,
            status text,
            requested_at timestamptz,
            started_at timestamptz,
            completed_at timestamptz,
            result jsonb,
            error_code text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT command.command_id,
                   command.action,
                   command.source_key,
                   command.status,
                   command.requested_at,
                   command.started_at,
                   command.completed_at,
                   command.result,
                   command.error_code
            FROM public.ingestion_admin_commands AS command
            WHERE p_command_id IS NOT NULL
              AND command.command_id = p_command_id
        $$
        """
    )


def _create_command_write_capabilities() -> None:
    """Create exact-replay enqueue and database-clock lease transitions."""
    op.execute(
        """
        CREATE FUNCTION public.fn_enqueue_ingestion_admin_command(
            p_command_id uuid,
            p_action text,
            p_source_key text,
            p_requested_by text
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            existing_action text;
            existing_source_key text;
            existing_requested_by text;
            source_present boolean;
            source_available boolean;
            policy_allowed boolean;
        BEGIN
            IF p_command_id IS NULL
               OR p_action NOT IN ('refresh_source', 'refresh_due')
               OR (
                   p_action = 'refresh_source'
                   AND (
                       p_source_key IS NULL
                       OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
                   )
               )
               OR (p_action = 'refresh_due' AND p_source_key IS NOT NULL)
               OR p_requested_by IS NULL
               OR char_length(p_requested_by) NOT BETWEEN 1 AND 256
               OR p_requested_by ~ '[[:cntrl:]]'
            THEN
                RETURN 'invalid';
            END IF;

            SELECT command.action, command.source_key, command.requested_by
            INTO existing_action, existing_source_key, existing_requested_by
            FROM public.ingestion_admin_commands AS command
            WHERE command.command_id = p_command_id;
            IF FOUND THEN
                IF existing_action = p_action
                   AND existing_source_key IS NOT DISTINCT FROM p_source_key
                   AND existing_requested_by = p_requested_by
                THEN
                    RETURN 'replayed';
                END IF;
                RETURN 'conflict';
            END IF;

            IF p_action = 'refresh_source' THEN
                SELECT true,
                       source.enabled
                       AND source.handoff_only
                       AND NOT public.fn_ingestion_admin_source_is_fixture(
                           source.source_key, source.publisher, source.seed_url
                       )
                       AND source.reviewed_at IS NOT NULL
                       AND source.reviewed_at <= clock_timestamp()
                       AND (
                           source.review_expires_at IS NULL
                           OR source.review_expires_at > clock_timestamp()
                       )
                INTO source_present, source_available
                FROM public.catalog_sources AS source
                WHERE source.source_key = p_source_key;
                IF NOT COALESCE(source_present, false) THEN
                    RETURN 'not_found';
                END IF;
                IF NOT COALESCE(source_available, false) THEN
                    RETURN 'unavailable';
                END IF;
            END IF;

            SELECT policy.source IS NOT NULL
                   AND NOT policy.quarantined
                   AND COALESCE(
                       (policy.automation_allowed ->> 'browser')::boolean,
                       false
                   )
            INTO policy_allowed
            FROM (SELECT 1) AS singleton
            LEFT JOIN public.source_policy AS policy
              ON policy.source = 'public_jsonld';
            IF NOT COALESCE(policy_allowed, false) THEN
                RETURN 'policy_blocked';
            END IF;

            INSERT INTO public.ingestion_admin_commands (
                command_id, action, source_key, requested_by
            )
            VALUES (p_command_id, p_action, p_source_key, p_requested_by)
            ON CONFLICT (command_id) DO NOTHING;
            IF FOUND THEN
                RETURN 'enqueued';
            END IF;

            SELECT command.action, command.source_key, command.requested_by
            INTO existing_action, existing_source_key, existing_requested_by
            FROM public.ingestion_admin_commands AS command
            WHERE command.command_id = p_command_id;
            IF existing_action = p_action
               AND existing_source_key IS NOT DISTINCT FROM p_source_key
               AND existing_requested_by = p_requested_by
            THEN
                RETURN 'replayed';
            END IF;
            RETURN 'conflict';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_claim_ingestion_admin_commands(
            p_limit integer,
            p_lease_seconds integer
        )
        RETURNS TABLE (
            command_id uuid,
            action text,
            source_key text,
            attempt_count integer,
            lease_token uuid
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit < 1 OR p_limit > 100
               OR p_lease_seconds IS NULL
               OR p_lease_seconds < 300 OR p_lease_seconds > 21600
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'ingestion admin command claim input is invalid';
            END IF;

            RETURN QUERY
            WITH candidates AS (
                SELECT command.command_id
                FROM public.ingestion_admin_commands AS command
                WHERE command.status = 'queued'
                   OR (
                       command.status = 'running'
                       AND command.lease_expires_at <= clock_timestamp()
                   )
                ORDER BY command.requested_at, command.command_id
                FOR UPDATE SKIP LOCKED
                LIMIT p_limit
            )
            UPDATE public.ingestion_admin_commands AS command
            SET status = 'running',
                started_at = COALESCE(command.started_at, clock_timestamp()),
                attempt_count = command.attempt_count + 1,
                lease_token = gen_random_uuid(),
                lease_expires_at =
                    clock_timestamp() + p_lease_seconds * INTERVAL '1 second',
                result = NULL,
                error_code = NULL
            FROM candidates
            WHERE command.command_id = candidates.command_id
            RETURNING command.command_id,
                      command.action,
                      command.source_key,
                      command.attempt_count,
                      command.lease_token;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_complete_ingestion_admin_command(
            p_command_id uuid,
            p_lease_token uuid,
            p_result jsonb
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            command_action text;
            command_source_key text;
        BEGIN
            IF p_command_id IS NULL
               OR p_lease_token IS NULL
               OR NOT public.fn_ingestion_admin_result_is_valid(p_result)
            THEN
                RETURN false;
            END IF;

            SELECT command.action, command.source_key
            INTO command_action, command_source_key
            FROM public.ingestion_admin_commands AS command
            WHERE command.command_id = p_command_id
              AND command.status = 'running'
              AND command.lease_token = p_lease_token
              AND command.lease_expires_at > clock_timestamp()
            FOR UPDATE;
            IF NOT FOUND THEN
                RETURN false;
            END IF;
            IF p_result ->> 'action' <> command_action THEN
                RETURN false;
            END IF;
            IF command_action = 'refresh_source' THEN
                IF jsonb_typeof(p_result -> 'source_key') IS DISTINCT FROM 'string'
                   OR p_result ->> 'source_key' IS DISTINCT FROM command_source_key
                   OR NOT (p_result ? 'run_key')
                   OR jsonb_typeof(p_result -> 'run_key') IS DISTINCT FROM 'string'
                   OR COALESCE(
                       p_result ->> 'run_key'
                           !~ '^[A-Za-z0-9][A-Za-z0-9:._-]{0,255}$',
                       true
                   )
                   OR jsonb_typeof(p_result -> 'outcome') IS DISTINCT FROM 'string'
                   OR COALESCE(p_result ->> 'outcome' NOT IN (
                       'succeeded', 'queued', 'skipped', 'busy', 'already_succeeded',
                       'deferred', 'progressed'
                   ), true)
                THEN
                    RETURN false;
                END IF;
            ELSIF p_result ? 'source_key' OR p_result ? 'run_key' OR p_result ? 'outcome' THEN
                RETURN false;
            END IF;

            UPDATE public.ingestion_admin_commands AS command
            SET status = 'completed',
                completed_at = clock_timestamp(),
                lease_token = NULL,
                lease_expires_at = NULL,
                result = p_result,
                error_code = NULL
            WHERE command.command_id = p_command_id
              AND command.status = 'running'
              AND command.lease_token = p_lease_token
              AND command.lease_expires_at > clock_timestamp();
            RETURN FOUND;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_fail_ingestion_admin_command(
            p_command_id uuid,
            p_lease_token uuid,
            p_error_code text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_command_id IS NULL
               OR p_lease_token IS NULL
               OR p_error_code NOT IN (
                   'source_refresh_failed',
                   'due_refresh_failed',
                   'invalid_result',
                   'worker_unavailable'
               )
            THEN
                RETURN false;
            END IF;
            UPDATE public.ingestion_admin_commands AS command
            SET status = 'failed',
                completed_at = clock_timestamp(),
                lease_token = NULL,
                lease_expires_at = NULL,
                result = NULL,
                error_code = p_error_code
            WHERE command.command_id = p_command_id
              AND command.status = 'running'
              AND command.lease_token = p_lease_token
              AND command.lease_expires_at > clock_timestamp();
            RETURN FOUND;
        END;
        $$
        """
    )


def _grant_capabilities() -> None:
    """Grant ec_app only the fixed read/queue functions required by the admin backend."""
    private_functions = (f"public.fn_ingestion_admin_sources_base{_SOURCE_BASE_SIGNATURE}",)
    for signature in private_functions:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")

    granted_functions = (
        "public.fn_get_ingestion_admin_overview()",
        f"public.fn_list_ingestion_admin_sources{_SOURCE_LIST_SIGNATURE}",
        f"public.fn_count_ingestion_admin_sources{_SOURCE_COUNT_SIGNATURE}",
        f"public.fn_list_ingestion_admin_due_sources{_DUE_SIGNATURE}",
        f"public.fn_list_ingestion_admin_runs{_RUN_LIST_SIGNATURE}",
        f"public.fn_count_ingestion_admin_runs{_RUN_COUNT_SIGNATURE}",
        f"public.fn_get_ingestion_admin_run{_RUN_GET_SIGNATURE}",
        f"public.fn_list_ingestion_admin_commands{_COMMAND_LIST_SIGNATURE}",
        f"public.fn_get_ingestion_admin_command{_COMMAND_GET_SIGNATURE}",
        f"public.fn_enqueue_ingestion_admin_command{_ENQUEUE_SIGNATURE}",
        f"public.fn_claim_ingestion_admin_commands{_CLAIM_SIGNATURE}",
        f"public.fn_complete_ingestion_admin_command{_COMPLETE_SIGNATURE}",
        f"public.fn_fail_ingestion_admin_command{_FAIL_SIGNATURE}",
    )
    for signature in granted_functions:
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
