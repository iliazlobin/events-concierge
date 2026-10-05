"use client";

import {
  ArrowUpRight,
  AtSign,
  BookOpen,
  Building2,
  CalendarDays,
  Camera,
  Code2,
  Globe2,
  Hash,
  IdCard,
  LoaderCircle,
  Music,
  RefreshCw,
  UserRound,
  UsersRound,
  Video,
  X,
} from "lucide-react";
import type { ReactNode } from "react";
import { useEffect, useMemo, useRef, useState } from "react";

import { getCatalogEntity, refreshCatalogEntity } from "@/lib/api";
import { formatEventDate, formatEventTime } from "@/lib/date";
import type {
  CatalogEntityGraphEdge,
  CatalogEntityGraphNode,
  EntityGraphSceneModel,
  EntityGraphTextAppearance,
  EntityGraphTextModel,
} from "@/lib/entity-graph";
import { readEntityDetail, writeEntityDetail } from "@/lib/entity-graph-cache";
import { SOCIAL_API_PROVIDERS, socialProfileCards } from "@/lib/entity-social-profiles";
import type {
  EntitySourceGlyph,
  EntitySourcePresentation,
} from "@/lib/entity-inspector-model";
import {
  appearanceDateLabel,
  appearanceProvenance,
  entityFrameActivity,
  entityOverviewLines,
  entitySourcePresentation,
  groupEntityAppearances,
  groupEntitySources,
  roleLabel,
} from "@/lib/entity-inspector-model";
import { EntityIdentityLinks } from "@/components/entity-identity-links";
import { identityLinks } from "@/lib/entity-identity-links";
import { eventTopicLabel } from "@/lib/event-topics";
import { formatCity } from "@/lib/presentation";
import type {
  CatalogEntityDetail,
  CatalogEntityExternalFact,
  CatalogEntityExternalSource,
} from "@/lib/types";

/**
 * The structured public values worth showing, in reading order.
 *
 * A closed list rather than "render whatever arrived": the fact vocabulary is a database CHECK
 * shared with providers, so an unlabelled key reaching the panel would render as a raw enum.
 */
const PROFILE_FACTS: ReadonlyArray<readonly [CatalogEntityExternalFact["fact_key"], string]> = [
  ["location", "Location"],
  ["founded", "Founded"],
  ["entity_type", "Type"],
  ["job_title", "Role"],
  ["organization", "Organization"],
  ["industry", "Industry"],
  ["focus", "Focus"],
  ["known_for", "Known for"],
  ["public_repositories", "Public repositories"],
  ["followers", "Followers"],
];

/** First value per key, case-folded, so two sources asserting the same thing render once. */
/**
 * The provenance tail for one mention: which source asserted it, and when it was seen.
 *
 * Returned with its leading separator so it can be appended after a role, and stripped when the
 * same sentence is hoisted above the list.
 */
function edgeProvenance(edge: CatalogEntityGraphEdge): string {
  return [
    edge.source_labels.length ? `asserted by ${edge.source_labels.join(", ")}` : "",
    edge.observed_at ? `seen ${observedLabel(edge.observed_at)}` : "",
  ]
    .filter(Boolean)
    .map((part) => ` · ${part}`)
    .join("");
}

function firstFact(
  detail: CatalogEntityDetail | null,
  key: CatalogEntityExternalFact["fact_key"],
): CatalogEntityExternalFact | null {
  return detail?.external_facts.find((fact) => fact.fact_key === key && !SOCIAL_API_PROVIDERS.has(fact.provider_key)) ?? null;
}

/**
 * The reading panel beside the graph.
 *
 * Hard contract, and the reason this component exists as its own file: the "Profile & sources" tab
 * is the ONLY thing in this feature allowed to call `getCatalogEntity`, the five-round-trip detail
 * route, and it calls it once, on first open, then serves a session cache.  Nothing on first paint
 * touches it — first paint is one graph request and nothing else.
 */

const TABS = [
  { value: "overview", label: "Overview" },
  { value: "appearances", label: "Appearances" },
  { value: "profile", label: "Profile & sources" },
  { value: "same-name", label: "Same name" },
] as const;

type InspectorTab = (typeof TABS)[number]["value"];

/*
 * The detail payload is cached in `entity-graph-cache.ts`, not here.
 *
 * It is in memory only and never in `sessionStorage` — it carries names, venues and links, and
 * `catalog-cache.ts` already draws exactly that line for event records. Keeping it beside the graph
 * and directory bundles means sign-out and account erasure clear all three with one call, instead
 * of having to know that a component module holds a fourth cache of its own.
 */

