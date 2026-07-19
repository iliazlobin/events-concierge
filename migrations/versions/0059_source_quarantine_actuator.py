"""Install the narrow app-role source-ban circuit-breaker actuator.

Revision ID: 0059
Revises: 0058
Create Date: 2026-07-18

The durable source policy remains owner controlled.  `ec_app` receives one deliberately
one-way capability: a typed ban/forbidden signal may set a known source's `quarantined` bit from
false to true.  It cannot clear the bit, replace the automation map, change paid/signed-agent
controls, create a source row, or alter a kill switch.  Manual owner review through
`fn_set_source_policy` remains the sole release path (FR-10.3, FR-7.6, AC-72, ADR-004).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0059"
down_revision: str | None = "0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "(text, text)"


def upgrade() -> None:
    """Grant `ec_app` only an idempotent false-to-true quarantine transition."""
    op.execute(
        """
        CREATE FUNCTION public.fn_quarantine_source(
            p_source text,
            p_signal text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_source IS NULL
               OR p_source NOT IN (
                   'meetup', 'luma', 'eventbrite', 'ticketmaster', 'serpapi', 'public_jsonld',
                   'partiful'
               )
               OR p_signal IS NULL
               OR p_signal NOT IN ('ban', 'forbidden')
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'invalid source quarantine signal';
            END IF;

            UPDATE public.source_policy
            SET quarantined = true,
                updated_at = clock_timestamp()
            WHERE source = p_source
              AND quarantined IS FALSE;
            IF FOUND THEN
                RETURN true;
            END IF;

            IF NOT EXISTS (
                SELECT 1
                FROM public.source_policy
                WHERE source = p_source
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '55000',
                    MESSAGE = 'source policy control row is missing';
            END IF;

            -- A prior transaction already tripped the circuit breaker.  Do not rewrite its
            -- metadata, so crash/retry observes a converged, exactly-once policy effect.
            RETURN false;
        END;
        $$
        """
    )
    op.execute(f"REVOKE ALL ON FUNCTION public.fn_quarantine_source{_SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION public.fn_quarantine_source{_SIGNATURE} TO ec_app")


def downgrade() -> None:
    """Remove only the app-role one-way quarantine capability."""
    op.execute("DROP FUNCTION IF EXISTS public.fn_quarantine_source(text, text)")
