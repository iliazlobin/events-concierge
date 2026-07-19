"""Persist public watch-poll freshness state without enabling a provider feeder.

Revision ID: 0062
Revises: 0061
Create Date: 2026-07-18

ADR-008 requires a central detector to make missed polls and coverage loss observable. This
control-plane table tracks only a distinct public canonical-event/source watch and opaque timing
facts; tenant identity, credentials, raw source responses, and provider cadence defaults remain
outside it (FR-8.7a, NFR-17, ADR-008).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0062"
down_revision: str | None = "0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a lease-safe public watch-poll cursor and its bounded ready index."""
    op.execute(
        """
        CREATE TABLE public.watch_poll_state (
            canonical_event_id  uuid NOT NULL,
            source              text NOT NULL,
            first_seen_at       timestamptz NOT NULL DEFAULT now(),
            last_attempt_at     timestamptz,
            last_success_at     timestamptz,
            last_failure_at     timestamptz,
            consecutive_failures integer NOT NULL DEFAULT 0,
            next_due_at         timestamptz NOT NULL DEFAULT now(),
            attempt_count       integer NOT NULL DEFAULT 0,
            lease_token         text,
            lease_expires_at    timestamptz,
            last_error_type     text,
            PRIMARY KEY (canonical_event_id, source),
            FOREIGN KEY (canonical_event_id, source)
                REFERENCES public.watch_registry(canonical_event_id, source)
                ON DELETE CASCADE,
            CHECK (consecutive_failures >= 0),
            CHECK (attempt_count >= 0),
            CHECK (
                (lease_token IS NULL AND lease_expires_at IS NULL)
                OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
            ),
            CHECK (last_error_type IS NULL OR length(last_error_type) <= 128)
        )
        """
    )
    op.execute(
        """
        CREATE INDEX ix_watch_poll_state_ready
        ON public.watch_poll_state (next_due_at, lease_expires_at, canonical_event_id, source)
        """
    )
    op.execute("REVOKE ALL PRIVILEGES ON public.watch_poll_state FROM PUBLIC")
    op.execute("REVOKE ALL PRIVILEGES ON public.watch_poll_state FROM ec_app")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON public.watch_poll_state TO ec_app")


def downgrade() -> None:
    """Drop only the derived central polling cursor; watch/delivery state remains intact."""
    op.execute("DROP TABLE IF EXISTS public.watch_poll_state")
