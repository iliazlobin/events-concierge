"""initial schema: catalog (tenant-neutral) + tenant tables under FORCE RLS

Revision ID: 0001
Revises:
Create Date: 2026-07-15
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBED_DIM = 384


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ---- CATALOG (tenant-neutral: NO tenant_id column anywhere, no RLS) ----
    op.execute(
        """
        CREATE TABLE source_policy (
            source              text PRIMARY KEY,
            automation_allowed  jsonb NOT NULL DEFAULT '{}'::jsonb,
            paid_allowed        boolean NOT NULL DEFAULT false,
            quarantined         boolean NOT NULL DEFAULT false,
            signed_agent_mode   text NOT NULL DEFAULT 'none'
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE canonical_events (
            canonical_event_id  uuid PRIMARY KEY,
            title               text NOT NULL,
            start_at            timestamptz NOT NULL,
            end_at              timestamptz,
            venue_name          text,
            lat                 double precision,
            lon                 double precision,
            city_norm           text,
            event_status        text NOT NULL DEFAULT 'scheduled',
            description         text NOT NULL DEFAULT '',
            is_free             boolean NOT NULL DEFAULT true,
            embedding           vector({EMBED_DIM}),
            tsv                 tsvector GENERATED ALWAYS AS (
                                    to_tsvector('english',
                                        coalesce(title,'') || ' ' || coalesce(description,''))
                                ) STORED,
            normalizer_version  integer NOT NULL DEFAULT 1,
            merge_version       integer NOT NULL DEFAULT 1,
            created_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_canonical_city_date ON canonical_events (city_norm, start_at)")
    op.execute("CREATE INDEX ix_canonical_tsv ON canonical_events USING gin (tsv)")
    op.execute(
        "CREATE INDEX ix_canonical_title_trgm ON canonical_events USING gin (title gin_trgm_ops)"
    )
    op.execute(
        "CREATE INDEX ix_canonical_embedding ON canonical_events "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute(
        """
        CREATE TABLE event_source_links (
            source              text NOT NULL,
            source_event_id     text NOT NULL,
            canonical_event_id  uuid NOT NULL REFERENCES canonical_events(canonical_event_id)
                                    ON DELETE CASCADE,
            registration_url    text NOT NULL,
            last_seen_at        timestamptz,
            PRIMARY KEY (source, source_event_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_source_links_canonical ON event_source_links (canonical_event_id)")
    op.execute(
        """
        CREATE TABLE canonical_alias (
            loser_id     uuid PRIMARY KEY,
            survivor_id  uuid NOT NULL,
            created_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # ---- TENANT identity (minimal; not RLS-forced -- onboarding/admin writes it) ----
    op.execute(
        """
        CREATE TABLE tenants (
            tenant_id      uuid PRIMARY KEY,
            oidc_subject   text UNIQUE NOT NULL,
            notify_email   text NOT NULL,
            relay_inbox    text UNIQUE NOT NULL,
            created_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )

    # ---- TENANT DATA (FORCE RLS; tenant_id leading-indexed) ----
    op.execute(
        """
        CREATE TABLE event_requests (
            request_id   uuid PRIMARY KEY,
            tenant_id    uuid NOT NULL,
            raw_text     text NOT NULL,
            constraints  jsonb NOT NULL DEFAULT '{}'::jsonb,
            state        text NOT NULL DEFAULT 'received',
            created_at   timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE lifecycle (
            lifecycle_id        uuid PRIMARY KEY,
            tenant_id           uuid NOT NULL,
            canonical_event_id  uuid NOT NULL,
            workflow_id         text UNIQUE NOT NULL,
            state               text NOT NULL DEFAULT 'found',
            lane                text,
            conflict_warning    boolean NOT NULL DEFAULT false,
            created_at          timestamptz NOT NULL DEFAULT now(),
            updated_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    # One non-terminal lifecycle row per (tenant, event) (ADR-007).
    op.execute(
        """
        CREATE UNIQUE INDEX ux_lifecycle_active ON lifecycle (tenant_id, canonical_event_id)
        WHERE state NOT IN ('completed','cancelled','expired','failed_no_candidate')
        """
    )
    op.execute(
        """
        CREATE TABLE transition_ledger (
            transition_id  text PRIMARY KEY,
            tenant_id      uuid NOT NULL,
            lifecycle_id   uuid NOT NULL,
            to_state       text NOT NULL,
            created_at     timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE handoff_tasks (
            task_id             text PRIMARY KEY,
            tenant_id           uuid NOT NULL,
            workflow_id         text NOT NULL,
            canonical_event_id  uuid NOT NULL,
            reason              text NOT NULL,
            deep_link           text NOT NULL,
            event_summary       text NOT NULL,
            ttl_expires_at      timestamptz NOT NULL,
            state               text NOT NULL DEFAULT 'open',
            metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
            created_at          timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ix_handoff_tenant ON handoff_tasks (tenant_id, state)")
    op.execute(
        """
        CREATE TABLE outbox (
            id            bigserial PRIMARY KEY,
            tenant_id     uuid,
            topic         text NOT NULL,
            payload       jsonb NOT NULL,
            created_at    timestamptz NOT NULL DEFAULT now(),
            delivered_at  timestamptz
        )
        """
    )
    op.execute("CREATE INDEX ix_outbox_undelivered ON outbox (id) WHERE delivered_at IS NULL")

    for table in ("event_requests", "lifecycle", "transition_ledger", "handoff_tasks"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"""
            CREATE POLICY tenant_isolation ON {table}
                USING (tenant_id = current_setting('app.tenant_id', true)::uuid)
                WITH CHECK (tenant_id = current_setting('app.tenant_id', true)::uuid)
            """
        )
        op.execute(f"CREATE INDEX ix_{table}_tenant ON {table} (tenant_id)")


def downgrade() -> None:
    for table in (
        "outbox",
        "handoff_tasks",
        "transition_ledger",
        "lifecycle",
        "event_requests",
        "tenants",
        "canonical_alias",
        "event_source_links",
        "canonical_events",
        "source_policy",
    ):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
