"""Guard the public watch-poll cursor behind fixed-shape capabilities.

Revision ID: 0065
Revises: 0064
Create Date: 2026-07-18

`watch_poll_state` carries no tenant data, but direct application-role DML can still fabricate a
cursor, steal/extend a lease, suppress a due poll, erase failure evidence, or read a held lease
token. Fixed-shape owner capabilities retain the fixture-only P11 timing contract while limiting
the runtime role to exact public watch lease transitions (FR-8.7a, NFR-7/NFR-8, ADR-008).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0065"
down_revision: str | None = "0064"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace raw public cursor access with bounded claim, result, and health capabilities."""
    _create_claim_capability()
    _create_result_capabilities()
    _create_health_capability()
    _revoke_table_and_grant_capabilities()


def downgrade() -> None:
    """Restore 0062's direct app-role cursor access for an isolated rollback."""
    for signature in (
        "public.fn_list_watch_poll_states()",
        "public.fn_release_watch_poll(uuid, text, text, timestamptz, timestamptz, text)",
        "public.fn_mark_watch_poll_succeeded(uuid, text, text, timestamptz, timestamptz)",
        "public.fn_claim_watch_poll(uuid, text, timestamptz, integer, text)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.watch_poll_state TO ec_app")


def _create_claim_capability() -> None:
    """Create one atomic public cursor initialization and exact due/expired lease claim."""
    op.execute(
        """
        CREATE FUNCTION public.fn_claim_watch_poll(
            p_canonical_event_id uuid,
            p_source text,
            p_now timestamptz,
            p_lease_seconds integer,
            p_lease_token text
        )
        RETURNS TABLE(
            canonical_event_id uuid,
            source text,
            attempt_count integer,
            lease_token text
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_canonical_event_id IS NULL
               OR p_source IS NULL
               OR p_source NOT IN (
                   'public_jsonld', 'meetup', 'ticketmaster', 'luma', 'eventbrite', 'partiful', 'serpapi'
               )
               OR p_now IS NULL
               OR p_lease_seconds IS NULL
               OR p_lease_seconds < 1
               OR p_lease_seconds > 3600
               OR NULLIF(btrim(p_lease_token), '') IS NULL
               OR char_length(p_lease_token) > 128
            THEN
                RETURN;
            END IF;

            -- The central scheduler may only create a cursor for an active public watch. The
            -- subscription check prevents fabrication for an orphan registry key without
            -- exposing whether it was missing, inactive, already leased, or not yet due.
            INSERT INTO public.watch_poll_state
                (canonical_event_id, source, first_seen_at, next_due_at)
            SELECT registry.canonical_event_id, registry.source, registry.created_at, p_now
            FROM public.watch_registry AS registry
            WHERE registry.canonical_event_id = p_canonical_event_id
              AND registry.source = p_source
              AND EXISTS (
                  SELECT 1
                  FROM public.watch_subscriptions AS subscription
                  WHERE subscription.canonical_event_id = registry.canonical_event_id
                    AND subscription.source = registry.source
              )
            ON CONFLICT ON CONSTRAINT watch_poll_state_pkey DO NOTHING;

            RETURN QUERY
            UPDATE public.watch_poll_state AS state
            SET lease_token = p_lease_token,
                lease_expires_at = p_now + (p_lease_seconds * INTERVAL '1 second'),
                last_attempt_at = p_now,
                attempt_count = state.attempt_count + 1
            WHERE state.canonical_event_id = p_canonical_event_id
              AND state.source = p_source
              AND state.next_due_at <= p_now
              AND (
                  state.lease_expires_at IS NULL
                  OR state.lease_expires_at <= p_now
              )
            RETURNING state.canonical_event_id, state.source, state.attempt_count,
                      state.lease_token;
        END;
        $$
        """
    )


