"""Key catalog entities by a normalized profile URL and read them as a bipartite ego graph.

Revision ID: 0156
Revises: 0155
Create Date: 2026-08-26

``0147`` derived a profile identity as ``'profile:' || md5(lower(profile_url))`` and ``0152``
carried that expression forward unchanged.  ``lower()`` is not a URL normalizer: every one of the
948 live profile rows stores the ``https://www.linkedin.com/...`` form, so the same LinkedIn
profile arriving from a second source as ``https://linkedin.com/...`` or with a trailing slash --
both spellings ``fn_event_entity_profiles_valid`` (``0128``) explicitly admits -- keys to a
different ``identity_key`` and mints a second catalog row for one human.  The catalog therefore
had no defence against duplicating people, and no way to detect that it had.

The fix is a normalizer that is **host-scoped**.  Collapsing ``www.``, port and trailing slashes
on arbitrary URLs would be worse than the disease: the 97 non-LinkedIn organization sites carry
meaning in their query strings, so ``https://example.org/events?org=a`` and ``?org=b`` would fuse
two organizations no source ever claimed were the same -- fuzzy identity through the URL door.
``fn_normalize_profile_url_v1`` is consequently a no-op returning ``lower(btrim(url))`` for every
host except ``linkedin.com``, where it collapses only the three variants the validator already
admits.  Query and fragment cannot appear on an admitted LinkedIn ``/in/`` or ``/company/`` URL --
the validator anchors ``.../?$`` -- so nothing that could carry meaning is stripped.

**The bijection this revision is built on, stated once.**  For every profile row,
``profile_key = fn_normalize_profile_url_v1(canonical_profile_url)`` and
``identity_key = 'profile:' || md5(profile_key)``.  ``md5`` is injective over the values in play,
so a ``profile_key`` collision is an ``identity_key`` collision and vice versa.  That matters
because ``0152``'s refresh is one ``INSERT ... GROUP BY identity_key ... ON CONFLICT
(identity_key)`` running in the same transaction as ``DELETE FROM
catalog_entity_event_mentions``: a unique index built on a normalizer *stricter* than the dedup
key would turn a duplicate row into a catalog-wide refresh abort.  Because the two derivations are
the same function of the same input, the ``GROUP BY`` collapses every ``profile_key`` collision
before the ``INSERT`` can see it, and ``ux_catalog_entities_profile_key`` can only fire if a future
edit desynchronizes them -- which is precisely the failure it exists to catch.

``profile_key`` is a **plain column written by the refresh in the same expression list that
computes ``identity_key``**, deliberately not ``GENERATED ALWAYS AS ... STORED``.  A stored
generated column is recomputed only on write, so a later ``CREATE OR REPLACE`` of the normalizer
would leave old rows on the old definition while ``identity_key`` -- computed at INSERT time --
picked up the new one, and the unique index would then enforce a stale normalization against a
fresh one.  Two derivations of one fact cannot be allowed to drift.

The re-key is an in-place ``UPDATE``, never ``DELETE`` + re-insert, and this is the highest
consequence decision in the revision.  ``0152`` orphan-sweeps ``catalog_entities`` rows that have
no mentions, so letting the refresh discover the new derivation on its own would mint fresh uuids
and sweep every original row -- breaking every shared ``?entity=<uuid>`` link and CASCADE-deleting
its ``catalog_entity_external_sources``, ``catalog_entity_external_facts`` and
``catalog_entity_source_links``.  Expected ``identity_key`` rotation is 851 rows (all the ``www.``
forms); expected ``entity_id`` rotation is zero.

A placeholder slug (``/in/NA``) is the one real fusion vector in the live data: "no answer" is
emitted by unrelated sources, and under ``ON CONFLICT (identity_key)`` two unrelated entities would
silently become one node.  It is quarantined at the only correct place -- inside the refresh's
``profiled`` LATERAL, where a placeholder profile is treated as **no profile**.  The row falls back
to ``'source:'`` keying, becomes ``source_scoped`` with ``canonical_profile_url = NULL``, and never
reaches the enrichment loop, so a garbage URL is never sent to a provider.  The rule reads slug
shape only; it never compares a URL to a display name, because a name is display data and search
text, not an identity or a join key.  For the same reason this revision ships **no display-name
shape heuristic and no doubt marker derived from slug shape** anywhere: a slug that merely looks
short is not evidence about whose profile it is -- measured against the live catalog, a
``^[a-z]{4,12}$`` rule flags 264 of 666 person profiles and the flagged sample is ~100% ordinary
firstname+lastname concatenations.  A rule that cannot be discharged without reading the display
name must not be shipped at all.  The honest surface is the URL, the kind the source asserted, and
which source asserted it.

``ck_catalog_entities_profile_key`` ships ``NOT VALID`` and is validated at the very end, after the
rebuild.  It is enforced against every insert and update from the moment it is created; ``NOT
VALID`` only defers the scan of rows that predate it, which is required because the column starts
NULL on 948 existing profile rows and the quarantined placeholder rows do not reach their final
shape until the refresh re-mints them.  Validating after the rebuild turns the deferral into an
assertion rather than a loophole.

``fn_get_catalog_entity_graph_v1`` reads the neighbourhood as **entity -> event -> entity and never
entity <-> entity**.  Every drawn line is a literal ``catalog_entity_event_mentions`` row carrying a
role and a source, so peers are reached through the evidence that connects them, provenance is
structural instead of a disclaimer, and no entity-to-entity relation exists that could later be
mistaken for a same-as claim.  A co-mention projection would have generated 1,156 pairs from the
two largest events alone; the bipartite spine renders those as 69 spokes into two event nodes.
Every traversal leg is capped in SQL and every cap reports both what was returned and what matched,
because there is no ``statement_timeout`` anywhere in ``src/`` and a silent truncation is a lie the
UI cannot detect.

``fn_get_catalog_entity_directory_v1`` replaces an alphabetical grid with a ranked front door that
returns its own totals and a coverage disclosure computed server-side, so the ranking and the
caveat about the ranking cannot drift apart.

``fn_get_catalog_entity_insights_v2`` supersedes and drops v1 for one reason: v1 hardcoded
``HAVING count(DISTINCT ...) >= 2`` on collaborators, which left 87% of entity detail pages with an
empty collaborator panel while the underlying graph is not sparse at all -- 63.7% of entities have
two or more co-mention peers and only 11.5% are isolated.  The floor becomes a parameter so the
caller can ask for 1.  v1 is dropped rather than kept beside v2: two near-identical 16-column
functions maintained forever is a standing invitation to fix a bug in one of them.

Nothing derived is persisted -- no degree column, no centrality cache, no entity-to-entity edge
table, no materialized view.  The refresh deletes and rebuilds the mention projection and every
table hanging off ``catalog_entities`` cascades, so any derived table would be silently wiped on
the next source refresh.  ``profile_key`` is the sole exception and it is not derived state: it is
written by the same statement, from the same input, as the key it must agree with.  Degree is
recomputed per request over 3,736 rows; revisit when that table exceeds 100,000 rows.

``fn_refresh_catalog_entity_index_v2`` is superseded and is **retained only for the downgrade
path**, not because it is unused today: it is the function both live write paths call, and this
revision moves them.  *This* revision's ``downgrade()`` calls v2 to rebuild the projection after
inverting the identity derivation, which is why dropping v2 here would break the alembic downgrade
contract.  (``0152``'s own ``downgrade()`` does *not* call v2 -- it ``DROP FUNCTION``s v2 and falls
back to ``fn_refresh_catalog_entity_index_v1``, which is why v1 is retained too.  Anyone tidying
superseded functions must check both facts before dropping either.)

Retained is not the same as reachable.  v2's ``INSERT`` column list has no ``profile_key`` and its
profile branch still derives ``'profile:' || md5(lower(url))``, so against the re-keyed rows it
would miss ``ON CONFLICT (identity_key)``, take the plain ``INSERT`` branch, and write
``canonical_profile_url NOT NULL`` with ``profile_key NULL`` -- a ``ck_catalog_entities_profile_key``
violation inside the publication transaction, i.e. a source refresh that fails closed on every
retry.  Two things therefore land together with this migration and cannot be split from it:
``adapters/postgres/catalog_refresh_commit.py`` and ``adapters/postgres/catalog_paged_promotion.py``
call ``fn_refresh_catalog_entity_index_v3``, and ``upgrade()`` **revokes ``EXECUTE`` on v2 from
``ec_app``** so the stale derivation is unreachable from the application while remaining available
to the migration role that runs ``downgrade()``.  ``downgrade()`` restores the grant, returning the
privilege surface to exactly what ``0152`` left.  The same rule applies to
``fn_get_catalog_entity_insights_v1``: it is dropped here, so
``adapters/postgres/catalog_entities.py`` moves to ``fn_get_catalog_entity_insights_v2(id, 1)`` in
this commit -- the 16-column shape is identical, so only the call text changes.

**Revision numbering deviates from the binding correction, deliberately.**  That correction measured
the alembic head as ``0154`` and assigned this work ``0155``/``down_revision = "0154"``.  The head
moved after it was written: ``0155_cadence_failure_aware_due_sources.py`` claimed ``0155``, and
``0157_tenant_profile.py`` already chains ``down_revision = "0156"``.  Keeping ``0155`` would make
alembic refuse every command with ``Duplicate revision identifiers: 0155``, and reverting to
``down_revision = "0154"`` would fork the head and orphan ``0157``.  This revision is therefore
``0156`` behind ``0155``, and any artifact that still names a ``0155`` module for this work is
stale.

``downgrade()`` restores behaviour but cannot be lossless, and says so plainly: the collision merge
is not reversed (two rows that were one LinkedIn profile stay one), and a quarantined placeholder
entity keeps the ``entity_id`` the rebuild gave it.  Reverting an identity derivation never is
lossless; the in-place ``UPDATE`` is what keeps it from being catastrophic.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0156"
down_revision: str | None = "0155"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NORMALIZE = "public.fn_normalize_profile_url_v1(text)"
_PLACEHOLDER = "public.fn_catalog_profile_url_placeholder_v1(text)"
_REFRESH_V3 = "public.fn_refresh_catalog_entity_index_v3(text)"
_GRAPH = "public.fn_get_catalog_entity_graph_v1(uuid,integer,integer,integer)"
_DIRECTORY = "public.fn_get_catalog_entity_directory_v1(text,text[],text[],integer,integer)"
_INSIGHTS_V2 = "public.fn_get_catalog_entity_insights_v2(uuid,integer)"
_INSIGHTS_V1 = "public.fn_get_catalog_entity_insights_v1(uuid)"
# Not created here: 0152 owns it.  Named so upgrade() can take the grant away and downgrade() can
# give it back, without either having to spell the signature inline.
_REFRESH_V2 = "public.fn_refresh_catalog_entity_index_v2(text)"

_PROFILE_KEY_CHECK = "ck_catalog_entities_profile_key"
_PROFILE_KEY_INDEX = "ux_catalog_entities_profile_key"


def upgrade() -> None:
    # The ordering is load-bearing: the helpers must exist before the re-key can call them, the
    # re-key must run before the unique index can be built, and the new refresh must not become
    # the steady state until the existing rows already agree with it.
    _create_identity_helpers()
    _add_profile_key_column()
    _rekey_existing_identities()
    _create_profile_key_unique_index()
    _create_refresh_v3()
    _create_graph()
    _create_directory()
    _create_insights_v2()
    for signature in (
        _NORMALIZE,
        _PLACEHOLDER,
        _REFRESH_V3,
        _GRAPH,
        _DIRECTORY,
        _INSIGHTS_V2,
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {signature} TO ec_app")
    op.execute(f"REVOKE ALL ON FUNCTION {_INSIGHTS_V1} FROM ec_app")
    op.execute(f"DROP FUNCTION IF EXISTS {_INSIGHTS_V1}")
    # v2 stays installed for downgrade(), but it must stop being callable by the application: it
    # cannot write profile_key and it still derives identity from lower(url), so against the rows
    # re-keyed above it would insert a constraint-violating duplicate rather than conflict-update.
    # The migration role keeps EXECUTE, so downgrade() can still call it; downgrade() restores this
    # grant so the privilege surface returns to exactly what 0152 left.
    op.execute(f"REVOKE EXECUTE ON FUNCTION {_REFRESH_V2} FROM ec_app")
    # House pattern, 0152:61-62: any change to the projection's identity derivation is followed by
    # a full rebuild and then the quality gate, in this order.  This reads stored observations
    # only -- no provider egress.
    op.execute("SELECT public.fn_refresh_catalog_entity_index_v3(NULL)")
    op.execute("SELECT public.fn_prune_catalog_entity_index_v1(NULL)")
    # Now that every row has been written by the new derivation, the deferred check becomes an
    # assertion: if the rebuild left a profile URL without a profile key, this aborts the upgrade.
    op.execute(f"ALTER TABLE public.catalog_entities VALIDATE CONSTRAINT {_PROFILE_KEY_CHECK}")


def downgrade() -> None:
    for signature in (_INSIGHTS_V2, _DIRECTORY, _GRAPH, _REFRESH_V3):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    _create_insights_v1()
    op.execute(f"REVOKE ALL ON FUNCTION {_INSIGHTS_V1} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {_INSIGHTS_V1} TO ec_app")
    op.execute(f"DROP INDEX IF EXISTS public.{_PROFILE_KEY_INDEX}")
    # Invert the re-key in place, so entity_id survives the way down as well as the way up.  The
    # collision merge is NOT reversed: rows that were proven to be one LinkedIn profile stay one.
    op.execute(
        """
        UPDATE public.catalog_entities
        SET identity_key = 'profile:' || md5(lower(canonical_profile_url)),
            updated_at = clock_timestamp()
        WHERE canonical_profile_url IS NOT NULL
        """
    )
    op.execute(f"ALTER TABLE public.catalog_entities DROP CONSTRAINT {_PROFILE_KEY_CHECK}")
    op.execute("ALTER TABLE public.catalog_entities DROP COLUMN profile_key")
    for signature in (_PLACEHOLDER, _NORMALIZE):
        op.execute(f"REVOKE ALL ON FUNCTION {signature} FROM ec_app")
        op.execute(f"DROP FUNCTION IF EXISTS {signature}")
    # Hand v2 back to ec_app before using it, so the schema and the privileges both land on what
    # 0152 left behind rather than on a half-reverted state.
    op.execute(f"GRANT EXECUTE ON FUNCTION {_REFRESH_V2} TO ec_app")
    op.execute("SELECT public.fn_refresh_catalog_entity_index_v2(NULL)")
    op.execute("SELECT public.fn_prune_catalog_entity_index_v1(NULL)")


def _create_identity_helpers() -> None:
    """Pure, table-free predicates.  IMMUTABLE STRICT so the planner can inline and fold them."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_normalize_profile_url_v1(p_url text)
        RETURNS text
        LANGUAGE sql
        IMMUTABLE
        STRICT
        SET search_path = pg_catalog, public
        AS $$
            -- Host-scoped by design.  Outside linkedin.com this is exactly today's expression,
            -- lower(btrim(url)): provably zero rotation and zero new collision risk for the org
            -- sites whose query strings carry meaning.  btrim is a no-op on admitted URLs --
            -- 0128 rejects any URL matching [[:cntrl:][:space:]].
            SELECT CASE
                WHEN lower(btrim(p_url)) ~ '^https://(www[.])?linkedin[.]com(:[0-9]{1,5})?/'
                THEN regexp_replace(
                         regexp_replace(
                             lower(btrim(p_url)),
                             '^https://(www[.])?linkedin[.]com(:[0-9]{1,5})?/',
                             'https://linkedin.com/'
                         ),
                         '/+$',
                         ''
                     )
                ELSE lower(btrim(p_url))
            END
        $$
        """
    )
    op.execute(
        r"""
        CREATE FUNCTION public.fn_catalog_profile_url_placeholder_v1(p_url text)
        RETURNS boolean
        LANGUAGE sql
        IMMUTABLE
        STRICT
        SET search_path = pg_catalog, public
        AS $$
            -- Slug shape only.  Never reads a display name: a name is display data, not identity.
            SELECT CASE
                WHEN public.fn_normalize_profile_url_v1(p_url) !~ '^https://linkedin[.]com/in/'
                    THEN false
                ELSE (
                    SELECT slug.value IN (
                               'na', 'n-a', 'none', 'null', 'nil', 'unknown', 'test', 'example',
                               'profile', 'user', 'username', 'linkedin', 'me', 'admin', 'feed'
                           )
                           OR char_length(slug.value) <= 3
                    FROM (
                        SELECT regexp_replace(
                                   public.fn_normalize_profile_url_v1(p_url),
                                   '^https://linkedin[.]com/in/', ''
                               ) AS value
                    ) AS slug
                )
            END
        $$
        """
    )


def _add_profile_key_column() -> None:
    op.execute("ALTER TABLE public.catalog_entities ADD COLUMN profile_key text")
    # NOT VALID defers only the scan of pre-existing rows -- the check is enforced on every insert
    # and update from this point on.  It has to be deferred: the column starts NULL on all 948
    # profile rows, and the quarantined placeholder rows do not reach their final shape until the
    # rebuild at the end of upgrade().  upgrade() validates it there.
    op.execute(
        f"""
        ALTER TABLE public.catalog_entities
        ADD CONSTRAINT {_PROFILE_KEY_CHECK}
        CHECK ((profile_key IS NULL) = (canonical_profile_url IS NULL))
        NOT VALID
        """
    )


def _rekey_existing_identities() -> None:
    """Re-derive identity_key in place.  entity_id must not rotate; nothing may cascade."""
    # 1. Collision merge.  Two rows that normalize to one URL are the SAME LinkedIn profile
    #    (www./port/trailing-slash variants).  Merging them is exactly what a verified direct
    #    profile URL authorizes as a cross-source identity.  This is not a name merge and it
    #    touches no display name.  The oldest row wins, so the longest-lived entity_id survives.
    op.execute(
        """
        CREATE TEMP TABLE profile_key_merge ON COMMIT DROP AS
        WITH keyed AS (
            SELECT entity_id,
                   created_at,
                   public.fn_normalize_profile_url_v1(canonical_profile_url) AS profile_key
            FROM public.catalog_entities
            WHERE canonical_profile_url IS NOT NULL
              AND NOT public.fn_catalog_profile_url_placeholder_v1(canonical_profile_url)
        ), ranked AS (
            SELECT keyed.*,
                   first_value(entity_id) OVER (
                       PARTITION BY profile_key ORDER BY created_at, entity_id
                   ) AS keep_entity_id
            FROM keyed
        )
        SELECT entity_id AS drop_entity_id, keep_entity_id, profile_key
        FROM ranked
        WHERE entity_id <> keep_entity_id
        """
    )
    # A large collision count means the normalizer is wrong, not the data.  Fail loudly rather
    # than quietly rewriting the catalog's identity graph.
    op.execute(
        """
        DO $$
        DECLARE v_groups integer;
        BEGIN
            SELECT count(DISTINCT profile_key) INTO v_groups FROM pg_temp.profile_key_merge;
            RAISE NOTICE 'profile_key collision groups: %', v_groups;
            IF v_groups > 50 THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'profile url normalization collides on more than 50 groups';
            END IF;
        END
        $$
        """
    )
    for statement in (
        # Mentions carry a 4-column PK.  Copy-then-delete, not UPDATE-in-place: a merge group can
        # hold three raw spellings of one profile, and two different losers can each own the same
        # (event, source, role) slot on the keeper.  An UPDATE guarded only by NOT EXISTS against
        # the pre-statement snapshot would let both pass and collide on the PK, aborting the whole
        # migration.  DISTINCT ON picks one deterministically -- newest observation first, exactly
        # the refresh's own tie-break -- and ON CONFLICT DO NOTHING yields to a row the keeper
        # already had.
        """
        INSERT INTO public.catalog_entity_event_mentions (
            entity_id, canonical_event_id, source_key, source, source_event_id,
            source_run_key, role, observed_name, observed_at
        )
        SELECT DISTINCT ON (
                   merge.keep_entity_id, mention.canonical_event_id, mention.source_key,
                   mention.role
               )
               merge.keep_entity_id,
               mention.canonical_event_id,
               mention.source_key,
               mention.source,
               mention.source_event_id,
               mention.source_run_key,
               mention.role,
               mention.observed_name,
               mention.observed_at
        FROM public.catalog_entity_event_mentions AS mention
        JOIN pg_temp.profile_key_merge AS merge
          ON merge.drop_entity_id = mention.entity_id
        ORDER BY merge.keep_entity_id, mention.canonical_event_id, mention.source_key,
                 mention.role, mention.observed_at DESC, mention.source_event_id
        ON CONFLICT (entity_id, canonical_event_id, source_key, role) DO NOTHING
        """,
        """
        DELETE FROM public.catalog_entity_event_mentions AS mention
        USING pg_temp.profile_key_merge AS merge
        WHERE mention.entity_id = merge.drop_entity_id
        """,
        # source_entity_id is UNIQUE on its own, so repointing catalog_entity_id cannot collide.
        """
        UPDATE public.catalog_entity_source_links AS link
        SET catalog_entity_id = merge.keep_entity_id
        FROM pg_temp.profile_key_merge AS merge
        WHERE link.catalog_entity_id = merge.drop_entity_id
        """,
        # Enrichment results are NOT duplicates by construction.  catalog_entity_external_sources
        # and _external_facts are written by the enrichment pipeline keyed on entity_id, and the
        # keeper is chosen purely as the oldest created_at -- it may never have been researched at
        # all while the loser carries a full provider fact set.  Deleting the loser and letting
        # ON DELETE CASCADE take those rows would repoint the loser's source link onto a keeper
        # that then shows 'researchable' with zero facts, irreversibly (downgrade does not undo
        # the merge).  So the keeper inherits every provider it lacks first.  What stays behind is
        # only a provider the keeper already holds for the same profile URL -- there the two rows
        # really are two reads of one profile, and the keeper's is the one to keep.  DISTINCT ON
        # is required for the same reason as the mentions above: the (entity_id, provider_key)
        # UNIQUE constraint would otherwise be violated by two losers carrying one provider.
        """
        WITH claimed AS (
            SELECT DISTINCT ON (merge.keep_entity_id, external.provider_key)
                   external.source_id,
                   merge.keep_entity_id
            FROM pg_temp.profile_key_merge AS merge
            JOIN public.catalog_entity_external_sources AS external
              ON external.entity_id = merge.drop_entity_id
            WHERE NOT EXISTS (
                SELECT 1 FROM public.catalog_entity_external_sources AS kept
                WHERE kept.entity_id = merge.keep_entity_id
                  AND kept.provider_key = external.provider_key
            )
            ORDER BY merge.keep_entity_id, external.provider_key,
                     external.fetched_at DESC NULLS LAST, external.source_id
        )
        UPDATE public.catalog_entity_external_sources AS external
        SET entity_id = claimed.keep_entity_id,
            updated_at = clock_timestamp()
        FROM claimed
        WHERE external.source_id = claimed.source_id
        """,
        # Facts hang off both source_id and entity_id; the statement above moved the source row,
        # so this restores the pair's agreement for exactly the moved rows.  Facts still pointing
        # at a loser after this are the ones whose provider stayed behind, and they cascade with it.
        """
        UPDATE public.catalog_entity_external_facts AS fact
        SET entity_id = external.entity_id
        FROM public.catalog_entity_external_sources AS external
        WHERE external.source_id = fact.source_id
          AND fact.entity_id <> external.entity_id
          AND fact.entity_id IN (SELECT drop_entity_id FROM pg_temp.profile_key_merge)
        """,
        """
        DELETE FROM public.catalog_entities AS entity
        USING pg_temp.profile_key_merge AS merge
        WHERE entity.entity_id = merge.drop_entity_id
        """,
    ):
        op.execute(statement)

    # 2. Placeholder quarantine, counted here so the operator sees it in the migration log.  These
    #    rows genuinely change identity class (cross-source -> source-scoped), so they are the ONE
    #    population allowed to rotate entity_id: keeping the uuid would carry a verified identity's
    #    history onto a scoped record.  The rebuild at the end of upgrade() re-mints them under
    #    'source:' keying and the orphan sweep retires the profile-keyed original.
    op.execute(
        """
        DO $$
        DECLARE v_count integer;
        BEGIN
            SELECT count(*) INTO v_count FROM public.catalog_entities
            WHERE canonical_profile_url IS NOT NULL
              AND public.fn_catalog_profile_url_placeholder_v1(canonical_profile_url);
            RAISE NOTICE 'placeholder profile urls quarantined: %', v_count;
        END
        $$
        """
    )

    # 3. The in-place re-key.  UPDATE, never DELETE+INSERT: entity_id survives, so every shared
    #    ?entity=<uuid> link and every external source/fact row survives with it.  identity_key
    #    and profile_key are written by one expression list, from one input, in one statement.
    op.execute(
        """
        UPDATE public.catalog_entities
        SET identity_key = 'profile:' || md5(
                public.fn_normalize_profile_url_v1(canonical_profile_url)
            ),
            profile_key = public.fn_normalize_profile_url_v1(canonical_profile_url),
            updated_at = clock_timestamp()
        WHERE canonical_profile_url IS NOT NULL
          AND NOT public.fn_catalog_profile_url_placeholder_v1(canonical_profile_url)
        """
    )
    # 4. Post-condition assertion.  Structural, not hopeful: if this fires the unique index below
    #    could not have been built, and the transaction is rolled back before anything is visible.
    op.execute(
        """
        DO $$
        DECLARE v_dupes integer;
        BEGIN
            SELECT count(*) INTO v_dupes FROM (
                SELECT profile_key FROM public.catalog_entities
                WHERE profile_key IS NOT NULL
                GROUP BY profile_key HAVING count(*) > 1
            ) AS collisions;
            IF v_dupes <> 0 THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'profile_key is not unique after re-key';
            END IF;
        END
        $$
        """
    )


def _create_profile_key_unique_index() -> None:
    """A tripwire, not a performance index.

    It is exactly equivalent to the existing ``identity_key UNIQUE`` restricted to profile rows,
    so it can never fire while the bijection holds.  It fires only if a future edit lets the two
    derivations drift -- and catching that at refresh time beats shipping duplicate humans.
    """
    op.execute(
        f"""
        CREATE UNIQUE INDEX {_PROFILE_KEY_INDEX}
        ON public.catalog_entities (profile_key)
        WHERE profile_key IS NOT NULL
        """
    )


def _create_refresh_v3() -> None:
    """v2 with four edits: the quarantine, the identity recipe, profile_key, and the name.

    Everything else -- the two-window UNION ALL, the role_facts LATERAL, the kind derivation, the
    delete scoping, the mention DISTINCT ON, the enrichment loop, the orphan sweep, the return --
    is byte-identical to v2, so the projection's behaviour stays auditable against 0152.
    """
    op.execute(
        r"""
        CREATE FUNCTION public.fn_refresh_catalog_entity_index_v3(p_source_key text)
        RETURNS integer
        LANGUAGE plpgsql
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_fact record;
            v_enrichment record;
            v_count integer;
        BEGIN
            IF p_source_key IS NOT NULL
               AND p_source_key !~ '^[a-z0-9][a-z0-9-]{1,79}$'
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'entity source is invalid';
            END IF;

            DROP TABLE IF EXISTS pg_temp.entity_refresh_facts;
            CREATE TEMP TABLE entity_refresh_facts ON COMMIT DROP AS
            WITH browse_observations AS MATERIALIZED (
                -- Live catalog rows: unchanged from the v1 projection.
                SELECT *
                FROM public.fn_list_retained_catalog_browse_observations_v1(
                    p_source_key, NULL, NULL
                )
                UNION ALL
                -- Retained history: only events that already ended enter this branch, so the two
                -- windows cannot return the same observation twice.
                SELECT *
                FROM public.fn_list_retained_catalog_browse_observations_v1(
                    p_source_key,
                    statement_timestamp() - interval '365 days',
                    statement_timestamp()
                )
            ), role_facts AS (
                SELECT observation.source_key,
                       observation.observation_source AS source,
                       observation.source_event_id,
                       observation.refresh_run_key AS source_run_key,
                       observation.canonical_event_id,
                       observation.last_seen_at AS observed_at,
                       role_fact.role,
                       role_fact.name
                FROM browse_observations AS observation
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = observation.canonical_event_id
                CROSS JOIN LATERAL (
                    SELECT 'organizer'::text, event.organizer_name
                    WHERE nullif(btrim(event.organizer_name), '') IS NOT NULL
                    UNION ALL
                    SELECT 'host'::text, name FROM unnest(event.host_names) AS name
                    UNION ALL
                    SELECT 'speaker'::text, name FROM unnest(event.speaker_names) AS name
                    UNION ALL
                    SELECT 'partner'::text, name FROM unnest(event.partner_names) AS name
                ) AS role_fact(role, name)
                WHERE nullif(btrim(role_fact.name), '') IS NOT NULL
            ), profiled AS (
                SELECT role_fact.*,
                       profile.value AS profile
                FROM role_facts AS role_fact
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = role_fact.canonical_event_id
                LEFT JOIN LATERAL (
                    SELECT candidate.value
                    FROM jsonb_array_elements(event.entity_profiles) AS candidate(value)
                    WHERE candidate.value ->> 'role' = role_fact.role
                      AND lower(btrim(candidate.value ->> 'name')) = lower(btrim(role_fact.name))
                      -- A placeholder slug is not an identity.  Dropping it here means the row
                      -- falls back to source-scoped keying AND never reaches the enrichment loop
                      -- below, so a garbage URL is never sent to a provider.
                      AND NOT public.fn_catalog_profile_url_placeholder_v1(
                              candidate.value ->> 'profile_url'
                          )
                    LIMIT 1
                ) AS profile ON true
            )
            SELECT source_key,
                   source,
                   source_event_id,
                   source_run_key,
                   canonical_event_id,
                   observed_at,
                   role,
                   btrim(name) AS observed_name,
                   regexp_replace(lower(btrim(name)), '\s+', ' ', 'g') AS normalized_name,
                   CASE
                       WHEN profile IS NOT NULL THEN profile ->> 'kind'
                       WHEN role IN ('organizer', 'partner') THEN 'organization'
                       WHEN role = 'speaker' THEN 'person'
                       WHEN role = 'host' AND name ~* (
                           '(^|[^[:alnum:]])(association|capital|club|collective|company|events|'
                           'foundation|group|inc|institute|labs?|library|llc|magazine|museum|'
                           'school|society|studio|university|ventures)([^[:alnum:]]|$)'
                       ) THEN 'organization'
                       ELSE 'unknown'
                   END AS kind,
                   CASE WHEN profile IS NOT NULL
                        THEN profile ->> 'profile_url' ELSE NULL END AS profile_url,
                   CASE WHEN profile IS NOT NULL THEN 'profile_verified'
                        ELSE 'source_scoped' END AS identity_status,
                   CASE WHEN profile IS NOT NULL
                        THEN 'profile:' || md5(
                            public.fn_normalize_profile_url_v1(profile ->> 'profile_url')
                        )
                        ELSE 'source:' || md5(
                            source_key || chr(31) ||
                            CASE
                                WHEN role IN ('organizer', 'partner') THEN 'organization'
                                WHEN role = 'speaker' THEN 'person'
                                WHEN role = 'host' AND name ~* (
                                    '(^|[^[:alnum:]])(association|capital|club|collective|company|'
                                    'events|foundation|group|inc|institute|labs?|library|llc|'
                                    'magazine|museum|school|society|studio|university|ventures)'
                                    '([^[:alnum:]]|$)'
                                ) THEN 'organization'
                                ELSE 'unknown'
                            END || chr(31) ||
                            regexp_replace(lower(btrim(name)), '\s+', ' ', 'g')
                        )
                   END AS identity_key,
                   -- Written from the same input, in the same expression list, as the key above.
                   -- profile_key collision <=> identity_key collision; the GROUP BY below
                   -- therefore collapses a collision before the INSERT can observe it.
                   CASE WHEN profile IS NOT NULL
                        THEN public.fn_normalize_profile_url_v1(profile ->> 'profile_url')
                        ELSE NULL
                   END AS profile_key
            FROM profiled;

            IF p_source_key IS NULL THEN
                DELETE FROM public.catalog_entity_event_mentions;
                DELETE FROM public.catalog_entity_source_links;
            ELSE
                DELETE FROM public.catalog_entity_event_mentions
                WHERE source_key = p_source_key;
                DELETE FROM public.catalog_entity_source_links
                WHERE source_key = p_source_key;
            END IF;

            INSERT INTO public.catalog_entities (
                identity_key, identity_status, kind, display_name, normalized_name,
                canonical_profile_url, profile_key, first_seen_at, last_seen_at
            )
            SELECT identity_key,
                   max(identity_status),
                   (array_agg(kind ORDER BY CASE kind
                       WHEN 'organization' THEN 1 WHEN 'person' THEN 2 ELSE 3 END))[1],
                   (array_agg(observed_name ORDER BY observed_at DESC, observed_name))[1],
                   max(normalized_name),
                   max(profile_url),
                   max(profile_key),
                   min(observed_at),
                   max(observed_at)
            FROM entity_refresh_facts
            GROUP BY identity_key
            ON CONFLICT (identity_key) DO UPDATE
            SET identity_status = EXCLUDED.identity_status,
                kind = EXCLUDED.kind,
                display_name = EXCLUDED.display_name,
                normalized_name = EXCLUDED.normalized_name,
                canonical_profile_url = EXCLUDED.canonical_profile_url,
                profile_key = EXCLUDED.profile_key,
                first_seen_at = least(public.catalog_entities.first_seen_at, EXCLUDED.first_seen_at),
                last_seen_at = greatest(public.catalog_entities.last_seen_at, EXCLUDED.last_seen_at),
                updated_at = clock_timestamp();

            INSERT INTO public.catalog_entity_event_mentions (
                entity_id, canonical_event_id, source_key, source, source_event_id,
                source_run_key, role, observed_name, observed_at
            )
            SELECT DISTINCT ON (
                       entity.entity_id, fact.canonical_event_id, fact.source_key, fact.role
                   )
                   entity.entity_id,
                   fact.canonical_event_id,
                   fact.source_key,
                   fact.source,
                   fact.source_event_id,
                   fact.source_run_key,
                   fact.role,
                   fact.observed_name,
                   fact.observed_at
            FROM entity_refresh_facts AS fact
            JOIN public.catalog_entities AS entity
              ON entity.identity_key = fact.identity_key
            ORDER BY entity.entity_id, fact.canonical_event_id, fact.source_key, fact.role,
                     fact.observed_at DESC, fact.source_event_id
            ON CONFLICT (entity_id, canonical_event_id, source_key, role) DO UPDATE
            SET source = EXCLUDED.source,
                source_event_id = EXCLUDED.source_event_id,
                source_run_key = EXCLUDED.source_run_key,
                observed_name = EXCLUDED.observed_name,
                observed_at = EXCLUDED.observed_at;

            -- Only exact, bounded profile URLs enter the enrichment identity plane.  Name-only
            -- entities remain browseable but cannot be researched automatically.
            FOR v_fact IN
                SELECT DISTINCT fact.*, entity.entity_id AS catalog_entity_id
                FROM entity_refresh_facts AS fact
                JOIN public.catalog_entities AS entity
                  ON entity.identity_key = fact.identity_key
                WHERE fact.profile_url IS NOT NULL
                  AND char_length(fact.profile_url) <= 500
                  AND fact.kind IN ('person', 'organization')
            LOOP
                SELECT * INTO v_enrichment
                FROM public.fn_upsert_entity_enrichment_source_fact(
                    v_fact.source_key,
                    v_fact.source,
                    v_fact.source_event_id,
                    v_fact.source_run_key,
                    v_fact.profile_url,
                    'source_profile_url',
                    v_fact.role,
                    v_fact.kind,
                    v_fact.observed_name,
                    v_fact.profile_url,
                    10000,
                    v_fact.observed_at,
                    NULL
                );
                IF v_enrichment.outcome IN ('inserted', 'updated', 'replayed') THEN
                    INSERT INTO public.catalog_entity_source_links (
                        catalog_entity_id, source_entity_id, source_key
                    )
                    VALUES (
                        v_fact.catalog_entity_id, v_enrichment.entity_id, v_fact.source_key
                    )
                    ON CONFLICT (source_entity_id) DO UPDATE
                    SET catalog_entity_id = EXCLUDED.catalog_entity_id,
                        source_key = EXCLUDED.source_key;
                END IF;
            END LOOP;

            DELETE FROM public.catalog_entities AS entity
            WHERE NOT EXISTS (
                SELECT 1 FROM public.catalog_entity_event_mentions AS mention
                WHERE mention.entity_id = entity.entity_id
            );
            SELECT count(*)::integer INTO v_count FROM public.catalog_entities;
            RETURN v_count;
        END;
        $$
        """
    )


def _create_graph() -> None:
    """The ego neighbourhood, bipartite by construction.

    Entity -> event -> entity only.  No entity-to-entity edge is emitted anywhere, so there is no
    line on screen that a reader could mistake for a same-as claim and no derived relation for a
    later change to promote into one.  Every cap is enforced here because there is no
    ``statement_timeout`` anywhere in ``src/``, and every cap reports both the returned count and
    the matched total so the caller can say what was dropped instead of pretending nothing was.
    """
    op.execute(
        r"""
        CREATE FUNCTION public.fn_get_catalog_entity_graph_v1(
            p_entity_id uuid,
            p_event_limit integer,
            p_peer_limit integer,
            p_topic_limit integer
        )
        RETURNS jsonb
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_ego record;
            v_result jsonb;
        BEGIN
            -- Validation runs before any scan, in the idiom of fn_list_catalog_entity_events_v2.
            IF p_entity_id IS NULL
               OR p_event_limit IS NULL OR p_event_limit NOT BETWEEN 1 AND 24
               OR p_peer_limit IS NULL OR p_peer_limit NOT BETWEEN 1 AND 48
               OR p_topic_limit IS NULL OR p_topic_limit NOT BETWEEN 0 AND 6
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'entity graph request is invalid';
            END IF;

            SELECT entity.entity_id, entity.display_name, entity.kind, entity.identity_status,
                   entity.canonical_profile_url, entity.profile_key, entity.normalized_name
              INTO v_ego
              FROM public.catalog_entities AS entity
             WHERE entity.entity_id = p_entity_id;
            IF NOT FOUND THEN
                RETURN NULL;
            END IF;

            WITH ego_events AS MATERIALIZED (
                SELECT event.canonical_event_id,
                       event.title,
                       event.start_at,
                       event.end_at,
                       event.venue_name,
                       event.city_norm,
                       event.topics,
                       event.price_status,
                       coalesce(event.end_at, event.start_at) <= statement_timestamp() AS is_past,
                       array_agg(DISTINCT mention.role ORDER BY mention.role) AS ego_roles
                  FROM public.catalog_entity_event_mentions AS mention
                  JOIN public.canonical_events AS event
                    ON event.canonical_event_id = mention.canonical_event_id
                 WHERE mention.entity_id = p_entity_id
                   AND event.event_status <> 'cancelled'
                 GROUP BY event.canonical_event_id
            ), event_totals AS (
                SELECT count(*) AS matched FROM ego_events
            ), events AS (
                -- Same order as fn_list_catalog_entity_events_v2: upcoming ascending, then past
                -- descending, so history can never crowd out an upcoming appearance.
                SELECT ego_events.*
                  FROM ego_events
                 ORDER BY is_past,
                          CASE WHEN is_past THEN NULL ELSE start_at END ASC NULLS LAST,
                          CASE WHEN is_past THEN start_at END DESC NULLS LAST,
                          canonical_event_id
                 LIMIT p_event_limit
            ), event_registration AS (
                SELECT mention.canonical_event_id,
                       min(observation.registration_url) AS registration_url
                  FROM public.catalog_entity_event_mentions AS mention
                  JOIN public.catalog_event_observations AS observation
                    ON observation.source_key = mention.source_key
                   AND observation.source = mention.source
                   AND observation.source_event_id = mention.source_event_id
                 WHERE mention.canonical_event_id IN (SELECT canonical_event_id FROM events)
                 GROUP BY mention.canonical_event_id
            ), event_degree AS (
                SELECT mention.canonical_event_id,
                       count(DISTINCT mention.entity_id) AS entity_count
                  FROM public.catalog_entity_event_mentions AS mention
                 WHERE mention.canonical_event_id IN (SELECT canonical_event_id FROM events)
                 GROUP BY mention.canonical_event_id
            ), peer_ranked AS (
                -- Peers at shared_event_count >= 1.  Admissible here in a way it is not in a
                -- co-mention projection, because the event that connects them is itself a drawn
                -- node carrying the role and the source: the tie is evidence, not a weight.
                SELECT other.entity_id,
                       other.display_name,
                       other.kind,
                       other.identity_status,
                       other.canonical_profile_url,
                       other.profile_key,
                       count(DISTINCT mention.canonical_event_id) AS shared_event_count,
                       count(DISTINCT mention.canonical_event_id)
                           FILTER (WHERE NOT events.is_past) AS shared_upcoming_count
                  FROM public.catalog_entity_event_mentions AS mention
                  JOIN events
                    ON events.canonical_event_id = mention.canonical_event_id
                  JOIN public.catalog_entities AS other
                    ON other.entity_id = mention.entity_id
                 WHERE mention.entity_id <> p_entity_id
                 GROUP BY other.entity_id
            ), peer_totals AS (
                SELECT count(*) AS matched FROM peer_ranked
            ), peers AS (
                SELECT * FROM peer_ranked
                 ORDER BY shared_event_count DESC, shared_upcoming_count DESC,
                          display_name, entity_id
                 LIMIT p_peer_limit
            ), degrees AS (
                -- One global degree per drawn entity: "17 events" is a property of the organizer,
                -- not of this frame.  Node radius reads off this and nothing else.
                SELECT mention.entity_id,
                       count(DISTINCT mention.canonical_event_id) AS degree
                  FROM public.catalog_entity_event_mentions AS mention
                 WHERE mention.entity_id = p_entity_id
                    OR mention.entity_id IN (SELECT entity_id FROM peers)
                 GROUP BY mention.entity_id
            ), topics AS (
                SELECT topic.value AS topic, count(*) AS event_count
                  FROM events
                  CROSS JOIN LATERAL unnest(events.topics) AS topic(value)
                 GROUP BY topic.value
                 ORDER BY count(*) DESC, topic.value
                 LIMIT p_topic_limit
            ), edges_raw AS (
                -- One edge per (entity, event); roles and sources are aggregated onto it, so a
                -- co-host pair is two 'host' edges into one event node rather than a synthesized
                -- entity-to-entity relation.
                SELECT mention.entity_id,
                       mention.canonical_event_id,
                       array_agg(DISTINCT mention.role ORDER BY mention.role) AS roles,
                       array_agg(DISTINCT source.display_name ORDER BY source.display_name)
                           AS source_labels,
                       max(mention.observed_at) AS observed_at
                  FROM public.catalog_entity_event_mentions AS mention
                  JOIN public.catalog_sources AS source
                    ON source.source_key = mention.source_key
                 WHERE mention.canonical_event_id IN (SELECT canonical_event_id FROM events)
                   AND (mention.entity_id = p_entity_id
                        OR mention.entity_id IN (SELECT entity_id FROM peers))
                 GROUP BY mention.entity_id, mention.canonical_event_id
            ), edge_totals AS (
                SELECT count(*) AS matched FROM edges_raw
            ), edges AS (
                SELECT * FROM edges_raw
                 ORDER BY entity_id, canonical_event_id
                 LIMIT 600
            ), same_name AS (
                -- Review candidates.  EXACT normalized_name only: normalized_name is pure
                -- whitespace/case folding (0147).  No token, prefix or fuzzy matching is offered,
                -- because a near-name match would make a name into an identity judgment.
                -- Surfaced, never merged, never joined.
                SELECT other.entity_id, other.display_name, other.kind, other.identity_status,
                       count(DISTINCT mention.canonical_event_id) AS event_count
                  FROM public.catalog_entities AS other
                  JOIN public.catalog_entity_event_mentions AS mention
                    ON mention.entity_id = other.entity_id
                 WHERE other.normalized_name = v_ego.normalized_name
                   AND other.entity_id <> p_entity_id
                 GROUP BY other.entity_id
                 ORDER BY count(DISTINCT mention.canonical_event_id) DESC, other.display_name
                 LIMIT 5
            )
            SELECT jsonb_build_object(
                'focus_id', 'entity:' || p_entity_id::text,
                'generated_at', statement_timestamp(),
                'counts', jsonb_build_object(
                    'events', (SELECT count(*) FROM events),
                    'events_total', (SELECT matched FROM event_totals),
                    'peers', (SELECT count(*) FROM peers),
                    'peers_total', (SELECT matched FROM peer_totals),
                    'topics', (SELECT count(*) FROM topics),
                    -- 'edges' counts the emitted array, which is mention edges UNION ALL one
                    -- entity->topic edge per topic node; a name that does not match its quantity
                    -- is the same silent lie as an unreported truncation.  'mention_edges' is the
                    -- capped subset, and it is the one 'edges_total' and truncated.edges compare
                    -- against, because topic edges are derived from the already-capped event set
                    -- and are never truncated.
                    'edges', (SELECT count(*) FROM edges) + (SELECT count(*) FROM topics),
                    'mention_edges', (SELECT count(*) FROM edges),
                    'edges_total', (SELECT matched FROM edge_totals)
                ),
                'truncated', jsonb_build_object(
                    'events', (SELECT matched FROM event_totals) > (SELECT count(*) FROM events),
                    'peers', (SELECT matched FROM peer_totals) > (SELECT count(*) FROM peers),
                    'edges', (SELECT matched FROM edge_totals) > (SELECT count(*) FROM edges)
                ),
                'nodes',
                (SELECT coalesce(jsonb_agg(node ORDER BY node ->> 'node_id'), '[]'::jsonb) FROM (
                    SELECT jsonb_build_object(
                        'node_id', 'entity:' || v_ego.entity_id::text,
                        'node_kind', 'entity',
                        'ring', 0,
                        'label', v_ego.display_name,
                        'entity_id', v_ego.entity_id,
                        'entity_kind', v_ego.kind,
                        'identity_status', v_ego.identity_status,
                        'profile_url', v_ego.canonical_profile_url,
                        'profile_key', v_ego.profile_key,
                        'degree', (SELECT degree FROM degrees
                                    WHERE degrees.entity_id = v_ego.entity_id),
                        'shared_event_count', NULL,
                        'roles', (SELECT array_agg(DISTINCT mention.role ORDER BY mention.role)
                                    FROM public.catalog_entity_event_mentions AS mention
                                   WHERE mention.entity_id = p_entity_id)
                    ) AS node
                    UNION ALL
                    SELECT jsonb_build_object(
                        'node_id', 'event:' || events.canonical_event_id::text,
                        'node_kind', 'event',
                        'ring', 1,
                        'label', events.title,
                        'canonical_event_id', events.canonical_event_id,
                        'start_at', events.start_at,
                        'end_at', events.end_at,
                        'is_past', events.is_past,
                        'venue_name', events.venue_name,
                        'city', events.city_norm,
                        'price_status', events.price_status,
                        'topics', to_jsonb(coalesce(events.topics, '{}'::text[])),
                        'ego_roles', to_jsonb(events.ego_roles),
                        'registration_url', event_registration.registration_url,
                        'degree', event_degree.entity_count
                    )
                      FROM events
                      LEFT JOIN event_registration USING (canonical_event_id)
                      LEFT JOIN event_degree USING (canonical_event_id)
                    UNION ALL
                    SELECT jsonb_build_object(
                        'node_id', 'entity:' || peers.entity_id::text,
                        'node_kind', 'entity',
                        'ring', 2,
                        'label', peers.display_name,
                        'entity_id', peers.entity_id,
                        'entity_kind', peers.kind,
                        'identity_status', peers.identity_status,
                        'profile_url', peers.canonical_profile_url,
                        'profile_key', peers.profile_key,
                        'degree', degrees.degree,
                        'shared_event_count', peers.shared_event_count
                    )
                      FROM peers LEFT JOIN degrees USING (entity_id)
                    UNION ALL
                    SELECT jsonb_build_object(
                        'node_id', 'topic:' || topics.topic,
                        'node_kind', 'topic',
                        'ring', 3,
                        'label', topics.topic,
                        'degree', topics.event_count
                    )
                      FROM topics
                ) AS assembled),
                'edges',
                (SELECT coalesce(jsonb_agg(edge), '[]'::jsonb) FROM (
                    SELECT jsonb_build_object(
                        'a', 'entity:' || edges.entity_id::text,
                        'b', 'event:' || edges.canonical_event_id::text,
                        'kind', 'mention',
                        'roles', to_jsonb(edges.roles),
                        'source_labels', to_jsonb(edges.source_labels),
                        'observed_at', edges.observed_at
                    ) AS edge
                      FROM edges
                    UNION ALL
                    SELECT jsonb_build_object(
                        'a', 'entity:' || p_entity_id::text,
                        'b', 'topic:' || topics.topic,
                        'kind', 'topic',
                        'roles', '[]'::jsonb,
                        'source_labels', '[]'::jsonb,
                        'observed_at', NULL
                    )
                      FROM topics
                ) AS assembled),
                'same_name_candidates',
                (SELECT coalesce(jsonb_agg(jsonb_build_object(
                            'entity_id', same_name.entity_id,
                            'display_name', same_name.display_name,
                            'kind', same_name.kind,
                            'identity_status', same_name.identity_status,
                            'event_count', same_name.event_count
                        ) ORDER BY same_name.event_count DESC, same_name.display_name), '[]'::jsonb)
                   FROM same_name)
            ) INTO v_result;

            RETURN v_result;
        END;
        $$
        """
    )


def _create_directory() -> None:
    """The ranked front door, one request, with the coverage caveat computed beside the ranking.

    ``coverage`` is returned rather than hardcoded in the UI so the disclosure ("only 4.0% of
    catalog events name an organizer, host, speaker or partner") cannot drift away from the
    ranking it qualifies.  ``peer_counts`` is deliberately correlated over the <= 60 ranked rows
    rather than a whole-graph self-join.
    """
    op.execute(
        r"""
        CREATE FUNCTION public.fn_get_catalog_entity_directory_v1(
            p_query text,
            p_kinds text[],
            p_city_norms text[],
            p_limit integer,
            p_min_events integer
        )
        RETURNS jsonb
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_result jsonb;
        BEGIN
            IF p_limit IS NULL OR p_limit NOT BETWEEN 1 AND 60
               OR p_min_events IS NULL OR p_min_events NOT BETWEEN 1 AND 10
               OR length(coalesce(p_query, '')) > 160
               OR p_query ~ '[\x00-\x1f\x7f]'
               OR cardinality(coalesce(p_kinds, '{}'::text[])) > 3
               OR EXISTS (
                   SELECT 1 FROM unnest(coalesce(p_kinds, '{}'::text[])) AS kind(value)
                   WHERE kind.value NOT IN ('person', 'organization', 'unknown')
               )
               OR cardinality(coalesce(p_city_norms, '{}'::text[])) > 8
               OR EXISTS (
                   SELECT 1 FROM unnest(coalesce(p_city_norms, '{}'::text[])) AS city(value)
                   WHERE length(city.value) > 64 OR city.value ~ '[\x00-\x1f\x7f]'
               )
            THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'entity directory request is invalid';
            END IF;

            WITH scoped AS MATERIALIZED (
                SELECT entity.entity_id,
                       entity.display_name,
                       entity.kind,
                       entity.identity_status,
                       entity.canonical_profile_url,
                       entity.profile_key,
                       entity.normalized_name,
                       count(DISTINCT mention.canonical_event_id) AS event_count,
                       count(DISTINCT mention.canonical_event_id)
                           FILTER (WHERE coalesce(event.end_at, event.start_at)
                                         > statement_timestamp()) AS upcoming_count,
                       count(DISTINCT mention.source_key) AS source_count,
                       array_agg(DISTINCT mention.role ORDER BY mention.role) AS roles,
                       max(event.start_at) AS last_event_at,
                       (array_agg(event.city_norm
                           ORDER BY event.start_at DESC NULLS LAST))[1] AS top_city
                  FROM public.catalog_entities AS entity
                  JOIN public.catalog_entity_event_mentions AS mention
                    ON mention.entity_id = entity.entity_id
                  JOIN public.canonical_events AS event
                    ON event.canonical_event_id = mention.canonical_event_id
                 WHERE event.event_status <> 'cancelled'
                   AND (cardinality(coalesce(p_kinds, '{}'::text[])) = 0
                        OR entity.kind = ANY(p_kinds))
                   AND (cardinality(coalesce(p_city_norms, '{}'::text[])) = 0
                        OR event.city_norm = ANY(p_city_norms))
                   AND (nullif(btrim(p_query), '') IS NULL
                        OR entity.search_document @@ plainto_tsquery('simple', p_query)
                        -- LIKE metacharacters are escaped, so the substring branch matches what
                        -- the reader typed.  Unescaped, a query of '%' matches every row and the
                        -- front door presents the whole unfiltered ranking as search results,
                        -- while a run of '_' silently means "any name at least this long".  The
                        -- tsquery branch cannot compensate: plainto_tsquery('simple','%') is an
                        -- empty query, which leaves LIKE as the only matching branch.  '!' is the
                        -- escape character and is escaped first, so it survives as a literal.
                        OR entity.normalized_name LIKE
                           '%' || replace(
                                      replace(
                                          replace(lower(btrim(p_query)), '!', '!!'),
                                          '%', '!%'
                                      ),
                                      '_', '!_'
                                  ) || '%' ESCAPE '!')
                 GROUP BY entity.entity_id
            ), ranked AS (
                SELECT * FROM scoped
                 WHERE event_count >= p_min_events
                 ORDER BY event_count DESC, upcoming_count DESC, display_name, entity_id
                 LIMIT p_limit
            ), peer_counts AS (
                -- Bounded to the <= 60 ranked rows: one small lookup each, never a whole-graph
                -- self-join.
                SELECT ranked.entity_id,
                       (SELECT count(DISTINCT other.entity_id)
                          FROM public.catalog_entity_event_mentions AS mine
                          JOIN public.catalog_entity_event_mentions AS other
                            ON other.canonical_event_id = mine.canonical_event_id
                           AND other.entity_id <> mine.entity_id
                         WHERE mine.entity_id = ranked.entity_id) AS peer_count
                  FROM ranked
            ), totals AS (
                SELECT count(*) AS entity_count,
                       count(*) FILTER (WHERE kind = 'person') AS person_count,
                       count(*) FILTER (WHERE kind = 'organization') AS organization_count,
                       count(*) FILTER (WHERE kind = 'unknown') AS unknown_count,
                       count(*) FILTER (
                           WHERE identity_status = 'profile_verified'
                       ) AS verified_count,
                       count(*) FILTER (
                           WHERE identity_status = 'source_scoped'
                       ) AS scoped_count
                  FROM public.catalog_entities
            ), coverage AS (
                SELECT (SELECT count(DISTINCT canonical_event_id)
                          FROM public.catalog_entity_event_mentions) AS events_with_entities,
                       (SELECT count(*) FROM public.canonical_events
                         WHERE event_status <> 'cancelled') AS events_total,
                       (SELECT count(*)
                          FROM public.catalog_entity_event_mentions) AS mention_count
            )
            SELECT jsonb_build_object(
                'generated_at', statement_timestamp(),
                'totals', (SELECT to_jsonb(totals) FROM totals),
                'coverage', (SELECT to_jsonb(coverage) FROM coverage),
                'matched', (SELECT count(*) FROM scoped WHERE event_count >= p_min_events),
                'hubs', (SELECT coalesce(jsonb_agg(jsonb_build_object(
                            'entity_id', ranked.entity_id,
                            'display_name', ranked.display_name,
                            'kind', ranked.kind,
                            'identity_status', ranked.identity_status,
                            'profile_url', ranked.canonical_profile_url,
                            'profile_key', ranked.profile_key,
                            'event_count', ranked.event_count,
                            'upcoming_count', ranked.upcoming_count,
                            'peer_count', peer_counts.peer_count,
                            'source_count', ranked.source_count,
                            'roles', to_jsonb(ranked.roles),
                            'top_city', ranked.top_city,
                            'last_event_at', ranked.last_event_at
                        ) ORDER BY ranked.event_count DESC, ranked.upcoming_count DESC,
                                   ranked.display_name, ranked.entity_id), '[]'::jsonb)
                   FROM ranked LEFT JOIN peer_counts USING (entity_id))
            ) INTO v_result;

            RETURN v_result;
        END;
        $$
        """
    )


def _create_insights_v2() -> None:
    """v1 with the collaborator floor parameterised.

    v1 hardcoded ``>= 2`` shared events, which is why 87% of entity detail pages rendered an empty
    collaborator panel against a graph in which only 11.5% of entities are actually isolated.  The
    columns are identical to v1 on purpose: the caller's mapper does not change, only the floor.
    """
    op.execute(
        r"""
        CREATE FUNCTION public.fn_get_catalog_entity_insights_v2(
            p_entity_id uuid,
            p_min_shared integer
        )
        RETURNS TABLE (
            event_count bigint,
            upcoming_count bigint,
            past_count bigint,
            first_event_at timestamptz,
            last_event_at timestamptz,
            recent_event_count bigint,
            active_months integer,
            events_per_month numeric,
            typical_attendance integer,
            free_count bigint,
            paid_count bigint,
            top_topics text[],
            top_venues text[],
            top_cities text[],
            source_labels text[],
            collaborators jsonb
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_window_start timestamptz := statement_timestamp() - interval '365 days';
            v_window_end timestamptz := statement_timestamp() + interval '90 days';
        BEGIN
            IF p_min_shared IS NULL OR p_min_shared NOT BETWEEN 1 AND 5 THEN
                RAISE EXCEPTION USING ERRCODE = '22023',
                    MESSAGE = 'entity insights request is invalid';
            END IF;
            RETURN QUERY
            WITH mentioned AS MATERIALIZED (
                SELECT DISTINCT event.canonical_event_id,
                       event.start_at,
                       event.end_at,
                       event.venue_name,
                       event.city_norm,
                       event.topics,
                       event.price_status,
                       event.attendance_count
                FROM public.catalog_entity_event_mentions AS mention
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = mention.canonical_event_id
                WHERE mention.entity_id = p_entity_id
                  AND event.event_status <> 'cancelled'
            ), totals AS (
                SELECT count(*) AS event_count,
                       count(*) FILTER (
                           WHERE coalesce(end_at, start_at) > statement_timestamp()
                       ) AS upcoming_count,
                       count(*) FILTER (
                           WHERE coalesce(end_at, start_at) <= statement_timestamp()
                       ) AS past_count,
                       min(start_at) AS first_event_at,
                       max(start_at) AS last_event_at,
                       count(*) FILTER (WHERE price_status = 'free') AS free_count,
                       count(*) FILTER (WHERE price_status = 'paid') AS paid_count
                FROM mentioned
            ), cadence AS (
                SELECT count(*) AS recent_event_count,
                       greatest(
                           1,
                           ceil(
                               extract(epoch FROM (max(start_at) - min(start_at)))
                               / 2629746.0
                           )::integer
                       ) AS active_months
                FROM mentioned
                WHERE start_at >= v_window_start
                  AND start_at < v_window_end
            ), attendance AS (
                SELECT round(
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY attendance_count)
                       )::integer AS typical_attendance
                FROM mentioned
                WHERE attendance_count IS NOT NULL
                  AND attendance_count > 0
            ), topics AS (
                SELECT array_agg(topic ORDER BY topic_count DESC, topic) AS values
                FROM (
                    SELECT selected.topic, count(*) AS topic_count
                    FROM mentioned
                    CROSS JOIN LATERAL unnest(mentioned.topics) AS selected(topic)
                    GROUP BY selected.topic
                    ORDER BY count(*) DESC, selected.topic
                    LIMIT 6
                ) AS ranked
            ), venues AS (
                SELECT array_agg(venue_name ORDER BY venue_count DESC, venue_name) AS values
                FROM (
                    SELECT mentioned.venue_name, count(*) AS venue_count
                    FROM mentioned
                    WHERE nullif(btrim(mentioned.venue_name), '') IS NOT NULL
                    GROUP BY mentioned.venue_name
                    ORDER BY count(*) DESC, mentioned.venue_name
                    LIMIT 3
                ) AS ranked
            ), cities AS (
                SELECT array_agg(city_norm ORDER BY city_count DESC, city_norm) AS values
                FROM (
                    SELECT mentioned.city_norm, count(*) AS city_count
                    FROM mentioned
                    WHERE nullif(btrim(mentioned.city_norm), '') IS NOT NULL
                    GROUP BY mentioned.city_norm
                    ORDER BY count(*) DESC, mentioned.city_norm
                    LIMIT 3
                ) AS ranked
            ), sources AS (
                SELECT array_agg(
                           DISTINCT source.display_name ORDER BY source.display_name
                       ) AS values
                FROM public.catalog_entity_event_mentions AS mention
                JOIN public.catalog_sources AS source
                  ON source.source_key = mention.source_key
                WHERE mention.entity_id = p_entity_id
            ), peers AS (
                SELECT jsonb_agg(
                           jsonb_build_object(
                               'entity_id', peer_entity_id,
                               'display_name', display_name,
                               'kind', kind,
                               'shared_event_count', shared_event_count
                           )
                           ORDER BY shared_event_count DESC, display_name
                       ) AS values
                FROM (
                    SELECT other.entity_id AS peer_entity_id,
                           other.display_name,
                           other.kind,
                           count(DISTINCT peer.canonical_event_id) AS shared_event_count
                    FROM public.catalog_entity_event_mentions AS peer
                    JOIN public.catalog_entities AS other
                      ON other.entity_id = peer.entity_id
                    WHERE peer.entity_id <> p_entity_id
                      AND peer.canonical_event_id IN (
                          SELECT canonical_event_id FROM mentioned
                      )
                    GROUP BY other.entity_id, other.display_name, other.kind
                    HAVING count(DISTINCT peer.canonical_event_id) >= p_min_shared
                    ORDER BY count(DISTINCT peer.canonical_event_id) DESC, other.display_name,
                             other.entity_id
                    LIMIT 12
                ) AS ranked
            )
            SELECT totals.event_count,
                   totals.upcoming_count,
                   totals.past_count,
                   totals.first_event_at,
                   totals.last_event_at,
                   coalesce(cadence.recent_event_count, 0),
                   coalesce(cadence.active_months, 1),
                   CASE
                       WHEN coalesce(cadence.recent_event_count, 0) = 0 THEN NULL
                       ELSE round(
                           cadence.recent_event_count::numeric
                           / greatest(cadence.active_months, 1),
                           1
                       )
                   END,
                   attendance.typical_attendance,
                   totals.free_count,
                   totals.paid_count,
                   coalesce(topics.values, '{}'::text[]),
                   coalesce(venues.values, '{}'::text[]),
                   coalesce(cities.values, '{}'::text[]),
                   coalesce(sources.values, '{}'::text[]),
                   coalesce(peers.values, '[]'::jsonb)
            FROM totals
            CROSS JOIN cadence
            CROSS JOIN attendance
            CROSS JOIN topics
            CROSS JOIN venues
            CROSS JOIN cities
            CROSS JOIN sources
            CROSS JOIN peers;
        END;
        $$
        """
    )


def _create_insights_v1() -> None:
    """Restore 0152's body verbatim so the downgrade lands on exactly what 0152 left behind."""
    op.execute(
        r"""
        CREATE FUNCTION public.fn_get_catalog_entity_insights_v1(p_entity_id uuid)
        RETURNS TABLE (
            event_count bigint,
            upcoming_count bigint,
            past_count bigint,
            first_event_at timestamptz,
            last_event_at timestamptz,
            recent_event_count bigint,
            active_months integer,
            events_per_month numeric,
            typical_attendance integer,
            free_count bigint,
            paid_count bigint,
            top_topics text[],
            top_venues text[],
            top_cities text[],
            source_labels text[],
            collaborators jsonb
        )
        LANGUAGE plpgsql
        STABLE
        SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
            v_window_start timestamptz := statement_timestamp() - interval '365 days';
            v_window_end timestamptz := statement_timestamp() + interval '90 days';
        BEGIN
            RETURN QUERY
            WITH mentioned AS MATERIALIZED (
                SELECT DISTINCT event.canonical_event_id,
                       event.start_at,
                       event.end_at,
                       event.venue_name,
                       event.city_norm,
                       event.topics,
                       event.price_status,
                       event.attendance_count
                FROM public.catalog_entity_event_mentions AS mention
                JOIN public.canonical_events AS event
                  ON event.canonical_event_id = mention.canonical_event_id
                WHERE mention.entity_id = p_entity_id
                  AND event.event_status <> 'cancelled'
            ), totals AS (
                SELECT count(*) AS event_count,
                       count(*) FILTER (
                           WHERE coalesce(end_at, start_at) > statement_timestamp()
                       ) AS upcoming_count,
                       count(*) FILTER (
                           WHERE coalesce(end_at, start_at) <= statement_timestamp()
                       ) AS past_count,
                       min(start_at) AS first_event_at,
                       max(start_at) AS last_event_at,
                       count(*) FILTER (WHERE price_status = 'free') AS free_count,
                       count(*) FILTER (WHERE price_status = 'paid') AS paid_count
                FROM mentioned
            ), cadence AS (
                SELECT count(*) AS recent_event_count,
                       greatest(
                           1,
                           ceil(
                               extract(epoch FROM (max(start_at) - min(start_at)))
                               / 2629746.0
                           )::integer
                       ) AS active_months
                FROM mentioned
                WHERE start_at >= v_window_start
                  AND start_at < v_window_end
            ), attendance AS (
                SELECT round(
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY attendance_count)
                       )::integer AS typical_attendance
                FROM mentioned
                WHERE attendance_count IS NOT NULL
                  AND attendance_count > 0
            ), topics AS (
                SELECT array_agg(topic ORDER BY topic_count DESC, topic) AS values
                FROM (
                    SELECT selected.topic, count(*) AS topic_count
                    FROM mentioned
                    CROSS JOIN LATERAL unnest(mentioned.topics) AS selected(topic)
                    GROUP BY selected.topic
                    ORDER BY count(*) DESC, selected.topic
                    LIMIT 6
                ) AS ranked
            ), venues AS (
                SELECT array_agg(venue_name ORDER BY venue_count DESC, venue_name) AS values
                FROM (
                    SELECT mentioned.venue_name, count(*) AS venue_count
                    FROM mentioned
                    WHERE nullif(btrim(mentioned.venue_name), '') IS NOT NULL
                    GROUP BY mentioned.venue_name
                    ORDER BY count(*) DESC, mentioned.venue_name
                    LIMIT 3
                ) AS ranked
            ), cities AS (
                SELECT array_agg(city_norm ORDER BY city_count DESC, city_norm) AS values
                FROM (
                    SELECT mentioned.city_norm, count(*) AS city_count
                    FROM mentioned
                    WHERE nullif(btrim(mentioned.city_norm), '') IS NOT NULL
                    GROUP BY mentioned.city_norm
                    ORDER BY count(*) DESC, mentioned.city_norm
                    LIMIT 3
                ) AS ranked
            ), sources AS (
                SELECT array_agg(
                           DISTINCT source.display_name ORDER BY source.display_name
                       ) AS values
                FROM public.catalog_entity_event_mentions AS mention
                JOIN public.catalog_sources AS source
                  ON source.source_key = mention.source_key
                WHERE mention.entity_id = p_entity_id
            ), peers AS (
                SELECT jsonb_agg(
                           jsonb_build_object(
                               'entity_id', peer_entity_id,
                               'display_name', display_name,
                               'kind', kind,
                               'shared_event_count', shared_event_count
                           )
                           ORDER BY shared_event_count DESC, display_name
                       ) AS values
                FROM (
                    SELECT other.entity_id AS peer_entity_id,
                           other.display_name,
                           other.kind,
                           count(DISTINCT peer.canonical_event_id) AS shared_event_count
                    FROM public.catalog_entity_event_mentions AS peer
                    JOIN public.catalog_entities AS other
                      ON other.entity_id = peer.entity_id
                    WHERE peer.entity_id <> p_entity_id
                      AND peer.canonical_event_id IN (
                          SELECT canonical_event_id FROM mentioned
                      )
                    GROUP BY other.entity_id, other.display_name, other.kind
                    HAVING count(DISTINCT peer.canonical_event_id) >= 2
                    ORDER BY count(DISTINCT peer.canonical_event_id) DESC, other.display_name
                    LIMIT 8
                ) AS ranked
            )
            SELECT totals.event_count,
                   totals.upcoming_count,
                   totals.past_count,
                   totals.first_event_at,
                   totals.last_event_at,
                   coalesce(cadence.recent_event_count, 0),
                   coalesce(cadence.active_months, 1),
                   CASE
                       WHEN coalesce(cadence.recent_event_count, 0) = 0 THEN NULL
                       ELSE round(
                           cadence.recent_event_count::numeric
                           / greatest(cadence.active_months, 1),
                           1
                       )
                   END,
                   attendance.typical_attendance,
                   totals.free_count,
                   totals.paid_count,
                   coalesce(topics.values, '{}'::text[]),
                   coalesce(venues.values, '{}'::text[]),
                   coalesce(cities.values, '{}'::text[]),
                   coalesce(sources.values, '{}'::text[]),
                   coalesce(peers.values, '[]'::jsonb)
            FROM totals
            CROSS JOIN cadence
            CROSS JOIN attendance
            CROSS JOIN topics
            CROSS JOIN venues
            CROSS JOIN cities
            CROSS JOIN sources
            CROSS JOIN peers;
        END;
        $$
        """
    )
