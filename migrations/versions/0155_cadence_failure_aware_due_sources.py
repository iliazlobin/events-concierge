"""Stop a permanently failing source from owning the cadence slot forever.

Revision ID: 0155
Revises: 0154
Create Date: 2026-08-26

``fn_list_ingestion_admin_due_sources`` derives ``due_at`` from a source's last *successful* run.
Failures are invisible to it, and it orders ``due_at`` ascending, so a source that can never
succeed stays due at a timestamp that recedes further into the past on every probe -- it therefore
sorts first, forever, and the fleet has exactly one active ``refresh_due`` command. Two paged
BiblioCommons sources reached 5,290 and 2,226 attempts (roughly ninety cumulative wall-clock hours)
publishing nothing while the healthy remainder of the fleet waited behind them.

This adds a failure-aware wrapper. Eligibility, retirement and the stable ``due_at`` slot stay
owned by ``_v2``: keeping ``due_at`` untouched matters, because ``CatalogRefreshDue.run_key`` is
derived from it and every retry of one slot is deliberately the same durable run. Only *dispatch*
is gated, along two axes:

* A configuration defect -- ``page_cap_exceeded``, ``source_access_denied``, ``policy_blocked`` --
  cannot be resolved by waiting, so cadence stops offering that source immediately.
* Anything else backs off exponentially from its last completed failure and is withdrawn from
  cadence entirely once one slot has burned ``_ATTEMPT_BREAK`` attempts.

Both gates read only ``cadence:`` run keys. An operator ``refresh_source`` command mints its own
``admin:`` key and never reads this projection, so a parked source stays manually dispatchable and
a failed manual retry cannot reset the cadence backoff. Editing the source configuration re-opens
the gate, so raising a page cap resumes cadence on its own rather than silently requiring a second
manual step.

``fn_claim_catalog_refresh`` re-claims one slot in place, clearing ``error``/``completed_at`` and
setting ``status = 'running'`` while incrementing ``attempt_count``. The normalized error code is
therefore only observable between a failure and the next claim, so the gate opens for the few
minutes a doomed slot is actually executing. That window is harmless: the dispatcher cannot start a
second run against a live lease, so the worst case is one no-op fleet pass rather than the
multi-minute doomed page walk this migration exists to stop.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0155"
down_revision: str | None = "0154"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DUE = "(timestamp with time zone,integer)"

# Withdraw a slot from cadence once it has burned this many attempts without a success. The two
# observed runaway slots were four and five orders of magnitude past any useful retry.
_ATTEMPT_BREAK = 20

# Exponential backoff, doubling from two minutes, so the first few retries still land inside the
# ordinary cadence tick and only a persistently failing slot is actually delayed. The shift runs
# past the ceiling on purpose: a long-broken source settles at one retry a day rather than never.
_BACKOFF_BASE_MINUTES = 1
_BACKOFF_MAX_SHIFT = 12
_BACKOFF_CEILING_HOURS = 24

# Error codes from fn_normalize_catalog_refresh_error that no amount of waiting can clear, because
# only an operator editing catalog_sources (or an adapter change) can make the next attempt differ.
#
# `policy_blocked` is deliberately absent. Predicate order in fn_normalize_catalog_refresh_error
# sends the genuinely terminal quarantine case to `source_access_denied`, so what reaches
# `policy_blocked` is the kill switch and a fail-closed policy-store outage -- transient fleet-wide
# states that clear on their own. Parking every source on one of those would turn a brief outage
# into a manual restart of the whole roster.
_TERMINAL_ERROR_CODES = (
    "page_cap_exceeded",
    "source_access_denied",
)

# Only the cadence slot's own history gates cadence.
_CADENCE_RUN_KEY_PREFIX = "cadence:"


def upgrade() -> None:
    """Add the failure-aware due projection without altering any slot identity."""
    _create_due_list_v3()


def downgrade() -> None:
    """Drop the gate; ``_v2`` remains the unfiltered projection it always was."""
    qualified = f"public.fn_list_ingestion_admin_due_sources_v3{_DUE}"
    op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM ec_app")
    op.execute(f"DROP FUNCTION IF EXISTS {qualified}")


def _grant(function: str, signature: str) -> None:
    qualified = f"public.{function}{signature}"
    op.execute(f"REVOKE ALL ON FUNCTION {qualified} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {qualified} TO ec_app")


def _terminal_predicate() -> str:
    """Render the closed terminal-code test as one AND-chain of IS DISTINCT FROM comparisons."""
    return "\n                          AND ".join(
        f"latest.attempt_error_code IS DISTINCT FROM '{code}'" for code in _TERMINAL_ERROR_CODES
    )


def _create_due_list_v3() -> None:
    op.execute(
        f"""
        CREATE FUNCTION public.fn_list_ingestion_admin_due_sources_v3(
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
            WITH candidate AS (
                -- Read the whole eligible fleet rather than p_limit rows: the gate below removes
                -- sources, and filtering after a LIMIT would let one parked slot displace a
                -- healthy source from the pass.
                SELECT due_source.*
                FROM public.fn_list_ingestion_admin_due_sources_v2(p_now, 500) AS due_source
            ), latest AS (
                -- Cadence gates on cadence history only. An operator's manual `admin:` retry
                -- neither parks a slot nor resets an accumulated backoff.
                SELECT DISTINCT ON (refresh.source_key)
                       refresh.source_key AS attempt_source_key,
                       refresh.status AS attempt_status,
                       refresh.started_at AS attempt_started_at,
                       refresh.completed_at AS attempt_completed_at,
                       refresh.attempt_count AS attempt_count,
                       public.fn_normalize_catalog_refresh_error(
                           refresh.error
                       ) AS attempt_error_code
                FROM public.catalog_refresh_runs AS refresh
                JOIN candidate
                  ON candidate.source_key = refresh.source_key
                WHERE refresh.run_key LIKE '{_CADENCE_RUN_KEY_PREFIX}%'
                  AND NOT public.fn_ingestion_admin_run_is_fixture(
                      refresh.run_key, refresh.error
                  )
                ORDER BY refresh.source_key,
                         refresh.started_at DESC,
                         refresh.run_key DESC
            )
            SELECT candidate.source_key,
                   candidate.display_name,
                   candidate.publisher,
                   candidate.seed_url,
                   candidate.approved_origins,
                   candidate.region,
                   candidate.mode,
                   candidate.handoff_only,
                   candidate.enabled,
                   candidate.reviewed_at,
                   candidate.review_expires_at,
                   candidate.refresh_interval_minutes,
                   candidate.min_interval_ms,
                   candidate.page_limit,
                   candidate.source_revision,
                   candidate.due_at,
                   candidate.last_succeeded_at
            FROM candidate
            JOIN public.catalog_sources AS registry
              ON registry.source_key = candidate.source_key
            LEFT JOIN latest
              ON latest.attempt_source_key = candidate.source_key
            WHERE latest.attempt_source_key IS NULL
               OR latest.attempt_status <> 'failed'
               -- An operator edit after the attempt is an explicit "try this again".
               OR registry.updated_at > COALESCE(
                      latest.attempt_completed_at, latest.attempt_started_at
                  )
               OR (
                      {_terminal_predicate()}
                          AND latest.attempt_count < {_ATTEMPT_BREAK}
                          AND COALESCE(
                                  latest.attempt_completed_at, latest.attempt_started_at
                              )
                              + LEAST(
                                    INTERVAL '{_BACKOFF_CEILING_HOURS} hours',
                                    INTERVAL '{_BACKOFF_BASE_MINUTES} minute'
                                        * power(
                                              2::double precision,
                                              LEAST(
                                                  GREATEST(latest.attempt_count, 1),
                                                  {_BACKOFF_MAX_SHIFT}
                                              )::double precision
                                          )
                                ) <= p_now
                  )
            ORDER BY candidate.due_at, candidate.source_key
            LIMIT p_limit;
        END;
        $$
        """
    )
    _grant("fn_list_ingestion_admin_due_sources_v3", _DUE)