/**
 * Neutral marks for the source kinds.
 *
 * lucide-react ships no brand glyphs and nothing here inlines one: a brand mark would be an
 * unlicensed asset, and next to a URL a source merely published it would read as a verification
 * badge. These name the kind of page a link is, which is all the record actually knows.
 */
const SOURCE_GLYPHS: Record<EntitySourceGlyph, ReactNode> = {
  website: <Globe2 aria-hidden="true" />,
  code: <Code2 aria-hidden="true" />,
  reference: <BookOpen aria-hidden="true" />,
  identity: <IdCard aria-hidden="true" />,
  handle: <AtSign aria-hidden="true" />,
  photo: <Camera aria-hidden="true" />,
  music: <Music aria-hidden="true" />,
  video: <Video aria-hidden="true" />,
  generic: <Globe2 aria-hidden="true" />,
};

function nodeGlyph(node: CatalogEntityGraphNode): ReactNode {
  if (node.node_kind === "event") return <CalendarDays aria-hidden="true" />;
  if (node.node_kind === "topic") return <Hash aria-hidden="true" />;
  if (node.entity_kind === "organization") return <Building2 aria-hidden="true" />;
  if (node.entity_kind === "person") return <UserRound aria-hidden="true" />;
  return <UsersRound aria-hidden="true" />;
}

function kindLabel(node: CatalogEntityGraphNode): string {
  if (node.node_kind === "event") return "Event";
  if (node.node_kind === "topic") return "Topic";
  if (node.entity_kind === "organization") return "Organization";
  if (node.entity_kind === "person") return "Person";
  return "Public event entity";
}

function observedLabel(value: string | null): string {
  if (!value) return "";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return "";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(parsed);
}

function SocialAvatar({ url }: { url: string | null }) {
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  return (
    <span className="entity-social-profile__avatar">
      {url && failedUrl !== url ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={url} alt="" width={40} height={40} loading="lazy" referrerPolicy="no-referrer"
          onError={() => setFailedUrl(url)} />
      ) : <UserRound aria-hidden="true" />}
    </span>
  );
}

export interface EntityInspectorProps {
  tenantId: string | null;
  canRefresh?: boolean;
  /** The original occurrence scene, retained beneath the canvas session grouping. */
  model: EntityGraphSceneModel;
  textModel: EntityGraphTextModel;
  /** The node under inspection; `null` reads the ego. */
  eventSessions?: CatalogEntityGraphNode[];
  selectedNodeId: string | null;
  onSelectNode: (nodeId: string | null) => void;
  onFocusEntity: (entityId: string) => void;
  onHoverNode: (nodeId: string | null) => void;
}

