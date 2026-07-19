"""Add tenant-scoped registration consent evidence and guarded audit validation.

Revision ID: 0067
Revises: 0066
Create Date: 2026-07-17

The application role must not manufacture a delegation record merely because it intends to act.
This migration therefore creates an owner-seeded, append-only consent evidence registry and exposes
only fixed tenant-derived resolve/validate capabilities.  Registration audit facts may reference a
consent only after an exact replay check, preserving P6a's ACK-loss convergence for historical
nullable rows (FR-2.9, FR-7.3, NFR-8/10, ADR-003/004/007).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0067"
down_revision: str | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_AUDIT_SIGNATURE = "(text, text, text, text, text, text, text, uuid)"
_RESOLVE_SIGNATURE = "(text, text)"
_VALIDATE_SIGNATURE = "(uuid, text, text)"
_TENANT_POLICY = "tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid"


def upgrade() -> None:
    """Install owner-seeded consent evidence and make new allowed audit facts validate it."""
    _create_consent_table()
    _create_consent_capabilities()
    _replace_audit_append_function(require_current_consent=True)
    _grant_capabilities()


def downgrade() -> None:
    """Restore P6a's prior optional-consent append contract before removing this boundary."""
    _replace_audit_append_function(require_current_consent=False)
    op.execute(
        "DROP FUNCTION IF EXISTS public.fn_validate_registration_consent" + _VALIDATE_SIGNATURE
    )
    op.execute(
        "DROP FUNCTION IF EXISTS public.fn_resolve_registration_consent" + _RESOLVE_SIGNATURE
    )
    op.execute("DROP TABLE IF EXISTS public.tenant_source_consents")


def _create_consent_table() -> None:
    """Create a narrow immutable evidence registry; real capture remains a later trusted flow."""
    op.execute(
        """
        CREATE TABLE public.tenant_source_consents (
            consent_id uuid PRIMARY KEY,
            tenant_id uuid NOT NULL
                REFERENCES public.tenants(tenant_id) ON DELETE CASCADE,
            source text NOT NULL CHECK (source IN ('meetup', 'luma')),
            modality text NOT NULL CHECK (modality IN ('api', 'browser')),
            scope text NOT NULL DEFAULT 'registration'
                CHECK (scope = 'registration'),
            granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
            CONSTRAINT tenant_source_consents_source_modality_valid CHECK (
                (source = 'meetup' AND modality = 'api')
                OR (source = 'luma' AND modality = 'browser')
            ),
            CONSTRAINT tenant_source_consents_one_current_registration_scope UNIQUE (
                tenant_id, source, modality, scope
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_tenant_source_consents_tenant_source_modality_granted
        ON public.tenant_source_consents (tenant_id, source, modality, granted_at DESC)
        """
    )
    op.execute("ALTER TABLE public.tenant_source_consents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.tenant_source_consents FORCE ROW LEVEL SECURITY")
    op.execute(
        f"""
        CREATE POLICY tenant_source_consents_tenant_isolation
        ON public.tenant_source_consents
        USING ({_TENANT_POLICY})
        WITH CHECK ({_TENANT_POLICY})
        """
    )
    # Consent capture is intentionally not an app-role write surface in this increment. Migration
    # owners seed fixture evidence directly; app code receives only the capabilities below.
    op.execute("REVOKE ALL ON TABLE public.tenant_source_consents FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.tenant_source_consents FROM ec_app")