def _create_result_capabilities() -> None:
    """Create exact-token success/failure transitions with bounded public error evidence."""
    op.execute(
        """
        CREATE FUNCTION public.fn_mark_watch_poll_succeeded(
            p_canonical_event_id uuid,
            p_source text,
            p_lease_token text,
            p_completed_at timestamptz,
            p_next_due_at timestamptz
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_updated integer;
        BEGIN
            IF p_canonical_event_id IS NULL
               OR p_source IS NULL
               OR p_source NOT IN (
                   'public_jsonld', 'meetup', 'ticketmaster', 'luma', 'eventbrite', 'partiful', 'serpapi'
               )
               OR NULLIF(btrim(p_lease_token), '') IS NULL
               OR char_length(p_lease_token) > 128
               OR p_completed_at IS NULL
               OR p_next_due_at IS NULL
               OR p_next_due_at < p_completed_at
            THEN
                RETURN false;
            END IF;

            UPDATE public.watch_poll_state AS state
            SET last_success_at = p_completed_at,
                consecutive_failures = 0,
                next_due_at = p_next_due_at,
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error_type = NULL
            WHERE state.canonical_event_id = p_canonical_event_id
              AND state.source = p_source
              AND state.lease_token = p_lease_token;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_release_watch_poll(
            p_canonical_event_id uuid,
            p_source text,
            p_lease_token text,
            p_completed_at timestamptz,
            p_next_due_at timestamptz,
            p_error_type text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_error_type text;
            v_updated integer;
        BEGIN
            v_error_type := btrim(p_error_type);
            IF p_canonical_event_id IS NULL
               OR p_source IS NULL
               OR p_source NOT IN (
                   'public_jsonld', 'meetup', 'ticketmaster', 'luma', 'eventbrite', 'partiful', 'serpapi'
               )
               OR NULLIF(btrim(p_lease_token), '') IS NULL
               OR char_length(p_lease_token) > 128
               OR p_completed_at IS NULL
               OR p_next_due_at IS NULL
               OR p_next_due_at < p_completed_at
               OR v_error_type IS NULL
               OR v_error_type !~ '^[A-Za-z][A-Za-z0-9_.]{0,127}$'
            THEN
                RETURN false;
            END IF;

            UPDATE public.watch_poll_state AS state
            SET last_failure_at = p_completed_at,
                consecutive_failures = state.consecutive_failures + 1,
                next_due_at = p_next_due_at,
                lease_token = NULL,
                lease_expires_at = NULL,
                last_error_type = v_error_type
            WHERE state.canonical_event_id = p_canonical_event_id
              AND state.source = p_source
              AND state.lease_token = p_lease_token;
            GET DIAGNOSTICS v_updated = ROW_COUNT;
            RETURN v_updated = 1;
        END;
        $$
        """
    )


def _create_health_capability() -> None:
    """Create the public health projection while withholding live acknowledgement secrets."""
    op.execute(
        """
        CREATE FUNCTION public.fn_list_watch_poll_states()
        RETURNS TABLE(
            canonical_event_id uuid,
            source text,
            first_seen_at timestamptz,
            last_attempt_at timestamptz,
            last_success_at timestamptz,
            last_failure_at timestamptz,
            consecutive_failures integer,
            next_due_at timestamptz,
            attempt_count integer,
            lease_token text,
            lease_expires_at timestamptz,
            last_error_type text
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT state.canonical_event_id,
                   state.source,
                   state.first_seen_at,
                   state.last_attempt_at,
                   state.last_success_at,
                   state.last_failure_at,
                   state.consecutive_failures,
                   state.next_due_at,
                   state.attempt_count,
                   NULL::text AS lease_token,
                   NULL::timestamptz AS lease_expires_at,
                   state.last_error_type
            FROM public.watch_poll_state AS state
            ORDER BY state.canonical_event_id, state.source
        $$
        """
    )


def _revoke_table_and_grant_capabilities() -> None:
    """Leave ec_app no raw cursor read/DML path and only exact bounded operations."""
    op.execute("REVOKE ALL PRIVILEGES ON TABLE public.watch_poll_state FROM PUBLIC")
    op.execute("REVOKE ALL PRIVILEGES ON TABLE public.watch_poll_state FROM ec_app")
    for signature in (
        "public.fn_claim_watch_poll(uuid, text, timestamptz, integer, text)",
        "public.fn_mark_watch_poll_succeeded(uuid, text, text, timestamptz, timestamptz)",
        "public.fn_release_watch_poll(uuid, text, text, timestamptz, timestamptz, text)",
        "public.fn_list_watch_poll_states()",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