export function EntityInspector({
  tenantId,
  canRefresh = false,
  model,
  textModel,
  selectedNodeId,
  eventSessions,
  onSelectNode,
  onFocusEntity,
  onHoverNode,
}: EntityInspectorProps) {
  const subject = (selectedNodeId ? model.byId.get(selectedNodeId) : undefined)
    ?? model.ego
    ?? null;

  const [tab, setTab] = useState<InspectorTab>("overview");
  const [detail, setDetail] = useState<CatalogEntityDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const autoRefreshAttempted = useRef(new Set<string>());
  /**
   * The subject whose "Profile & sources" tab was opened, not a boolean.
   *
   * Latching rather than reading `tab` inside the fetch effect is what stops that effect from
   * re-running — and therefore from cancelling itself — when the reader tabs away and back before
   * the response lands. But a boolean latch cannot invalidate itself: on a subject change the
   * reset effect below schedules `false` while the fetch effect, running later in the SAME commit,
   * still reads `true` against the already-updated entity id, and fires the five-round-trip detail
   * route for a subject nobody asked about. Walking a 32-peer ring with the tab left open once
   * would issue one such request per hop against a five-connection, no-overflow pool.
   *
   * Holding the node id instead makes the latch self-invalidating with no cross-effect ordering
   * dependency: it stops matching the moment the subject changes.
   */
  const [profileOpenedFor, setProfileOpenedFor] = useState<string | null>(null);

  const subjectNodeId = subject?.node_id ?? null;
  const subjectEntityId = subject?.node_kind === "entity" ? subject.entity_id : null;

  // Read through a ref so the latch effect below can depend on `tab` alone.  If it also depended
  // on the subject it would re-run on a subject change — while `tab` is still "profile", because
  // the reset above has only scheduled its update — and re-latch onto the new subject, which is
  // exactly the fetch this latch exists to prevent.
  const subjectNodeIdRef = useRef(subjectNodeId);
  subjectNodeIdRef.current = subjectNodeId;

  useEffect(() => {
    setTab("overview");
  }, [subjectNodeId]);

  useEffect(() => {
    if (tab === "profile") setProfileOpenedFor(subjectNodeIdRef.current);
  }, [tab]);

  useEffect(() => {
    if (!subjectEntityId) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    setDetail(readEntityDetail(tenantId, subjectEntityId));
    setDetailError(null);
  }, [subjectEntityId, tenantId]);

  useEffect(() => {
    if (profileOpenedFor !== subjectNodeId || !subjectEntityId) return;
    const cached = readEntityDetail(tenantId, subjectEntityId);
    if (cached) {
      setDetail(cached);
      setDetailError(null);
      return;
    }
    let cancelled = false;
    setDetailLoading(true);
    setDetailError(null);
    void getCatalogEntity(tenantId, subjectEntityId)
      .then((item) => {
        // Written even if this component has moved on, so the next open costs nothing.
        writeEntityDetail(tenantId, subjectEntityId, item);
        if (!cancelled) setDetail(item);
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setDetail(null);
          setDetailError(
            caught instanceof Error ? caught.message : "Public sources could not be loaded.",
          );
        }
      })
      .finally(() => {
        if (!cancelled) setDetailLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [profileOpenedFor, subjectEntityId, subjectNodeId, tenantId]);

  /**
   * Re-read the entity's own exact profile/source URLs.
   *
   * Only ever the URLs a source already attached to this entity: nothing here searches for a
   * profile, and a name is never turned into a query.
   */
  const refreshSources = async () => {
    if (!canRefresh || !subjectEntityId || refreshing) return;
    setRefreshing(true);
    setDetailError(null);
    try {
      const item = await refreshCatalogEntity(tenantId, subjectEntityId);
      writeEntityDetail(tenantId, subjectEntityId, item);
      setDetail(item);
    } catch (caught: unknown) {
      setDetailError(
        caught instanceof Error ? caught.message : "Public sources could not be refreshed.",
      );
    } finally {
      setRefreshing(false);
    }
  };

  // A verified profile whose crawl window has elapsed refreshes itself once per entity per session.
  // Gated on `profile_verified` because a source-scoped record has no exact URL to re-read.
  useEffect(() => {
    if (
      !canRefresh
      || !subjectEntityId
      || !detail?.refresh_due
      || detail.entity.identity_status !== "profile_verified"
      || autoRefreshAttempted.current.has(subjectEntityId)
    ) return;
    autoRefreshAttempted.current.add(subjectEntityId);
    void refreshSources();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refreshSources is stable per render
  }, [canRefresh, detail?.entity.identity_status, detail?.refresh_due, subjectEntityId]);

  const appearancesByNode = useMemo(() => {
    const index = new Map<string, EntityGraphTextAppearance>();
    for (const item of [...textModel.upcoming, ...textModel.past]) index.set(item.node_id, item);
    return index;
  }, [textModel]);

  /** Appearances for whichever node is under inspection: all of the ego's, or a peer's shared set. */
  const appearances = useMemo(() => {
    if (!subject || subject.node_kind !== "entity") return [];
    const nodeIds = subject.node_id === model.focusId
      ? model.events.map((node) => node.node_id)
      : model.peerEvents.get(subject.node_id) ?? [];
    return nodeIds
      .map((nodeId) => appearancesByNode.get(nodeId))
      .filter((item): item is EntityGraphTextAppearance => item !== undefined);
  }, [appearancesByNode, model, subject]);

  const egoLabel = model.ego?.label ?? "the focus";

  /**
   * The one identity the graph node itself carries, for the Overview.
   *
   * Scoped to `profile_url` deliberately. `detail` — and with it every external fact and provider
   * record — is fetched only when the reader opens "Profile & sources", per the hard contract
   * above. A row on the *default* tab fed from `detail` would therefore show one link on a first
   * visit and silently grow after a trip to the third tab and back, so the panel's content would
   * depend on which tabs you had visited. The full set is rendered where the data is actually
   * loaded, below.
   */
  const egoIdentityLinks = useMemo(
    () => (subject?.node_kind === "entity" ? identityLinks(subject.profile_url) : []),
    [subject],
  );

  /**
   * Every public identity we hold for the inspected entity, for the Profile & sources tab.
   *
   * Gathered from all three places a URL can reach us — the identity the catalog keyed on, the
   * public-data facts behind it, and the provider records — so the row is whatever is genuinely on
   * file rather than only the one field the projection happened to key. This is also where the
   * social rows the enrichment plane now captures (`x_profile`, `instagram_profile`,
   * `tiktok_profile`, `youtube_profile`) surface as links rather than only as list entries.
   */
  const identityProfileLinks = useMemo(() => {
    if (!subject || subject.node_kind !== "entity") return [];
    return identityLinks(
      subject.profile_url,
      (detail?.external_facts ?? []).filter((fact) => fact.fact_key !== "avatar").map((fact) => fact.value_url),
      (detail?.external_sources ?? []).map((source) => source.source_url),
    );
  }, [detail, subject]);

  if (!subject) return null;

  if (subject.node_kind === "event") {
    const date = subject.start_at ? formatEventDate(subject.start_at) : null;
    const named: CatalogEntityGraphEdge[] = model.edges.filter(
      (edge) => edge.kind === "mention" && edge.b === subject.node_id,
    );
    const provenances = new Set(named.map(edgeProvenance));
    const sharedProvenance = named.length > 1 && provenances.size === 1
      ? [...provenances][0]?.replace(/^ · /, "") || null
      : null;
    return (
      <aside className="entity-graph-inspector" aria-label="Event detail">
        <header className="entity-graph-inspector__head">
          <span className="entity-hero__icon">{nodeGlyph(subject)}</span>
          <div>
            <p>{kindLabel(subject)}</p>
            <h2>{subject.label}</h2>
            <span>
              {[
                date ? `${date.weekday} ${date.month} ${date.day}` : null,
                subject.start_at ? formatEventTime(subject.start_at, subject.end_at) : null,
                [subject.venue_name, formatCity(subject.city)].filter(Boolean).join(" · "),
              ].filter(Boolean).join(" — ")}
            </span>
          </div>
          <button
            type="button"
            className="entity-graph-inspector__close"
            onClick={() => onSelectNode(null)}
          >
            <X aria-hidden="true" />
            <span className="sr-only">Back to {egoLabel}</span>
          </button>
        </header>

        {eventSessions && eventSessions.length > 1 ? (
          <section aria-label="Event dates">
            <h3>{eventSessions.length} dates in this graph</h3>
            <p className="entity-graph-inspector__provenance">Matching sessions. Select a date to see its own details and source evidence.</p>
            <ul className="entity-graph-inspector__mentions">
              {eventSessions.map((session) => (
                <li key={session.node_id}>
                  <button type="button" aria-pressed={session.node_id === subject.node_id}
                    onClick={() => onSelectNode(session.node_id)}>
                    <strong>{session.start_at ? new Intl.DateTimeFormat(undefined, {year:"numeric",month:"short",day:"numeric",weekday:"short"}).format(new Date(session.start_at)) : "Date unknown"}</strong>
                    <small>{session.start_at ? formatEventTime(session.start_at,session.end_at) : ""}{session.is_past ? " · Past event" : ""}{session.node_id === subject.node_id ? " · Selected" : ""}</small>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        ) : null}

        {subject.topics.length ? (
          <ul className="entity-activity-lines">
            {subject.topics.map((topic) => <li key={topic}>{eventTopicLabel(topic)}</li>)}
          </ul>
        ) : null}

        {/*
          * The provenance is said once when it is the same for everyone.
          *
          * Every row carried "asserted by <source> · seen <date>", and on a single event those are
          * almost always one source and one date — so a three-name card printed the same sentence
          * three times, wrapped it onto two lines each, and buried the three names it exists to
          * show. It is hoisted when it is shared and kept per-row when it genuinely differs, so
          * nothing is dropped in either case.
          */}
        <h3>Who this event names ({named.length})</h3>
        {sharedProvenance ? (
          <p className="entity-graph-inspector__provenance">{sharedProvenance}</p>
        ) : null}
        <ul className="entity-graph-inspector__mentions">
          {named.map((edge) => {
            const entity = model.byId.get(edge.a);
            const roles = edge.roles.map(roleLabel).join(" · ") || "Named";
            return (
              <li key={`${edge.a} ${edge.b}`}>
                <button
                  type="button"
                  onMouseEnter={() => onHoverNode(edge.a)}
                  onMouseLeave={() => onHoverNode(null)}
                  onClick={() => (entity?.entity_id
                    ? onFocusEntity(entity.entity_id)
                    : onSelectNode(edge.a))}
                >
                  <strong>{entity?.label ?? edge.a}</strong>
                  <small>
                    {roles}
                    {sharedProvenance ? "" : edgeProvenance(edge)}
                  </small>
                </button>
              </li>
            );
          })}
        </ul>

        {subject.registration_url ? (
          <a
            className="entity-graph-inspector__cta"
            href={subject.registration_url}
            target="_blank"
            rel="noopener noreferrer"
          >
            View event <ArrowUpRight aria-hidden="true" />
          </a>
        ) : null}
      </aside>
    );
  }

  if (subject.node_kind === "topic") {
    const eventTotal = textModel.upcoming.length + textModel.past.length;
    return (
      <aside className="entity-graph-inspector" aria-label="Topic detail">
        <header className="entity-graph-inspector__head">
          <span className="entity-hero__icon">{nodeGlyph(subject)}</span>
          <div>
            <p>{kindLabel(subject)}</p>
            <h2>{eventTopicLabel(subject.label)}</h2>
            <span>{subject.degree} of the {eventTotal} shown events</span>
          </div>
          <button
            type="button"
            className="entity-graph-inspector__close"
            onClick={() => onSelectNode(null)}
          >
            <X aria-hidden="true" />
            <span className="sr-only">Back to {egoLabel}</span>
          </button>
        </header>
        <p className="entity-empty-copy">
          Topics come from the catalog&rsquo;s own closed vocabulary, applied to the events drawn in
          this frame. They are a property of those events, not a claim about what this entity is.
        </p>
      </aside>
    );
  }

  const isEgo = subject.node_id === model.focusId;
  /*
   * `same_name_candidates` is computed from the EGO's normalized name and nothing else — the
   * capability derives it from `v_ego.normalized_name`.  Rendering it under a peer would present
   * the ego's namesakes as the peer's, and its empty case would assert "no other catalog row
   * carries this exact display name" about a peer nobody checked.  Both are name-as-identity
   * claims, in the one panel binding rule (b) governs most directly.  There is no per-peer
   * namesake list to fetch instead, and asking for one would widen a name into a join key.
   */
  const tabs = isEgo ? TABS : TABS.filter((entry) => entry.value !== "same-name");

  /*
   * Reading logic lives in `entity-inspector-model.ts`, and all four values below are plain
   * derivations rather than `useMemo`s: they run only on the entity branch, which is below the
   * early returns above, and a hook cannot live there.  Each is a walk of at most a few dozen rows.
   */

  /**
   * The frame speaks for the record only for the ego, and only when it was not truncated.
   *
   * A peer's `appearances` are the events it SHARES with the ego, not its own record, so counting
   * them as its schedule would understate every peer.  A truncated frame knows what it drew, not
   * what exists.  `entityFrameActivity` returns null in the truncated case; the peer case is
   * excluded here.
   */
  const frameActivity = isEgo
    ? entityFrameActivity(appearances, textModel.truncated.events)
    : null;
  /** Catalog-wide insights when the detail payload is in hand, the frame otherwise, nothing if neither. */
  const overviewLines = entityOverviewLines(detail?.insights ?? null, frameActivity);
  const appearanceGroups = groupEntityAppearances(appearances);
  const sourceGroups = groupEntitySources<CatalogEntityExternalSource>(
    (detail?.external_sources ?? []).filter((source) => !SOCIAL_API_PROVIDERS.has(source.provider_key)),
  );
  /** Catalog-wide top topics when known; the drawn frame's topics are an ego-only fallback. */
  const topicLabels = detail?.insights?.top_topics?.length
    ? detail.insights.top_topics
    : isEgo
      ? textModel.topics.map((topic) => topic.label)
      : [];
  const subjectNoun = subject.entity_kind === "organization" ? "organization" : "person";

  const renderSource = (source: CatalogEntityExternalSource, presentation: EntitySourcePresentation) => (
    <li key={source.provider_key} data-status={source.status}>
      {SOURCE_GLYPHS[presentation.glyph]}
      <div>
        <strong>{source.display_name}</strong>
        <span>{presentation.label}</span>
        <small>
          {source.fetched_at
            ? `Updated ${observedLabel(source.fetched_at)}`
            : "Exact public profile"}
        </small>
      </div>
      <a
        href={source.source_url}
        target="_blank"
        rel="noopener noreferrer"
        aria-label={`Open ${source.display_name} on ${presentation.label}`}
      >
        <ArrowUpRight aria-hidden="true" />
      </a>
    </li>
  );

  return (
    <aside className="entity-graph-inspector" aria-label="Entity detail">
      <header className="entity-graph-inspector__head">
        <span className="entity-hero__icon">{nodeGlyph(subject)}</span>
        <div>
          <p>{kindLabel(subject)}</p>
          <h2>{subject.label}</h2>
          <span>
            {subject.degree} {subject.degree === 1 ? "event" : "events"}
            {subject.shared_event_count !== null
              ? ` · ${subject.shared_event_count} shared with ${egoLabel}`
              : ""}
          </span>
        </div>
        {isEgo ? null : (
          <button
            type="button"
            className="entity-graph-inspector__close"
            onClick={() => onSelectNode(null)}
          >
            <X aria-hidden="true" />
            <span className="sr-only">Back to {egoLabel}</span>
          </button>
        )}
      </header>

      {isEgo || !subject.entity_id ? null : (
        <button
          type="button"
          className="entity-graph-inspector__refocus"
          onClick={() => subject.entity_id && onFocusEntity(subject.entity_id)}
        >
          Centre the graph on {subject.label}
        </button>
      )}

      <div className="entity-graph-tabs" role="tablist" aria-label="Entity detail sections">
        {tabs.map((entry) => (
          <button
            key={entry.value}
            type="button"
            role="tab"
            id={`entity-tab-${entry.value}`}
            aria-selected={tab === entry.value}
            aria-controls={`entity-panel-${entry.value}`}
            data-state={tab === entry.value ? "active" : "idle"}
            onClick={() => setTab(entry.value)}
          >
            {entry.label}
            {entry.value === "same-name" && textModel.same_name_candidates.length
              ? <small>{textModel.same_name_candidates.length}</small>
              : null}
          </button>
        ))}
      </div>

      {tab === "overview" ? (
        <div
          className="entity-graph-panel"
          role="tabpanel"
          id="entity-panel-overview"
          aria-labelledby="entity-tab-overview"
        >
          {/*
            The header two lines above already states the event count, so it is not restated here:
            the old "N events in the catalog" row was the largest thing on this panel and carried no
            information the reader did not have.  Activity leads instead — what is coming, how often,
            how big, what it costs, and where — and every line is derived, never invented.  When the
            payload carries nothing to say, the list renders nothing rather than a row of zeroes.
          */}
          <dl className="entity-activity-grid entity-insight-grid">
            {overviewLines.map((line) => (
              <div key={line.key}>
                <dt>{line.label}</dt>
                <dd>{line.value}</dd>
              </div>
            ))}
            {subject.roles.length ? (
              <div>
                <dt>Named as</dt>
                <dd>{subject.roles.map(roleLabel).join(" · ")}</dd>
              </div>
            ) : null}
            {topicLabels.length ? (
              <div>
                <dt>Usual topics</dt>
                <dd>{topicLabels.map((topic) => eventTopicLabel(topic)).join(" · ")}</dd>
              </div>
            ) : null}
            <div className="entity-insight-grid__identity">
              <dt>Identity</dt>
              <dd>
                {egoIdentityLinks.length ? (
                  <>
                    {/*
                      The link itself, not a sentence about it. This said "A direct profile URL is
                      on file" — a statement about the existence of a link, printed in the room the
                      link would have taken, and telling a reader nothing they could go and check.
                      The caveat below keeps its force: holding a URL a source published is a record
                      of an assertion, and nothing here goes looking for a profile from a name.
                    */}
                    <EntityIdentityLinks links={egoIdentityLinks} />
                    <span className="entity-insight-grid__caveat">
                      Attached by a catalog source. Holding it is not a verification that it belongs
                      to this {subjectNoun}.
                    </span>
                  </>
                ) : (
                  <>
                    No source attached a direct profile URL, so this record stays scoped to the
                    source that named it.
                  </>
                )}
              </dd>
            </div>
          </dl>

          {isEgo ? (
            <div className="entity-collaborators">
              {/*
                "Shares events with", not "Recurring together": the collaborator floor is one shared
                event, and a panel headed "Recurring" filled with single-event pairs would be false.
              */}
              <h3>Shares events with</h3>
              {textModel.peers.length ? (
                <ul>
                  {textModel.peers.map((peer) => (
                    <li key={peer.node_id}>
                      <button
                        type="button"
                        onMouseEnter={() => onHoverNode(peer.node_id)}
                        onMouseLeave={() => onHoverNode(null)}
                        onClick={() => onSelectNode(peer.node_id)}
                      >
                        {peer.display_name}
                        <small>{peer.shared_event_count} shared</small>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : (
                <p className="entity-empty-copy">No one else is named on these events yet.</p>
              )}
            </div>
          ) : null}
        </div>
      ) : null}

      {tab === "appearances" ? (
        <div
          className="entity-graph-panel"
          role="tabpanel"
          id="entity-panel-appearances"
          aria-labelledby="entity-tab-appearances"
        >
          {/*
            Three things were wrong with the old list and each is fixed structurally rather than by
            restyling.  Every row showed a clock time and no date, so a list spanning months could
            not be read — the date is now the row's second line, with the year added only when it is
            not this one.  Upcoming and past were interleaved — they are now labelled groups, soonest
            first ahead and most recent first behind.  And the loudest text on each card was
            "seen <date>", identical on every row because it is the observation date, not the
            event's: it is gone, and role plus asserting source are demoted to one quiet line.
          */}
          {appearanceGroups.length ? (
            appearanceGroups.map((group) => (
              <section key={group.key} className="entity-appearance-group">
                <h3>
                  {group.label} <small>{group.rows.length}</small>
                </h3>
                <ul className="entity-graph-inspector__appearances">
                  {group.rows.map((row) => {
                    const place = [row.venue_name, formatCity(row.city)]
                      .filter(Boolean)
                      .join(" · ");
                    return (
                      <li key={row.node_id} data-past={row.is_past ? "true" : undefined}>
                        <button
                          type="button"
                          onMouseEnter={() => onHoverNode(row.node_id)}
                          onMouseLeave={() => onHoverNode(null)}
                          onClick={() => onSelectNode(row.node_id)}
                        >
                          <strong>{row.title}</strong>
                          <span className="entity-appearance__when">
                            {appearanceDateLabel(row.start_at)}
                          </span>
                          {place ? <span>{place}</span> : null}
                          <small>{appearanceProvenance(row)}</small>
                        </button>
                      </li>
                    );
                  })}
                </ul>
              </section>
            ))
          ) : (
            <p className="entity-empty-copy">No appearances in this frame.</p>
          )}
        </div>
      ) : null}

      {tab === "profile" ? (
        <div
          className="entity-graph-panel"
          role="tabpanel"
          id="entity-panel-profile"
          aria-labelledby="entity-tab-profile"
        >
          <section className="entity-graph-identity">
            <h3>Identity</h3>
            {/*
              Every public identity on file, gathered here rather than on the Overview because this
              is the tab whose open triggers the detail fetch: `external_facts` and
              `external_sources` do not exist until the reader is standing here. It leads the
              section so the social rows the enrichment plane captures are reachable as links, not
              only as entries further down the source list.
            */}
            <EntityIdentityLinks links={identityProfileLinks} />
            {subject.profile_url ? (
              <>
                <a href={subject.profile_url} target="_blank" rel="noopener noreferrer">
                  {subject.profile_url} <ArrowUpRight aria-hidden="true" />
                </a>
                {/*
                  Stated plainly rather than implied: holding a URL is a record of what a source
                  asserted.  It is not a verification of the person, and nothing here offers to go
                  looking for a profile from the name — a name is display data, not an identity.
                */}
                <p className="entity-empty-copy">
                  This is the profile URL a catalog source attached to this name. Holding it is not
                  a verification that the profile belongs to this person or organization.
                </p>
              </>
            ) : (
              <p className="entity-empty-copy">
                No source attached a direct profile URL to this name, so this record stays scoped to
                the source that named it.
              </p>
            )}
          </section>

          {(() => {
            const description = firstFact(detail, "description");
            const values = PROFILE_FACTS.flatMap(([key, label]) => {
              const fact = firstFact(detail, key);
              return fact ? [{ key, label, fact }] : [];
            });
            if (!description && values.length === 0) return null;
            return (
              <section className="entity-graph-facts">
                <h3>Public data</h3>
                {description ? <p>{description.value}</p> : null}
                {values.length ? (
                  <dl>
                    {values.map(({ key, label, fact }) => (
                      <div key={key}>
                        <dt>{label}</dt>
                        <dd>
                          {/* Provenance next to the value, never a bare claim. */}
                          {fact.value_url ? (
                            <a href={fact.value_url} target="_blank" rel="noopener noreferrer">
                              {fact.value} <ArrowUpRight aria-hidden="true" />
                            </a>
                          ) : (
                            fact.value
                          )}
                        </dd>
                      </div>
                    ))}
                  </dl>
                ) : null}
              </section>
            );
          })()}

          {socialProfileCards(detail).map(({ source, description, followers, avatar }) => (
            <section className="entity-social-profile" key={source.provider_key} aria-label={source.display_name}>
              <header>
                <SocialAvatar url={avatar} />
                <div>
                  <h3><a href={source.source_url} target="_blank" rel="noopener noreferrer">{source.display_name}</a></h3>
                  <small>Updated {observedLabel(source.fetched_at!)}{source.status !== "fresh" ? " · Last successful snapshot" : ""}</small>
                </div>
              </header>
              {description ? <p>{description}</p> : null}
              {followers !== null ? <p>{followers} followers</p> : null}
              {source.status !== "fresh" ? <small>Refresh unavailable; saved facts are shown.</small> : null}
            </section>
          ))}

          <section>
            <div className="entity-graph-sources__heading">
              <h3>Connected public sources</h3>
              {canRefresh && subject.identity_status === "profile_verified" ? (
                <button
                  type="button"
                  className="entity-graph-refresh"
                  onClick={() => void refreshSources()}
                  disabled={refreshing}
                >
                  {refreshing ? (
                    <><LoaderCircle className="spin" aria-hidden="true" />Syncing public data</>
                  ) : (
                    <><RefreshCw aria-hidden="true" />Refresh</>
                  )}
                </button>
              ) : null}
            </div>
            {detailLoading ? (
              <p className="entity-loading">
                <LoaderCircle className="spin" />Loading public sources
              </p>
            ) : null}
            {detailError ? <p className="workspace-error" role="alert">{detailError}</p> : null}
            {/*
              Two groups, because they are two different kinds of claim.  A social or professional
              profile is identity-bearing — it says "this is who they are" — while a website or a
              repository host is a place the record points at.  Reading them as one list let the
              weaker claim borrow the stronger one's weight.
              Provider keys are rendered from the string, not from a closed switch, so the social
              keys another agent is adding to the CHECK light up the moment a row exists.
            */}
            {sourceGroups.profiles.length ? (
              <div className="entity-source-group" data-group="profile">
                <h4>Profiles a source published</h4>
                <p className="entity-source-group__caveat">
                  These are profile URLs a catalog source attached to this name. Holding one is not
                  a verification that it belongs to this {subjectNoun}.
                </p>
                <ul className="entity-source-list">
                  {sourceGroups.profiles.map((source) =>
                    renderSource(source, entitySourcePresentation(source.provider_key)))}
                </ul>
              </div>
            ) : null}
            {sourceGroups.public.length ? (
              <div className="entity-source-group" data-group="public">
                {sourceGroups.profiles.length ? <h4>Sites and public records</h4> : null}
                <ul className="entity-source-list">
                  {sourceGroups.public.map((source) =>
                    renderSource(source, entitySourcePresentation(source.provider_key)))}
                </ul>
              </div>
            ) : null}
            {!sourceGroups.profiles.length && !sourceGroups.public.length
              && !detailLoading && !detailError ? (
              <p className="entity-empty-copy">
                {subject.identity_status === "profile_verified"
                  ? "No external source has been connected for this profile yet."
                  : "A direct public profile is required before external data can be connected."}
              </p>
            ) : null}
          </section>
        </div>
      ) : null}

      {tab === "same-name" && isEgo ? (
        <div
          className="entity-graph-panel"
          role="tabpanel"
          id="entity-panel-same-name"
          aria-labelledby="entity-tab-same-name"
        >
          {/*
            Review candidates, and nothing else.  There is no merge action here and no write path
            anywhere in this feature: an identical display name is not evidence of an identical
            entity, and a button that acted on it would make a name into an identity judgment.
          */}
          <p className="entity-graph-samename__caveat">
            Same name — not merged. These rows share an exact display name. That is not evidence
            they are the same person or organization; compare them yourself.
          </p>
          {textModel.same_name_candidates.length ? (
            <ul className="entity-graph-samename">
              {textModel.same_name_candidates.map((candidate) => (
                <li key={candidate.entity_id}>
                  <button type="button" onClick={() => onFocusEntity(candidate.entity_id)}>
                    <strong>{candidate.display_name}</strong>
                    <span>
                      {candidate.kind}
                      {" · "}
                      {candidate.identity_status === "profile_verified"
                        ? "direct profile on file"
                        : "source-scoped"}
                      {" · "}
                      {candidate.event_count} {candidate.event_count === 1 ? "event" : "events"}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="entity-empty-copy">
              No other catalog row carries this exact display name.
            </p>
          )}
        </div>
      ) : null}
    </aside>
  );
}