def _create_consent_capabilities() -> None:
    """Create fixed-shape, tenant-derived lookup and validation functions (FR-2.9)."""
    op.execute(
        """
        CREATE FUNCTION public.fn_resolve_registration_consent(
            p_source text,
            p_modality text
        )
        RETURNS uuid
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_tenant_id uuid;
            v_consent_id uuid;
        BEGIN
            v_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_tenant_id IS NULL
               OR p_source IS NULL
               OR p_modality IS NULL
               OR NOT (
                   (p_source = 'meetup' AND p_modality = 'api')
                   OR (p_source = 'luma' AND p_modality = 'browser')
               )
            THEN
                RETURN NULL;
            END IF;

            SELECT consent.consent_id
            INTO v_consent_id
            FROM public.tenant_source_consents AS consent
            WHERE consent.tenant_id = v_tenant_id
              AND consent.source = p_source
              AND consent.modality = p_modality
              AND consent.scope = 'registration'
            ORDER BY consent.granted_at DESC, consent.consent_id DESC
            LIMIT 1;
            RETURN v_consent_id;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_validate_registration_consent(
            p_consent_id uuid,
            p_source text,
            p_modality text
        )
        RETURNS boolean
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_tenant_id uuid;
        BEGIN
            v_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_tenant_id IS NULL
               OR p_consent_id IS NULL
               OR p_source IS NULL
               OR p_modality IS NULL
               OR NOT (
                   (p_source = 'meetup' AND p_modality = 'api')
                   OR (p_source = 'luma' AND p_modality = 'browser')
               )
            THEN
                RETURN false;
            END IF;

            RETURN EXISTS (
                SELECT 1
                FROM public.tenant_source_consents AS consent
                WHERE consent.consent_id = p_consent_id
                  AND consent.tenant_id = v_tenant_id
                  AND consent.source = p_source
                  AND consent.modality = p_modality
                  AND consent.scope = 'registration'
            );
        END;
        $$
        """
    )


