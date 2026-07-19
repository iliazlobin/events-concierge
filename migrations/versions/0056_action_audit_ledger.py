"""Add the immutable, PII-minimized registration-action audit ledger.

Revision ID: 0056
Revises: 0055
Create Date: 2026-07-17
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SIGNATURE = "(text, text, text, text, text, text, text, uuid)"


def upgrade() -> None:
    """Install the sole guarded write path for P6a policy and RSVP observations.

    Lifecycle state edges remain in transition_ledger. This separate ledger records only opaque,
    closed policy/RVSP facts. The application role can read its tenant's rows but has no table DML;
    it must call the SECURITY DEFINER guard, which validates tenant context and immutable replay
    equivalence. P6a intentionally leaves consent_ref nullable and writes no fabricated consent
    assertion; it does not implement the separately held consent-capture/validation work
    (FR-2.9, FR-7.3, NFR-8/10, ADR-003/007).
    """
    op.execute(
        """
        CREATE TABLE public.registration_action_audit (
            audit_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            audit_key        text NOT NULL UNIQUE
                             CHECK (char_length(audit_key) <= 512 AND btrim(audit_key) <> ''),
            tenant_id        uuid NOT NULL,
            workflow_id      text NOT NULL CHECK (btrim(workflow_id) <> ''),
            source           text NOT NULL CHECK (source IN ('meetup', 'luma')),
            modality         text NOT NULL CHECK (modality IN ('api', 'browser')),
            phase            text NOT NULL CHECK (phase IN (
                                 'policy_precheck', 'policy_pre_mutate', 'source_rsvp_outcome'
                             )),
            policy_decision  text NOT NULL CHECK (policy_decision IN ('allowed', 'denied')),
            outcome          text CHECK (outcome IN (
                                 'confirmed', 'pending_confirmation', 'needs_handoff',
                                 'paywall', 'needs_reauth', 'failed'
                             )),
            consent_ref      uuid,
            created_at       timestamptz NOT NULL DEFAULT clock_timestamp(),
            CHECK (
                (source = 'meetup' AND modality = 'api')
                OR (source = 'luma' AND modality = 'browser')
            ),
            CHECK (
                (phase IN ('policy_precheck', 'policy_pre_mutate') AND outcome IS NULL)
                OR (
                    phase = 'source_rsvp_outcome'
                    AND policy_decision = 'allowed'
                    AND outcome IS NOT NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_registration_action_audit_tenant_workflow_created
        ON public.registration_action_audit (tenant_id, workflow_id, created_at)
        """
    )
    op.execute("ALTER TABLE public.registration_action_audit ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE public.registration_action_audit FORCE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY registration_action_audit_tenant_isolation
        ON public.registration_action_audit
        USING (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        WITH CHECK (tenant_id = NULLIF(current_setting('app.tenant_id', true), '')::uuid)
        """
    )
    # The table is append-only at the application boundary. SELECT supports tenant-scoped evidence
    # inspection; every write goes through the context-checking function below.
    op.execute("REVOKE ALL ON TABLE public.registration_action_audit FROM PUBLIC")
    op.execute("REVOKE ALL ON TABLE public.registration_action_audit FROM ec_app")
    op.execute("GRANT SELECT ON TABLE public.registration_action_audit TO ec_app")
    _create_append_function()
    op.execute(
        f"REVOKE ALL ON FUNCTION public.fn_append_registration_action_audit{_SIGNATURE} FROM PUBLIC"
    )
    op.execute(
        f"GRANT EXECUTE ON FUNCTION public.fn_append_registration_action_audit{_SIGNATURE} TO ec_app"
    )


def downgrade() -> None:
    """Remove P6a's bounded ledger without changing lifecycle-transition evidence."""
    op.execute(f"DROP FUNCTION IF EXISTS public.fn_append_registration_action_audit{_SIGNATURE}")
    op.execute("DROP TABLE IF EXISTS public.registration_action_audit")


def _create_append_function() -> None:
    """Create a serialized exact-replay append guard for immutable tenant-scoped evidence."""
    op.execute(
        """
        CREATE FUNCTION public.fn_append_registration_action_audit(
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

            -- Serialize a key's absent-row path too. An exact retry returns false, while a
            -- materially different use of any tenant's opaque key fails closed.
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
