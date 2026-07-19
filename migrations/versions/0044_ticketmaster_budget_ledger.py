"""Create the guarded Ticketmaster pre-dispatch daily budget ledger (ADR-002).

Revision ID: 0044
Revises: 0043
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUTHORIZE_SIGNATURE = "(text, text, text, text, integer)"
_OUTCOME_SIGNATURE = "(text, text, text)"


def upgrade() -> None:
    """Install an irreversible, tenant-neutral daily authorization choke point."""
    op.execute(
        """
        CREATE TABLE public.provider_budget_scope (
            source        text PRIMARY KEY CHECK (source = 'ticketmaster'),
            quota_scope   text NOT NULL UNIQUE CHECK (char_length(quota_scope) BETWEEN 1 AND 128),
            configured_at timestamptz NOT NULL DEFAULT clock_timestamp()
        )
        """
    )
    # A deployment changes this owner-controlled row together with its non-secret app-key label;
    # ec_app never gets DML access. Without this registry a buggy caller could invent a second
    # scope and silently turn one shared Ticketmaster key into multiple 5,000-call ledgers.
    op.execute(
        """INSERT INTO public.provider_budget_scope (source, quota_scope)
           VALUES ('ticketmaster', 'ticketmaster-app')"""
    )
    op.execute(
        """
        CREATE TABLE public.provider_budget_daily (
            source        text NOT NULL CHECK (source = 'ticketmaster'),
            quota_scope   text NOT NULL CHECK (char_length(quota_scope) BETWEEN 1 AND 128),
            budget_day    date NOT NULL,
            crawl_used    integer NOT NULL DEFAULT 0 CHECK (crawl_used BETWEEN 0 AND 4500),
            reserve_used  integer NOT NULL DEFAULT 0 CHECK (reserve_used BETWEEN 0 AND 500),
            CHECK (crawl_used + reserve_used <= 5000),
            PRIMARY KEY (source, quota_scope, budget_day)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.provider_budget_ledger (
            source               text NOT NULL CHECK (source = 'ticketmaster'),
            quota_scope          text NOT NULL CHECK (char_length(quota_scope) BETWEEN 1 AND 128),
            budget_day           date NOT NULL,
            dispatch_key         text NOT NULL CHECK (char_length(dispatch_key) BETWEEN 1 AND 256),
            request_fingerprint  text NOT NULL
                                 CHECK (request_fingerprint ~ '^[0-9a-f]{64}$'),
            budget_class         text NOT NULL CHECK (budget_class IN ('crawl', 'reserve')),
            cost                 integer NOT NULL CHECK (cost BETWEEN 1 AND 5000),
            authorized_at        timestamptz NOT NULL DEFAULT clock_timestamp(),
            outcome              text CHECK (outcome IN ('succeeded', 'failed')),
            completed_at         timestamptz,
            CHECK (
                (outcome IS NULL AND completed_at IS NULL)
                OR (outcome IS NOT NULL AND completed_at IS NOT NULL)
            ),
            PRIMARY KEY (source, quota_scope, dispatch_key),
            FOREIGN KEY (source, quota_scope, budget_day)
                REFERENCES public.provider_budget_daily (source, quota_scope, budget_day)
                ON DELETE RESTRICT
        )
        """
    )
    op.execute(
        """CREATE INDEX ix_provider_budget_ledger_day
               ON public.provider_budget_ledger (source, quota_scope, budget_day, budget_class)"""
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_authorize_ticketmaster_dispatch(
            p_quota_scope text,
            p_dispatch_key text,
            p_request_fingerprint text,
            p_budget_class text,
            p_cost integer
        )
        RETURNS TABLE (
            authorization_status text,
            budget_day date,
            dispatch_key text,
            budget_class text,
            cost integer
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_budget_day date;
            v_existing_fingerprint text;
            v_existing_budget_day date;
            v_existing_budget_class text;
            v_existing_cost integer;
            v_configured_scope text;
        BEGIN
            IF p_quota_scope IS NULL OR char_length(p_quota_scope) NOT BETWEEN 1 AND 128
               OR p_dispatch_key IS NULL OR char_length(p_dispatch_key) NOT BETWEEN 1 AND 256
               OR p_request_fingerprint IS NULL
               OR p_request_fingerprint !~ '^[0-9a-f]{64}$'
               OR p_budget_class NOT IN ('crawl', 'reserve')
               OR p_cost NOT BETWEEN 1 AND 5000
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'invalid Ticketmaster dispatch authorization input';
            END IF;

            SELECT configured_scope.quota_scope
            INTO v_configured_scope
            FROM public.provider_budget_scope AS configured_scope
            WHERE configured_scope.source = 'ticketmaster'
            FOR SHARE;
            IF NOT FOUND OR v_configured_scope IS DISTINCT FROM p_quota_scope THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'Ticketmaster dispatch scope is not configured for this deployment';
            END IF;

            -- A collision merely serializes unrelated attempts; it cannot create an extra permit.
            PERFORM pg_advisory_xact_lock(hashtext(p_quota_scope), hashtext(p_dispatch_key));

            SELECT ledger.request_fingerprint, ledger.budget_day, ledger.budget_class, ledger.cost
            INTO v_existing_fingerprint, v_existing_budget_day, v_existing_budget_class,
                 v_existing_cost
            FROM public.provider_budget_ledger AS ledger
            WHERE ledger.source = 'ticketmaster'
              AND ledger.quota_scope = p_quota_scope
              AND ledger.dispatch_key = p_dispatch_key;
            IF FOUND THEN
                IF v_existing_fingerprint IS DISTINCT FROM p_request_fingerprint
                   OR v_existing_budget_class IS DISTINCT FROM p_budget_class
                   OR v_existing_cost IS DISTINCT FROM p_cost
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'Ticketmaster dispatch key is already bound to another attempt';
                END IF;
                RETURN QUERY
                    SELECT 'already_authorized', v_existing_budget_day, p_dispatch_key,
                           v_existing_budget_class, v_existing_cost;
                RETURN;
            END IF;

            -- The database's clock is authoritative.  A future client must not begin a physical
            -- request with a stale permit around midnight UTC; this function never accepts a caller
            -- supplied timestamp that could move a charge between daily partitions.
            v_budget_day := (clock_timestamp() AT TIME ZONE 'UTC')::date;
            INSERT INTO public.provider_budget_daily (source, quota_scope, budget_day)
            VALUES ('ticketmaster', p_quota_scope, v_budget_day)
            ON CONFLICT ON CONSTRAINT provider_budget_daily_pkey DO NOTHING;

            IF p_budget_class = 'crawl' THEN
                UPDATE public.provider_budget_daily AS daily
                SET crawl_used = daily.crawl_used + p_cost
                WHERE daily.source = 'ticketmaster'
                  AND daily.quota_scope = p_quota_scope
                  AND daily.budget_day = v_budget_day
                  AND daily.crawl_used + daily.reserve_used <= 5000 - p_cost
                  AND daily.crawl_used <= 4500 - p_cost;
            ELSE
                UPDATE public.provider_budget_daily AS daily
                SET reserve_used = daily.reserve_used + p_cost
                WHERE daily.source = 'ticketmaster'
                  AND daily.quota_scope = p_quota_scope
                  AND daily.budget_day = v_budget_day
                  AND daily.crawl_used + daily.reserve_used <= 5000 - p_cost
                  AND daily.reserve_used <= 500 - p_cost;
            END IF;
            IF NOT FOUND THEN
                RETURN QUERY
                    SELECT 'exhausted', v_budget_day, p_dispatch_key, p_budget_class, p_cost;
                RETURN;
            END IF;

            INSERT INTO public.provider_budget_ledger
                (source, quota_scope, budget_day, dispatch_key, request_fingerprint, budget_class, cost)
            VALUES
                ('ticketmaster', p_quota_scope, v_budget_day, p_dispatch_key,
                 p_request_fingerprint, p_budget_class, p_cost);
            RETURN QUERY
                SELECT 'granted', v_budget_day, p_dispatch_key, p_budget_class, p_cost;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_record_ticketmaster_dispatch_outcome(
            p_quota_scope text,
            p_dispatch_key text,
            p_outcome text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_existing_outcome text;
        BEGIN
            IF p_quota_scope IS NULL OR char_length(p_quota_scope) NOT BETWEEN 1 AND 128
               OR p_dispatch_key IS NULL OR char_length(p_dispatch_key) NOT BETWEEN 1 AND 256
               OR p_outcome NOT IN ('succeeded', 'failed')
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'invalid Ticketmaster dispatch outcome input';
            END IF;

            PERFORM pg_advisory_xact_lock(hashtext(p_quota_scope), hashtext(p_dispatch_key));
            SELECT ledger.outcome
            INTO v_existing_outcome
            FROM public.provider_budget_ledger AS ledger
            WHERE ledger.source = 'ticketmaster'
              AND ledger.quota_scope = p_quota_scope
              AND ledger.dispatch_key = p_dispatch_key;
            IF NOT FOUND THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'Ticketmaster dispatch authorization does not exist';
            END IF;
            IF v_existing_outcome IS NOT NULL THEN
                IF v_existing_outcome = p_outcome THEN
                    RETURN false;
                END IF;
                RAISE EXCEPTION USING
                    ERRCODE = '23505',
                    MESSAGE = 'Ticketmaster dispatch outcome is already terminal';
            END IF;

            UPDATE public.provider_budget_ledger
            SET outcome = p_outcome,
                completed_at = clock_timestamp()
            WHERE source = 'ticketmaster'
              AND quota_scope = p_quota_scope
              AND dispatch_key = p_dispatch_key;
            RETURN true;
        END;
        $$
        """
    )
    # This ledger is global app-budget control-plane state, not tenant data.  RLS would hide one
    # tenant's decrement from another and defeat the arithmetic cap.  Direct DML is nevertheless
    # forbidden to ``ec_app``: only the two SECURITY DEFINER functions can mutate it (ADR-002).
    op.execute("REVOKE ALL ON TABLE public.provider_budget_scope FROM ec_app")
    op.execute("REVOKE ALL ON TABLE public.provider_budget_daily FROM ec_app")
    op.execute("REVOKE ALL ON TABLE public.provider_budget_ledger FROM ec_app")
    op.execute("GRANT SELECT ON TABLE public.provider_budget_daily TO ec_app")
    op.execute("GRANT SELECT ON TABLE public.provider_budget_ledger TO ec_app")
    op.execute(
        f"REVOKE ALL ON FUNCTION public.fn_authorize_ticketmaster_dispatch{_AUTHORIZE_SIGNATURE} FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.fn_authorize_ticketmaster_dispatch{_AUTHORIZE_SIGNATURE} TO ec_app"
    )
    op.execute(
        f"REVOKE ALL ON FUNCTION public.fn_record_ticketmaster_dispatch_outcome{_OUTCOME_SIGNATURE} FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.fn_record_ticketmaster_dispatch_outcome{_OUTCOME_SIGNATURE} TO ec_app"
    )


def downgrade() -> None:
    """Drop the guarded control-plane tables; no external dispatch state is stored here."""
    op.execute(
        f"DROP FUNCTION IF EXISTS public.fn_record_ticketmaster_dispatch_outcome{_OUTCOME_SIGNATURE}"
    )
    op.execute(
        f"DROP FUNCTION IF EXISTS public.fn_authorize_ticketmaster_dispatch{_AUTHORIZE_SIGNATURE}"
    )
    op.execute("DROP TABLE IF EXISTS public.provider_budget_ledger")
    op.execute("DROP TABLE IF EXISTS public.provider_budget_daily")
    op.execute("DROP TABLE IF EXISTS public.provider_budget_scope")
