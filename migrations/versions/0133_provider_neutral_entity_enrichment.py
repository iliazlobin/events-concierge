"""Add a disabled-by-default, evidence-first entity enrichment control plane.

Revision ID: 0133
Revises: 0132
Create Date: 2026-07-31

The schema stores bounded source identity/role facts, revision-fenced jobs, and field-level
observations.  It does not store raw provider payloads, admit name-only matching, or enable any
licensed provider.  Only approved, unexpired observations can be read by the future
materialization capability, and an exact source-extracted profile URL always wins.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0133"
down_revision: str | None = "0132"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_VALIDATE_PROFILE = "public.fn_entity_enrichment_direct_profile_valid(text,text)"
_UPSERT_FACT = (
    "public.fn_upsert_entity_enrichment_source_fact"
    "(text,text,text,text,text,text,text,text,text,text,integer,"
    "timestamp with time zone,timestamp with time zone)"
)
_ENQUEUE = "public.fn_enqueue_entity_enrichment_job(uuid,uuid,text,text)"
_CLAIM = "public.fn_claim_entity_enrichment_jobs(integer,integer)"
_RENEW = "public.fn_renew_entity_enrichment_job_lease(uuid,uuid,integer)"
_RECORD = (
    "public.fn_record_entity_enrichment_observation"
    "(uuid,uuid,uuid,text,text,text,text,text,integer,"
    "timestamp with time zone,timestamp with time zone)"
)
_COMPLETE = "public.fn_complete_entity_enrichment_job(uuid,uuid)"
_RELEASE = "public.fn_release_entity_enrichment_job(uuid,uuid,text,integer)"
_REVIEW = "public.fn_review_entity_enrichment_observation(uuid,text,text)"
_MATERIALIZABLE = "public.fn_list_materializable_entity_enrichment(uuid)"

_CAPABILITIES = (
    _UPSERT_FACT,
    _ENQUEUE,
    _CLAIM,
    _RENEW,
    _RECORD,
    _COMPLETE,
    _RELEASE,
    _REVIEW,
    _MATERIALIZABLE,
)
_APP_CAPABILITIES = tuple(signature for signature in _CAPABILITIES if signature != _REVIEW)


def upgrade() -> None:
    """Install additive storage and guarded capabilities; keep all providers disabled."""
    _create_profile_validator()
    _create_tables()
    _create_source_fact_capability()
    _create_job_capabilities()
    _create_observation_capabilities()
    for signature in (_VALIDATE_PROFILE, *_CAPABILITIES):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
    # Review deliberately has no application-role grant. A separately authenticated operator
    # boundary must be introduced by a later reviewed migration before approval is possible.
    for signature in _APP_CAPABILITIES:
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
    for table in (
        "entity_enrichment_providers",
        "entity_enrichment_source_entities",
        "entity_enrichment_source_facts",
        "entity_enrichment_jobs",
        "entity_enrichment_observations",
    ):
        op.execute(f"REVOKE ALL ON TABLE public.{table} FROM PUBLIC, ec_app")


def downgrade() -> None:
    """Remove only the dormant 0133 control plane."""
    for signature in reversed(_CAPABILITIES):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    for table in (
        "entity_enrichment_observations",
        "entity_enrichment_jobs",
        "entity_enrichment_source_facts",
        "entity_enrichment_source_entities",
        "entity_enrichment_providers",
    ):
        op.execute(f"DROP TABLE IF EXISTS public.{table}")
    op.execute(f"DROP FUNCTION IF EXISTS {_VALIDATE_PROFILE}")


def _create_profile_validator() -> None:
    """Accept only direct HTTPS identity URLs; never search/results or tracking URLs."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_entity_enrichment_direct_profile_valid(
            p_url text,
            p_entity_kind text
        )
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        PARALLEL SAFE
        SET search_path = pg_catalog, public
        AS $$
            SELECT p_url IS NOT NULL
               AND p_entity_kind IN ('person', 'organization')
               AND char_length(p_url) BETWEEN 9 AND 2048
               AND p_url !~ '[[:cntrl:][:space:]]'
               AND p_url !~ '[?#]'
               AND p_url !~* '/(search|results)(/|$)'
               AND p_url ~ '^https://[^/?#@[:space:]]+(/[^?#[:space:]]*)?$'
               AND CASE
                   WHEN p_entity_kind = 'person' THEN
                       p_url ~* (
                           '^https://(www[.])?linkedin[.]com'
                           '(:[0-9]{1,5})?/in/[a-z0-9][a-z0-9_%.-]*/?$'
                       )
                   WHEN p_url ~* (
                       '^https://(www[.])?linkedin[.]com(:[0-9]{1,5})?/'
                   ) THEN
                       p_url ~* (
                           '^https://(www[.])?linkedin[.]com'
                           '(:[0-9]{1,5})?/company/[a-z0-9][a-z0-9_%.-]*/?$'
                       )
                   ELSE true
               END
        $$
        """
    )


def _create_tables() -> None:
    """Create source facts, gated providers, revision-bound jobs, and observations."""
    op.execute(
        """
        CREATE TABLE public.entity_enrichment_providers (
            provider_key          text PRIMARY KEY,
            display_name          text NOT NULL,
            allowed_entity_kinds  text[] NOT NULL,
            enabled               boolean NOT NULL DEFAULT false,
            credentials_configured boolean NOT NULL DEFAULT false,
            terms_approved_at     timestamptz,
            operator_approved_at  timestamptz,
            created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
            CHECK (provider_key ~ '^[a-z][a-z0-9_]{1,79}$'),
            CHECK (
                cardinality(allowed_entity_kinds) BETWEEN 1 AND 2
                AND allowed_entity_kinds <@ ARRAY['person', 'organization']::text[]
                AND array_position(allowed_entity_kinds, NULL) IS NULL
            ),
            CHECK (
                NOT enabled
                OR (
                    credentials_configured
                    AND terms_approved_at IS NOT NULL
                    AND operator_approved_at IS NOT NULL
                )
            )
        )
        """
    )
    # These rows document future adapter identities.  No capability in this migration can enable
    # them, and claim/enqueue both re-check all credential/terms/operator gates.
    op.execute(
        """
        INSERT INTO public.entity_enrichment_providers (
            provider_key, display_name, allowed_entity_kinds, enabled,
            credentials_configured, terms_approved_at, operator_approved_at
        )
        VALUES
            ('linkedin_licensed', 'Licensed LinkedIn provider',
             ARRAY['person', 'organization']::text[], false, false, NULL, NULL),
            ('levels_licensed', 'Licensed Levels-style organization provider',
             ARRAY['organization']::text[], false, false, NULL, NULL)
        """
    )
    op.execute(
        """
        CREATE TABLE public.entity_enrichment_source_entities (
            entity_id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            source_key            text NOT NULL
                                  REFERENCES public.catalog_sources(source_key)
                                  ON DELETE RESTRICT,
            source_entity_key     text NOT NULL,
            identity_basis        text NOT NULL,
            entity_kind           text NOT NULL,
            display_name          text NOT NULL,
            direct_profile_url    text,
            entity_revision       integer NOT NULL DEFAULT 1,
            first_observed_at     timestamptz NOT NULL,
            last_observed_at      timestamptz NOT NULL,
            last_source_run_key   text NOT NULL,
            created_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at            timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (source_key, source_entity_key),
            UNIQUE (entity_id, source_key),
            FOREIGN KEY (source_key, last_source_run_key)
                REFERENCES public.catalog_refresh_runs(source_key, run_key)
                ON DELETE RESTRICT,
            CHECK (char_length(source_entity_key) BETWEEN 1 AND 500),
            CHECK (source_entity_key !~ '[[:cntrl:]]'),
            CHECK (identity_basis IN ('source_entity_id', 'source_profile_url')),
            CHECK (entity_kind IN ('person', 'organization')),
            CHECK (
                char_length(display_name) BETWEEN 1 AND 160
                AND display_name !~ '[[:cntrl:]]'
                AND lower(btrim(source_entity_key)) <> lower(btrim(display_name))
            ),
            CHECK (entity_revision > 0),
            CHECK (last_observed_at >= first_observed_at),
            CHECK (
                direct_profile_url IS NULL
                OR public.fn_entity_enrichment_direct_profile_valid(
                    direct_profile_url, entity_kind
                )
            ),
            CHECK (
                identity_basis <> 'source_profile_url'
                OR (
                    direct_profile_url IS NOT NULL
                    AND source_entity_key = direct_profile_url
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE public.entity_enrichment_source_facts (
            fact_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            entity_id           uuid NOT NULL,
            source_key          text NOT NULL,
            source              text NOT NULL,
            source_event_id     text NOT NULL,
            role                text NOT NULL,
            confidence_bps      integer NOT NULL,
            verification_status text NOT NULL DEFAULT 'source_asserted',
            observed_at         timestamptz NOT NULL,
            expires_at          timestamptz,
            source_run_key      text NOT NULL,
            created_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at          timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (entity_id, source_key, source, source_event_id, role),
            FOREIGN KEY (entity_id, source_key)
                REFERENCES public.entity_enrichment_source_entities(entity_id, source_key)
                ON DELETE RESTRICT,
            FOREIGN KEY (source_key, source, source_event_id)
                REFERENCES public.catalog_event_observations(
                    source_key, source, source_event_id
                )
                ON DELETE RESTRICT,
            FOREIGN KEY (source_key, source_run_key)
                REFERENCES public.catalog_refresh_runs(source_key, run_key)
                ON DELETE RESTRICT,
            CHECK (role IN ('organizer', 'host', 'speaker', 'partner')),
            CHECK (confidence_bps BETWEEN 1 AND 10000),
            CHECK (verification_status IN ('source_asserted', 'reviewed', 'rejected')),
            CHECK (expires_at IS NULL OR expires_at > observed_at)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_entity_enrichment_source_facts_event
        ON public.entity_enrichment_source_facts
            (source_key, source, source_event_id, role)
        """
    )
    op.execute(
        """
        CREATE TABLE public.entity_enrichment_jobs (
            job_id                     uuid PRIMARY KEY,
            entity_id                  uuid NOT NULL
                                       REFERENCES public.entity_enrichment_source_entities(entity_id)
                                       ON DELETE RESTRICT,
            provider_key               text NOT NULL
                                       REFERENCES public.entity_enrichment_providers(provider_key)
                                       ON DELETE RESTRICT,
            requested_entity_revision  integer NOT NULL,
            requested_by               text NOT NULL,
            status                     text NOT NULL DEFAULT 'queued',
            available_at               timestamptz NOT NULL DEFAULT clock_timestamp(),
            attempt_count              integer NOT NULL DEFAULT 0,
            lease_token                uuid,
            lease_expires_at           timestamptz,
            started_at                 timestamptz,
            completed_at               timestamptz,
            error_code                 text,
            created_at                 timestamptz NOT NULL DEFAULT clock_timestamp(),
            updated_at                 timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (job_id, entity_id),
            CHECK (requested_entity_revision > 0),
            CHECK (
                char_length(requested_by) BETWEEN 1 AND 256
                AND requested_by !~ '[[:cntrl:]]'
            ),
            CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'stale')),
            CHECK (attempt_count BETWEEN 0 AND 5),
            CHECK (
                error_code IS NULL
                OR error_code IN (
                    'provider_unavailable', 'rate_limited', 'transient',
                    'invalid_response', 'identity_unresolved', 'policy_blocked',
                    'stale_entity_revision'
                )
            ),
            CHECK (
                (status = 'queued' AND lease_token IS NULL AND lease_expires_at IS NULL
                                   AND completed_at IS NULL)
                OR (status = 'running' AND lease_token IS NOT NULL
                                      AND lease_expires_at IS NOT NULL
                                      AND started_at IS NOT NULL
                                      AND completed_at IS NULL)
                OR (status IN ('succeeded', 'failed', 'stale')
                    AND lease_token IS NULL AND lease_expires_at IS NULL
                    AND completed_at IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ux_entity_enrichment_jobs_active_revision
        ON public.entity_enrichment_jobs
            (entity_id, provider_key, requested_entity_revision)
        WHERE status IN ('queued', 'running')
        """
    )
    op.execute(
        """
        CREATE INDEX ix_entity_enrichment_jobs_due
        ON public.entity_enrichment_jobs (available_at, created_at, job_id)
        WHERE status = 'queued'
        """
    )
    op.execute(
        """
        CREATE TABLE public.entity_enrichment_observations (
            observation_id     uuid PRIMARY KEY,
            job_id             uuid NOT NULL,
            entity_id          uuid NOT NULL,
            attempt_count      integer NOT NULL,
            field_name         text NOT NULL,
            field_value        text NOT NULL,
            provenance_url     text NOT NULL,
            provider_record_id text NOT NULL,
            match_basis        text NOT NULL,
            confidence_bps     integer NOT NULL,
            observed_at        timestamptz NOT NULL,
            expires_at         timestamptz,
            review_status      text NOT NULL DEFAULT 'pending',
            reviewed_by        text,
            reviewed_at        timestamptz,
            created_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (job_id, attempt_count, field_name, provider_record_id),
            FOREIGN KEY (job_id, entity_id)
                REFERENCES public.entity_enrichment_jobs(job_id, entity_id)
                ON DELETE RESTRICT,
            CHECK (field_name IN (
                'profile_url', 'headline', 'job_title', 'organization_name',
                'organization_profile_url', 'seniority', 'function', 'industry',
                'compensation_market'
            )),
            CHECK (
                char_length(field_value) BETWEEN 1 AND 2048
                AND field_value !~ '[[:cntrl:]]'
                AND (
                    field_name IN ('profile_url', 'organization_profile_url')
                    OR char_length(field_value) <= 500
                )
            ),
            CHECK (
                char_length(provenance_url) BETWEEN 9 AND 2048
                AND provenance_url !~ '[[:cntrl:][:space:]]'
                AND provenance_url !~ '#'
                AND provenance_url ~ '^https://[^/?#@[:space:]]+(/|[?]|$)'
            ),
            CHECK (
                char_length(provider_record_id) BETWEEN 1 AND 500
                AND provider_record_id !~ '[[:cntrl:]]'
            ),
            CHECK (match_basis IN (
                'source_entity_id', 'source_profile_url',
                'provider_crosswalk', 'manual_review'
            )),
            CHECK (confidence_bps BETWEEN 1 AND 10000),
            CHECK (attempt_count BETWEEN 1 AND 5),
            CHECK (expires_at IS NULL OR expires_at > observed_at),
            CHECK (review_status IN ('pending', 'approved', 'rejected')),
            CHECK (
                (review_status = 'pending' AND reviewed_by IS NULL AND reviewed_at IS NULL)
                OR (
                    review_status IN ('approved', 'rejected')
                    AND reviewed_by IS NOT NULL
                    AND char_length(reviewed_by) BETWEEN 1 AND 256
                    AND reviewed_by !~ '[[:cntrl:]]'
                    AND reviewed_at IS NOT NULL
                )
            )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_entity_enrichment_observations_review
        ON public.entity_enrichment_observations
            (review_status, entity_id, field_name, reviewed_at DESC)
        """
    )


def _create_source_fact_capability() -> None:
    """Atomically upsert a source entity and its exact event-role assertion."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_upsert_entity_enrichment_source_fact(
            p_source_key text,
            p_source text,
            p_source_event_id text,
            p_source_run_key text,
            p_source_entity_key text,
            p_identity_basis text,
            p_role text,
            p_entity_kind text,
            p_display_name text,
            p_direct_profile_url text,
            p_confidence_bps integer,
            p_observed_at timestamptz,
            p_expires_at timestamptz
        )
        RETURNS TABLE (
            outcome text,
            entity_id uuid,
            fact_id uuid,
            entity_revision integer
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_entity public.entity_enrichment_source_entities%ROWTYPE;
            v_fact public.entity_enrichment_source_facts%ROWTYPE;
            v_changed boolean := false;
            v_outcome text := 'replayed';
        BEGIN
            IF p_source_key IS NULL
               OR p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
               OR p_source IS NULL
               OR p_source !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}$'
               OR p_source_event_id IS NULL
               OR char_length(p_source_event_id) NOT BETWEEN 1 AND 500
               OR btrim(p_source_event_id) = ''
               OR p_source_event_id ~ '[[:cntrl:]]'
               OR p_source_run_key IS NULL
               OR p_source_run_key !~ '^[A-Za-z0-9][A-Za-z0-9:._/-]{0,255}$'
               OR p_source_entity_key IS NULL
               OR char_length(p_source_entity_key) NOT BETWEEN 1 AND 500
               OR btrim(p_source_entity_key) = ''
               OR p_source_entity_key ~ '[[:cntrl:]]'
               OR p_identity_basis IS NULL
               OR p_identity_basis NOT IN ('source_entity_id', 'source_profile_url')
               OR p_role IS NULL
               OR p_role NOT IN ('organizer', 'host', 'speaker', 'partner')
               OR p_entity_kind IS NULL
               OR p_entity_kind NOT IN ('person', 'organization')
               OR p_display_name IS NULL
               OR char_length(p_display_name) NOT BETWEEN 1 AND 160
               OR btrim(p_display_name) = ''
               OR p_display_name ~ '[[:cntrl:]]'
               OR lower(btrim(p_source_entity_key)) = lower(btrim(p_display_name))
               OR p_confidence_bps IS NULL
               OR p_confidence_bps NOT BETWEEN 1 AND 10000
               OR p_observed_at IS NULL
               OR (p_expires_at IS NOT NULL AND p_expires_at <= p_observed_at)
               OR (
                   p_direct_profile_url IS NOT NULL
                   AND NOT public.fn_entity_enrichment_direct_profile_valid(
                       p_direct_profile_url, p_entity_kind
                   )
               )
               OR (
                   p_identity_basis = 'source_profile_url'
                   AND (
                       p_direct_profile_url IS NULL
                       OR p_source_entity_key <> p_direct_profile_url
                   )
               )
            THEN
                RETURN QUERY SELECT 'invalid', NULL::uuid, NULL::uuid, NULL::integer;
                RETURN;
            END IF;

            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_source_key || chr(31) || p_source_entity_key, 0)
            );

            IF NOT EXISTS (
                SELECT 1
                FROM public.catalog_event_observations AS observation
                WHERE observation.source_key = p_source_key
                  AND observation.source = p_source
                  AND observation.source_event_id = p_source_event_id
                  AND observation.last_run_key = p_source_run_key
            ) THEN
                RETURN QUERY SELECT 'not_found', NULL::uuid, NULL::uuid, NULL::integer;
                RETURN;
            END IF;

            SELECT entity.*
            INTO v_entity
            FROM public.entity_enrichment_source_entities AS entity
            WHERE entity.source_key = p_source_key
              AND entity.source_entity_key = p_source_entity_key
            FOR UPDATE;

            IF NOT FOUND THEN
                INSERT INTO public.entity_enrichment_source_entities (
                    source_key, source_entity_key, identity_basis, entity_kind,
                    display_name, direct_profile_url, first_observed_at,
                    last_observed_at, last_source_run_key
                ) VALUES (
                    p_source_key, p_source_entity_key, p_identity_basis, p_entity_kind,
                    p_display_name, p_direct_profile_url, p_observed_at,
                    p_observed_at, p_source_run_key
                )
                RETURNING * INTO v_entity;
                v_outcome := 'inserted';
            ELSE
                IF v_entity.identity_basis <> p_identity_basis
                   OR v_entity.entity_kind <> p_entity_kind
                THEN
                    RETURN QUERY SELECT 'conflict', v_entity.entity_id, NULL::uuid,
                                        v_entity.entity_revision;
                    RETURN;
                END IF;
                v_changed := p_observed_at >= v_entity.last_observed_at
                    AND (
                        v_entity.display_name IS DISTINCT FROM p_display_name
                        OR (
                            p_direct_profile_url IS NOT NULL
                            AND v_entity.direct_profile_url IS DISTINCT FROM p_direct_profile_url
                        )
                    );
                UPDATE public.entity_enrichment_source_entities AS entity
                SET display_name = CASE
                        WHEN p_observed_at >= entity.last_observed_at
                            THEN p_display_name
                        ELSE entity.display_name
                    END,
                    direct_profile_url = CASE
                        WHEN p_observed_at >= entity.last_observed_at
                            THEN COALESCE(p_direct_profile_url, entity.direct_profile_url)
                        ELSE entity.direct_profile_url
                    END,
                    entity_revision = entity.entity_revision + CASE WHEN v_changed THEN 1 ELSE 0 END,
                    last_observed_at = GREATEST(entity.last_observed_at, p_observed_at),
                    last_source_run_key = CASE
                        WHEN p_observed_at >= entity.last_observed_at
                            THEN p_source_run_key
                        ELSE entity.last_source_run_key
                    END,
                    updated_at = clock_timestamp()
                WHERE entity.entity_id = v_entity.entity_id
                RETURNING * INTO v_entity;
                IF v_changed THEN
                    v_outcome := 'updated';
                END IF;
            END IF;

            SELECT fact.*
            INTO v_fact
            FROM public.entity_enrichment_source_facts AS fact
            WHERE fact.entity_id = v_entity.entity_id
              AND fact.source_key = p_source_key
              AND fact.source = p_source
              AND fact.source_event_id = p_source_event_id
              AND fact.role = p_role
            FOR UPDATE;
            IF NOT FOUND THEN
                INSERT INTO public.entity_enrichment_source_facts (
                    entity_id, source_key, source, source_event_id, role,
                    confidence_bps, verification_status, observed_at,
                    expires_at, source_run_key
                ) VALUES (
                    v_entity.entity_id, p_source_key, p_source, p_source_event_id, p_role,
                    p_confidence_bps, 'source_asserted', p_observed_at,
                    p_expires_at, p_source_run_key
                )
                RETURNING * INTO v_fact;
                IF v_outcome = 'replayed' THEN
                    v_outcome := 'inserted';
                END IF;
            ELSE
                IF p_observed_at >= v_fact.observed_at THEN
                    IF v_fact.confidence_bps IS DISTINCT FROM p_confidence_bps
                       OR v_fact.expires_at IS DISTINCT FROM p_expires_at
                       OR v_fact.source_run_key IS DISTINCT FROM p_source_run_key
                       OR v_fact.observed_at IS DISTINCT FROM p_observed_at
                    THEN
                        v_outcome := 'updated';
                    END IF;
                    UPDATE public.entity_enrichment_source_facts AS fact
                    SET confidence_bps = p_confidence_bps,
                        observed_at = p_observed_at,
                        expires_at = p_expires_at,
                        source_run_key = p_source_run_key,
                        updated_at = clock_timestamp()
                    WHERE fact.fact_id = v_fact.fact_id
                    RETURNING * INTO v_fact;
                END IF;
            END IF;

            RETURN QUERY SELECT v_outcome, v_entity.entity_id, v_fact.fact_id,
                                v_entity.entity_revision;
        END;
        $$
        """
    )


def _create_job_capabilities() -> None:
    """Install provider-gated enqueue and exact-token lease transitions."""
    op.execute(
        """
        CREATE FUNCTION public.fn_enqueue_entity_enrichment_job(
            p_job_id uuid,
            p_entity_id uuid,
            p_provider_key text,
            p_requested_by text
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_entity public.entity_enrichment_source_entities%ROWTYPE;
            v_existing public.entity_enrichment_jobs%ROWTYPE;
            v_provider public.entity_enrichment_providers%ROWTYPE;
        BEGIN
            IF p_job_id IS NULL OR p_entity_id IS NULL
               OR p_provider_key IS NULL
               OR p_provider_key !~ '^[a-z][a-z0-9_]{1,79}$'
               OR p_requested_by IS NULL
               OR char_length(p_requested_by) NOT BETWEEN 1 AND 256
               OR btrim(p_requested_by) = ''
               OR p_requested_by ~ '[[:cntrl:]]'
            THEN
                RETURN 'invalid';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(p_job_id::text, 0));
            PERFORM pg_advisory_xact_lock(
                hashtextextended(p_entity_id::text || chr(31) || p_provider_key, 0)
            );
            SELECT job.* INTO v_existing
            FROM public.entity_enrichment_jobs AS job
            WHERE job.job_id = p_job_id;
            IF FOUND THEN
                IF v_existing.entity_id = p_entity_id
                   AND v_existing.provider_key = p_provider_key
                   AND v_existing.requested_by = p_requested_by
                THEN
                    RETURN 'replayed';
                END IF;
                RETURN 'conflict';
            END IF;
            SELECT entity.* INTO v_entity
            FROM public.entity_enrichment_source_entities AS entity
            WHERE entity.entity_id = p_entity_id
            FOR SHARE OF entity;
            IF NOT FOUND THEN
                RETURN 'not_found';
            END IF;
            SELECT provider.* INTO v_provider
            FROM public.entity_enrichment_providers AS provider
            WHERE provider.provider_key = p_provider_key
            FOR SHARE OF provider;
            IF NOT FOUND OR NOT v_provider.enabled
               OR NOT v_provider.credentials_configured
               OR v_provider.terms_approved_at IS NULL
               OR v_provider.operator_approved_at IS NULL
            THEN
                RETURN 'provider_disabled';
            END IF;
            IF v_entity.entity_kind <> ALL(v_provider.allowed_entity_kinds) THEN
                RETURN 'kind_unsupported';
            END IF;

            UPDATE public.entity_enrichment_jobs AS job
            SET status = 'stale', completed_at = clock_timestamp(),
                error_code = 'stale_entity_revision', updated_at = clock_timestamp()
            WHERE job.entity_id = p_entity_id
              AND job.provider_key = p_provider_key
              AND job.status = 'queued'
              AND job.requested_entity_revision <> v_entity.entity_revision;

            IF EXISTS (
                SELECT 1 FROM public.entity_enrichment_jobs AS job
                WHERE job.entity_id = p_entity_id
                  AND job.provider_key = p_provider_key
                  AND job.requested_entity_revision = v_entity.entity_revision
                  AND job.status IN ('queued', 'running')
            ) THEN
                RETURN 'conflict';
            END IF;
            INSERT INTO public.entity_enrichment_jobs (
                job_id, entity_id, provider_key, requested_entity_revision, requested_by
            ) VALUES (
                p_job_id, p_entity_id, p_provider_key,
                v_entity.entity_revision, p_requested_by
            );
            RETURN 'enqueued';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_claim_entity_enrichment_jobs(
            p_limit integer,
            p_lease_seconds integer
        )
        RETURNS TABLE (
            job_id uuid,
            entity_id uuid,
            provider_key text,
            requested_entity_revision integer,
            attempt_count integer,
            lease_token uuid,
            source_key text,
            source_entity_key text,
            identity_basis text,
            entity_kind text,
            display_name text,
            direct_profile_url text
        )
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 100
               OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 30 AND 3600
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'entity enrichment claim input is invalid';
            END IF;
            UPDATE public.entity_enrichment_jobs AS job
            SET status = 'failed', error_code = 'policy_blocked',
                lease_token = NULL, lease_expires_at = NULL,
                completed_at = clock_timestamp(), updated_at = clock_timestamp()
            FROM public.entity_enrichment_source_entities AS entity,
                 public.entity_enrichment_providers AS provider
            WHERE entity.entity_id = job.entity_id
              AND provider.provider_key = job.provider_key
              AND job.status IN ('queued', 'running')
              AND (
                  NOT provider.enabled
                  OR NOT provider.credentials_configured
                  OR provider.terms_approved_at IS NULL
                  OR provider.operator_approved_at IS NULL
                  OR entity.entity_kind <> ALL(provider.allowed_entity_kinds)
              );
            UPDATE public.entity_enrichment_jobs AS job
            SET status = CASE
                    WHEN job.requested_entity_revision <> entity.entity_revision THEN 'stale'
                    ELSE 'failed'
                END,
                error_code = CASE
                    WHEN job.requested_entity_revision <> entity.entity_revision
                        THEN 'stale_entity_revision'
                    ELSE COALESCE(job.error_code, 'provider_unavailable')
                END,
                lease_token = NULL, lease_expires_at = NULL,
                completed_at = clock_timestamp(), updated_at = clock_timestamp()
            FROM public.entity_enrichment_source_entities AS entity
            WHERE entity.entity_id = job.entity_id
              AND (
                  job.requested_entity_revision <> entity.entity_revision
                  OR (
                      job.attempt_count >= 5
                      AND (
                          job.status = 'queued'
                          OR (job.status = 'running' AND job.lease_expires_at <= clock_timestamp())
                      )
                  )
              )
              AND job.status IN ('queued', 'running');

            RETURN QUERY
            WITH due AS (
                SELECT job.job_id
                FROM public.entity_enrichment_jobs AS job
                JOIN public.entity_enrichment_source_entities AS entity
                  ON entity.entity_id = job.entity_id
                 AND entity.entity_revision = job.requested_entity_revision
                JOIN public.entity_enrichment_providers AS provider
                  ON provider.provider_key = job.provider_key
                 AND provider.enabled
                 AND provider.credentials_configured
                 AND provider.terms_approved_at IS NOT NULL
                 AND provider.operator_approved_at IS NOT NULL
                 AND entity.entity_kind = ANY(provider.allowed_entity_kinds)
                WHERE job.attempt_count < 5
                  AND (
                      (job.status = 'queued' AND job.available_at <= clock_timestamp())
                      OR (
                          job.status = 'running'
                          AND job.lease_expires_at <= clock_timestamp()
                      )
                  )
                ORDER BY job.available_at, job.created_at, job.job_id
                FOR UPDATE OF job SKIP LOCKED
                LIMIT p_limit
            ), claimed AS (
                UPDATE public.entity_enrichment_jobs AS job
                SET status = 'running', lease_token = gen_random_uuid(),
                    lease_expires_at = clock_timestamp()
                        + p_lease_seconds * INTERVAL '1 second',
                    attempt_count = job.attempt_count + 1,
                    started_at = COALESCE(job.started_at, clock_timestamp()),
                    completed_at = NULL, error_code = NULL,
                    updated_at = clock_timestamp()
                FROM due
                WHERE job.job_id = due.job_id
                RETURNING job.*
            )
            SELECT claimed.job_id, claimed.entity_id, claimed.provider_key,
                   claimed.requested_entity_revision, claimed.attempt_count,
                   claimed.lease_token, entity.source_key, entity.source_entity_key,
                   entity.identity_basis, entity.entity_kind, entity.display_name,
                   entity.direct_profile_url
            FROM claimed
            JOIN public.entity_enrichment_source_entities AS entity
              ON entity.entity_id = claimed.entity_id;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_renew_entity_enrichment_job_lease(
            p_job_id uuid,
            p_lease_token uuid,
            p_lease_seconds integer
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_job_id IS NULL OR p_lease_token IS NULL
               OR p_lease_seconds IS NULL OR p_lease_seconds NOT BETWEEN 30 AND 3600
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'entity enrichment lease renewal input is invalid';
            END IF;
            UPDATE public.entity_enrichment_jobs AS job
            SET lease_expires_at = clock_timestamp()
                    + p_lease_seconds * INTERVAL '1 second',
                updated_at = clock_timestamp()
            FROM public.entity_enrichment_source_entities AS entity,
                 public.entity_enrichment_providers AS provider
            WHERE job.job_id = p_job_id
              AND job.lease_token = p_lease_token
              AND job.status = 'running'
              AND job.lease_expires_at > clock_timestamp()
              AND entity.entity_id = job.entity_id
              AND entity.entity_revision = job.requested_entity_revision
              AND provider.provider_key = job.provider_key
              AND provider.enabled
              AND provider.credentials_configured
              AND provider.terms_approved_at IS NOT NULL
              AND provider.operator_approved_at IS NOT NULL
              AND entity.entity_kind = ANY(provider.allowed_entity_kinds);
            RETURN FOUND;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_complete_entity_enrichment_job(
            p_job_id uuid,
            p_lease_token uuid
        )
        RETURNS boolean
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        BEGIN
            IF p_job_id IS NULL OR p_lease_token IS NULL THEN
                RETURN false;
            END IF;
            UPDATE public.entity_enrichment_jobs AS job
            SET status = 'succeeded', lease_token = NULL, lease_expires_at = NULL,
                completed_at = clock_timestamp(), error_code = NULL,
                updated_at = clock_timestamp()
            FROM public.entity_enrichment_source_entities AS entity,
                 public.entity_enrichment_providers AS provider
            WHERE job.job_id = p_job_id
              AND job.lease_token = p_lease_token
              AND job.status = 'running'
              AND job.lease_expires_at > clock_timestamp()
              AND entity.entity_id = job.entity_id
              AND entity.entity_revision = job.requested_entity_revision
              AND provider.provider_key = job.provider_key
              AND provider.enabled
              AND provider.credentials_configured
              AND provider.terms_approved_at IS NOT NULL
              AND provider.operator_approved_at IS NOT NULL
              AND entity.entity_kind = ANY(provider.allowed_entity_kinds)
              AND EXISTS (
                  SELECT 1 FROM public.entity_enrichment_observations AS observation
                  WHERE observation.job_id = job.job_id
                    AND observation.attempt_count = job.attempt_count
              );
            RETURN FOUND;
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_release_entity_enrichment_job(
            p_job_id uuid,
            p_lease_token uuid,
            p_error_code text,
            p_retry_after_seconds integer
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_status text;
        BEGIN
            IF p_job_id IS NULL OR p_lease_token IS NULL
               OR p_error_code IS NULL
               OR p_error_code NOT IN (
                   'provider_unavailable', 'rate_limited', 'transient',
                   'invalid_response', 'identity_unresolved', 'policy_blocked'
               )
               OR p_retry_after_seconds IS NULL
               OR p_retry_after_seconds NOT BETWEEN 1 AND 86400
            THEN
                RETURN 'invalid';
            END IF;
            UPDATE public.entity_enrichment_jobs AS job
            SET status = CASE
                    WHEN job.attempt_count >= 5
                         OR p_error_code IN (
                             'invalid_response', 'identity_unresolved', 'policy_blocked'
                         )
                        THEN 'failed'
                    ELSE 'queued'
                END,
                available_at = CASE
                    WHEN job.attempt_count >= 5
                         OR p_error_code IN (
                             'invalid_response', 'identity_unresolved', 'policy_blocked'
                         )
                        THEN job.available_at
                    ELSE clock_timestamp()
                        + p_retry_after_seconds * INTERVAL '1 second'
                END,
                lease_token = NULL, lease_expires_at = NULL,
                completed_at = CASE
                    WHEN job.attempt_count >= 5
                         OR p_error_code IN (
                             'invalid_response', 'identity_unresolved', 'policy_blocked'
                         )
                        THEN clock_timestamp()
                    ELSE NULL
                END,
                error_code = p_error_code, updated_at = clock_timestamp()
            WHERE job.job_id = p_job_id
              AND job.lease_token = p_lease_token
              AND job.status = 'running'
              AND job.lease_expires_at > clock_timestamp()
            RETURNING job.status INTO v_status;
            RETURN COALESCE(v_status, 'lease_lost');
        END;
        $$
        """
    )


def _create_observation_capabilities() -> None:
    """Install fenced writes, reviewed transitions, and an approved-only read."""
    op.execute(
        """
        CREATE FUNCTION public.fn_record_entity_enrichment_observation(
            p_job_id uuid,
            p_lease_token uuid,
            p_observation_id uuid,
            p_field_name text,
            p_field_value text,
            p_provenance_url text,
            p_provider_record_id text,
            p_match_basis text,
            p_confidence_bps integer,
            p_observed_at timestamptz,
            p_expires_at timestamptz
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_job public.entity_enrichment_jobs%ROWTYPE;
            v_entity public.entity_enrichment_source_entities%ROWTYPE;
            v_provider public.entity_enrichment_providers%ROWTYPE;
            v_existing public.entity_enrichment_observations%ROWTYPE;
        BEGIN
            IF p_job_id IS NULL OR p_lease_token IS NULL OR p_observation_id IS NULL
               OR p_field_name IS NULL
               OR p_field_name NOT IN (
                   'profile_url', 'headline', 'job_title', 'organization_name',
                   'organization_profile_url', 'seniority', 'function', 'industry',
                   'compensation_market'
               )
               OR p_field_value IS NULL
               OR char_length(p_field_value) NOT BETWEEN 1 AND 2048
               OR btrim(p_field_value) = ''
               OR p_field_value ~ '[[:cntrl:]]'
               OR (
                   p_field_name NOT IN ('profile_url', 'organization_profile_url')
                   AND char_length(p_field_value) > 500
               )
               OR p_provenance_url IS NULL
               OR char_length(p_provenance_url) NOT BETWEEN 9 AND 2048
               OR p_provenance_url ~ '[[:cntrl:][:space:]]'
               OR p_provenance_url ~ '#'
               OR p_provenance_url !~ '^https://[^/?#@[:space:]]+(/|[?]|$)'
               OR p_provider_record_id IS NULL
               OR char_length(p_provider_record_id) NOT BETWEEN 1 AND 500
               OR btrim(p_provider_record_id) = ''
               OR p_provider_record_id ~ '[[:cntrl:]]'
               OR p_match_basis IS NULL
               OR p_match_basis NOT IN (
                   'source_entity_id', 'source_profile_url',
                   'provider_crosswalk', 'manual_review'
               )
               OR p_confidence_bps IS NULL
               OR p_confidence_bps NOT BETWEEN 1 AND 10000
               OR p_observed_at IS NULL
               OR (p_expires_at IS NOT NULL AND p_expires_at <= p_observed_at)
            THEN
                RETURN 'invalid';
            END IF;
            SELECT job.* INTO v_job
            FROM public.entity_enrichment_jobs AS job
            WHERE job.job_id = p_job_id
            FOR UPDATE OF job;
            IF NOT FOUND THEN
                RETURN 'lease_lost';
            END IF;
            SELECT entity.* INTO v_entity
            FROM public.entity_enrichment_source_entities AS entity
            WHERE entity.entity_id = v_job.entity_id
            FOR SHARE OF entity;
            IF NOT FOUND THEN
                RETURN 'lease_lost';
            END IF;
            SELECT provider.* INTO v_provider
            FROM public.entity_enrichment_providers AS provider
            WHERE provider.provider_key = v_job.provider_key
            FOR SHARE OF provider;
            IF NOT FOUND
               OR NOT v_provider.enabled
               OR NOT v_provider.credentials_configured
               OR v_provider.terms_approved_at IS NULL
               OR v_provider.operator_approved_at IS NULL
               OR v_entity.entity_kind <> ALL(v_provider.allowed_entity_kinds)
            THEN
                RETURN 'lease_lost';
            END IF;
            IF v_job.requested_entity_revision <> v_entity.entity_revision THEN
                RETURN 'stale';
            END IF;
            IF v_job.status <> 'running'
               OR v_job.lease_token IS DISTINCT FROM p_lease_token
               OR v_job.lease_expires_at <= clock_timestamp()
            THEN
                RETURN 'lease_lost';
            END IF;
            IF lower(btrim(p_provider_record_id)) = lower(btrim(v_entity.display_name)) THEN
                RETURN 'invalid';
            END IF;
            IF p_match_basis = 'source_entity_id'
               AND v_entity.identity_basis <> 'source_entity_id'
            THEN
                RETURN 'invalid';
            END IF;
            IF p_match_basis = 'source_profile_url'
               AND v_entity.direct_profile_url IS NULL
            THEN
                RETURN 'invalid';
            END IF;
            IF p_field_name = 'profile_url'
               AND NOT public.fn_entity_enrichment_direct_profile_valid(
                   p_field_value, v_entity.entity_kind
               )
            THEN
                RETURN 'invalid';
            END IF;
            IF p_field_name = 'organization_profile_url'
               AND NOT public.fn_entity_enrichment_direct_profile_valid(
                   p_field_value, 'organization'
               )
            THEN
                RETURN 'invalid';
            END IF;

            SELECT observation.* INTO v_existing
            FROM public.entity_enrichment_observations AS observation
            WHERE observation.observation_id = p_observation_id;
            IF FOUND THEN
                IF v_existing.job_id = p_job_id
                   AND v_existing.attempt_count = v_job.attempt_count
                   AND v_existing.field_name = p_field_name
                   AND v_existing.field_value = p_field_value
                   AND v_existing.provenance_url = p_provenance_url
                   AND v_existing.provider_record_id = p_provider_record_id
                   AND v_existing.match_basis = p_match_basis
                   AND v_existing.confidence_bps = p_confidence_bps
                   AND v_existing.observed_at = p_observed_at
                   AND v_existing.expires_at IS NOT DISTINCT FROM p_expires_at
                THEN
                    RETURN 'replayed';
                END IF;
                RETURN 'conflict';
            END IF;
            SELECT observation.* INTO v_existing
            FROM public.entity_enrichment_observations AS observation
            WHERE observation.job_id = p_job_id
              AND observation.attempt_count = v_job.attempt_count
              AND observation.field_name = p_field_name
              AND observation.provider_record_id = p_provider_record_id;
            IF FOUND THEN
                RETURN 'conflict';
            END IF;
            INSERT INTO public.entity_enrichment_observations (
                observation_id, job_id, entity_id, attempt_count, field_name, field_value,
                provenance_url, provider_record_id, match_basis, confidence_bps,
                observed_at, expires_at
            ) VALUES (
                p_observation_id, p_job_id, v_entity.entity_id, v_job.attempt_count, p_field_name,
                p_field_value, p_provenance_url, p_provider_record_id, p_match_basis,
                p_confidence_bps, p_observed_at, p_expires_at
            );
            RETURN 'recorded';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_review_entity_enrichment_observation(
            p_observation_id uuid,
            p_decision text,
            p_reviewed_by text
        )
        RETURNS text
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_observation public.entity_enrichment_observations%ROWTYPE;
            v_job_status text;
        BEGIN
            IF p_observation_id IS NULL
               OR p_decision IS NULL
               OR p_decision NOT IN ('approved', 'rejected')
               OR p_reviewed_by IS NULL
               OR char_length(p_reviewed_by) NOT BETWEEN 1 AND 256
               OR btrim(p_reviewed_by) = ''
               OR p_reviewed_by ~ '[[:cntrl:]]'
            THEN
                RETURN 'invalid';
            END IF;
            SELECT observation.* INTO v_observation
            FROM public.entity_enrichment_observations AS observation
            WHERE observation.observation_id = p_observation_id
            FOR UPDATE OF observation;
            IF NOT FOUND THEN
                RETURN 'not_found';
            END IF;
            SELECT job.status INTO v_job_status
            FROM public.entity_enrichment_jobs AS job
            WHERE job.job_id = v_observation.job_id;
            IF v_job_status <> 'succeeded' THEN
                RETURN 'not_ready';
            END IF;
            IF v_observation.review_status <> 'pending' THEN
                IF v_observation.review_status = p_decision
                   AND v_observation.reviewed_by = p_reviewed_by
                THEN
                    RETURN 'replayed';
                END IF;
                RETURN 'conflict';
            END IF;
            UPDATE public.entity_enrichment_observations AS observation
            SET review_status = p_decision, reviewed_by = p_reviewed_by,
                reviewed_at = clock_timestamp()
            WHERE observation.observation_id = p_observation_id;
            RETURN 'reviewed';
        END;
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION public.fn_list_materializable_entity_enrichment(p_fact_id uuid)
        RETURNS TABLE (
            observation_id uuid,
            fact_id uuid,
            entity_id uuid,
            role text,
            field_name text,
            field_value text,
            provider_key text,
            provenance_url text,
            confidence_bps integer,
            reviewed_at timestamptz,
            expires_at timestamptz
        )
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
            SELECT DISTINCT ON (observation.field_name)
                   observation.observation_id,
                   fact.fact_id,
                   entity.entity_id,
                   fact.role,
                   observation.field_name,
                   observation.field_value,
                   job.provider_key,
                   observation.provenance_url,
                   observation.confidence_bps,
                   observation.reviewed_at,
                   observation.expires_at
            FROM public.entity_enrichment_source_facts AS fact
            JOIN public.entity_enrichment_source_entities AS entity
              ON entity.entity_id = fact.entity_id
            JOIN public.entity_enrichment_observations AS observation
              ON observation.entity_id = entity.entity_id
            JOIN public.entity_enrichment_jobs AS job
              ON job.job_id = observation.job_id
             AND job.entity_id = entity.entity_id
             AND job.requested_entity_revision = entity.entity_revision
             AND job.status = 'succeeded'
             AND observation.attempt_count = job.attempt_count
            WHERE fact.fact_id = p_fact_id
              AND fact.verification_status <> 'rejected'
              AND (fact.expires_at IS NULL OR fact.expires_at > statement_timestamp())
              AND observation.review_status = 'approved'
              AND observation.reviewed_at IS NOT NULL
              AND (
                  observation.expires_at IS NULL
                  OR observation.expires_at > statement_timestamp()
              )
              AND NOT (
                  entity.direct_profile_url IS NOT NULL
                  AND (
                      observation.field_name = 'profile_url'
                      OR (
                          entity.entity_kind = 'organization'
                          AND observation.field_name = 'organization_profile_url'
                      )
                  )
              )
              AND EXISTS (
                  SELECT 1
                  FROM public.catalog_event_observations AS current_observation
                  WHERE current_observation.source_key = fact.source_key
                    AND current_observation.source = fact.source
                    AND current_observation.source_event_id = fact.source_event_id
                    AND current_observation.last_run_key = fact.source_run_key
              )
            ORDER BY observation.field_name,
                     observation.reviewed_at DESC,
                     observation.confidence_bps DESC,
                     observation.observation_id DESC
        $$
        """
    )