def _replace_audit_append_function(*, require_current_consent: bool) -> None:
    """Replace P6a's guard while keeping exact replay ahead of new-consent validation.

    A replay may target an immutable row written before this migration with ``consent_ref = NULL``.
    It must converge before a current-consent lookup; otherwise the migration would turn a lost
    acknowledgement into a failed retry.  Only the absent-row path gains the new requirement.
    """
    consent_validation = (
        """
            IF p_policy_decision = 'allowed' AND p_consent_ref IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'allowed registration action audit requires a consent reference';
            END IF;
            IF p_consent_ref IS NOT NULL
               AND NOT public.fn_validate_registration_consent(
                   p_consent_ref,
                   p_source,
                   p_modality
               )
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'registration action audit consent reference is invalid';
            END IF;
        """
        if require_current_consent
        else ""
    )
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION public.fn_append_registration_action_audit(
            p_audit_key text,
            p_workflow_id text,
            p_source text,
            p_modality text,
            p_phase text,
            p_policy_decision text,
            p_outcome text,
            p_consent_ref uuid
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_context_tenant_id uuid;
            v_workflow_event_id uuid;
            v_existing_tenant_id uuid;
            v_existing_workflow_id text;
            v_existing_source text;
            v_existing_modality text;
            v_existing_phase text;
            v_existing_policy_decision text;
            v_existing_outcome text;
            v_existing_consent_ref uuid;
        BEGIN
            v_context_tenant_id := NULLIF(current_setting('app.tenant_id', true), '')::uuid;
            IF v_context_tenant_id IS NULL THEN
                RAISE EXCEPTION USING
                    ERRCODE = '42501',
                    MESSAGE = 'registration action audit requires a tenant context';
            END IF;
            IF p_audit_key IS NULL
               OR NULLIF(btrim(p_audit_key), '') IS NULL
               OR char_length(p_audit_key) > 512
               OR p_workflow_id IS NULL
               OR NULLIF(btrim(p_workflow_id), '') IS NULL
               OR p_source IS NULL
               OR p_modality IS NULL
               OR p_phase IS NULL
               OR p_policy_decision IS NULL
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'registration action audit identity is incomplete or malformed';
            END IF;
            IF p_source NOT IN ('meetup', 'luma')
               OR p_modality NOT IN ('api', 'browser')
               OR NOT (
                   (p_source = 'meetup' AND p_modality = 'api')
                   OR (p_source = 'luma' AND p_modality = 'browser')
               )
               OR p_policy_decision NOT IN ('allowed', 'denied')
            THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'registration action audit source, modality, or policy decision is invalid';
            END IF;
            IF p_phase IN ('policy_precheck', 'policy_pre_mutate') THEN
                IF p_outcome IS NOT NULL THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'policy audit phases cannot carry a source outcome';
                END IF;
            ELSIF p_phase = 'source_rsvp_outcome' THEN
                IF p_policy_decision IS DISTINCT FROM 'allowed'
                   OR p_outcome IS NULL
                   OR p_outcome NOT IN (
                       'confirmed', 'pending_confirmation', 'needs_handoff',
                       'paywall', 'needs_reauth', 'failed'
                   )
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'source-RSVP audit outcome is invalid';
                END IF;
            ELSE
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'registration action audit phase is invalid';
            END IF;
            BEGIN
                v_workflow_event_id := split_part(p_workflow_id, chr(58), 2)::uuid;
            EXCEPTION
                WHEN invalid_text_representation THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '22023',
                        MESSAGE = 'registration action audit workflow id is invalid';
            END;
            IF p_workflow_id IS DISTINCT FROM (
                v_context_tenant_id::text || chr(58) || v_workflow_event_id::text
            ) THEN
                RAISE EXCEPTION USING
                    ERRCODE = '22023',
                    MESSAGE = 'registration action audit workflow id must be deterministic';
            END IF;

            -- Serialize an absent key too. Exact historical replay returns before validating a
            -- currently usable consent, so the P6a ACK-loss invariant survives this hardening.
            PERFORM pg_advisory_xact_lock(hashtext(p_audit_key)::bigint);
            SELECT audit.tenant_id, audit.workflow_id, audit.source, audit.modality, audit.phase,
                   audit.policy_decision, audit.outcome, audit.consent_ref
            INTO v_existing_tenant_id, v_existing_workflow_id, v_existing_source,
                 v_existing_modality, v_existing_phase, v_existing_policy_decision,
                 v_existing_outcome, v_existing_consent_ref
            FROM public.registration_action_audit AS audit
            WHERE audit.audit_key = p_audit_key;
            IF FOUND THEN
                IF v_existing_tenant_id IS DISTINCT FROM v_context_tenant_id
                   OR v_existing_workflow_id IS DISTINCT FROM p_workflow_id
                   OR v_existing_source IS DISTINCT FROM p_source
                   OR v_existing_modality IS DISTINCT FROM p_modality
                   OR v_existing_phase IS DISTINCT FROM p_phase
                   OR v_existing_policy_decision IS DISTINCT FROM p_policy_decision
                   OR v_existing_outcome IS DISTINCT FROM p_outcome
                   OR v_existing_consent_ref IS DISTINCT FROM p_consent_ref
                THEN
                    RAISE EXCEPTION USING
                        ERRCODE = '23505',
                        MESSAGE = 'registration action audit key is already bound to another fact';
                END IF;
                RETURN false;
            END IF;

{consent_validation}
            INSERT INTO public.registration_action_audit (
                audit_key, tenant_id, workflow_id, source, modality, phase, policy_decision,
                outcome, consent_ref
            ) VALUES (
                p_audit_key, v_context_tenant_id, p_workflow_id, p_source, p_modality, p_phase,
                p_policy_decision, p_outcome, p_consent_ref
            );
            RETURN true;
        END;
        $$
        """
    )


def _grant_capabilities() -> None:
    """Expose only tenant-derived consent lookups plus the existing guarded audit append."""
    for signature in (
        "public.fn_resolve_registration_consent" + _RESOLVE_SIGNATURE,
        "public.fn_validate_registration_consent" + _VALIDATE_SIGNATURE,
        "public.fn_append_registration_action_audit" + _AUDIT_SIGNATURE,
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
