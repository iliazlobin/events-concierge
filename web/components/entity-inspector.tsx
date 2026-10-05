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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { getCatalogEntity, refreshCatalogEntity } from "@/lib/api";
import type {
  CatalogEntityGraphNode,
  EntityGraphSceneModel,
  EntityGraphDetailModel,
} from "@/lib/entity-graph";
import { deriveEntityAppearances } from "@/lib/entity-graph";
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
import { GraphEventInspector } from "@/components/graph-event-inspector";
import { EntityIdentityLinks } from "@/components/entity-identity-links";
import { identityLinkKey, identityLinks } from "@/lib/entity-identity-links";
import { eventTopicLabel } from "@/lib/event-topics";
import { formatCity } from "@/lib/presentation";
import type {
  CatalogEntityDetail,
  CatalogEntityExternalFact,
  CatalogEntityExternalSource,
  EventEntityReference,
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
  scope?: "catalog" | "entity";
  /** The original occurrence scene, retained beneath the canvas session grouping. */
  model: EntityGraphSceneModel;
  detailModel: EntityGraphDetailModel;
  /** The node under inspection; `null` reads the ego. */
  eventSessions?: CatalogEntityGraphNode[];
  selectedNodeId: string | null;
  onSelectNode: (nodeId: string | null) => void;
  onFocusEntity: (entityId: string) => void;
  onHoverNode: (nodeId: string | null) => void;
  onEntitySelect: (reference: EventEntityReference) => void;
  onTopicSelect: (topic: string) => void;
}

export function EntityInspector({
  tenantId,
  canRefresh = false,
  scope = "entity",
  model,
  detailModel,
  selectedNodeId,
  eventSessions,
  onSelectNode,
  onFocusEntity,
  onHoverNode,
  onEntitySelect,
  onTopicSelect,
}: EntityInspectorProps) {
  const subject = (selectedNodeId ? model.byId.get(selectedNodeId) : undefined)
    ?? (scope === "entity" ? model.ego : null)
    ?? null;

  const subjectNodeId = subject?.node_id ?? null;
  const subjectEntityId = subject?.node_kind === "entity" ? subject.entity_id : null;
  const identity = `${tenantId ?? "anonymous"}:${subjectEntityId ?? ""}`;
  const currentIdentity = useRef(identity);
  currentIdentity.current = identity;
  const [tab, setTab] = useState<InspectorTab>("overview");
  const [detailEntry, setDetailEntry] = useState<{ tenantId: string | null; item: CatalogEntityDetail } | null>(null);
  const detail = detailEntry?.tenantId === tenantId && detailEntry.item.entity.entity_id === subjectEntityId
    ? detailEntry.item : null;
  const setDetail = useCallback((item: CatalogEntityDetail | null) => {
    setDetailEntry(item ? { tenantId, item } : null);
  }, [tenantId]);
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
    setDetailLoading(false);
    setRefreshing(false);
    if (!subjectEntityId) {
      setDetail(null);
      setDetailError(null);
      return;
    }
    setDetail(readEntityDetail(tenantId, subjectEntityId));
    setDetailError(null);
  }, [subjectEntityId, tenantId, setDetail]);

  useEffect(() => {
    if (profileOpenedFor !== subjectNodeId || !subjectEntityId) return;
    const cached = readEntityDetail(tenantId, subjectEntityId);
    if (cached) {
      setDetailLoading(false);
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
  }, [profileOpenedFor, subjectEntityId, subjectNodeId, tenantId, setDetail]);

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
      if (currentIdentity.current === identity) setDetail(item);
    } catch (caught: unknown) {
      if (currentIdentity.current !== identity) return;
      setDetailError(
        caught instanceof Error ? caught.message : "Public sources could not be refreshed.",
      );
    } finally {
      if (currentIdentity.current === identity) setRefreshing(false);
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

  const appearances = useMemo(() => (
    subject?.node_kind === "entity" ? deriveEntityAppearances(model, subject.node_id) : []
  ), [model, subject]);

  const egoLabel = model.ego?.label ?? "the graph";
  const closeLabel = scope === "catalog" ? "Close details" : `Back to ${egoLabel}`;

  /** Combine the catalog identity, profile facts and imported social links. */
  const identityProfileLinks = useMemo(() => {
    if (!subject || subject.node_kind !== "entity") return [];
    return identityLinks(
      subject.profile_url,
      (detail?.external_facts ?? []).filter((fact) => fact.fact_key === "profile").map((fact) => fact.value_url),
      groupEntitySources((detail?.external_sources ?? []).filter(
        (source) => !SOCIAL_API_PROVIDERS.has(source.provider_key),
      )).profiles.map((source) => source.source_url),
    );
  }, [detail, subject]);

  if (!subject) return null;

  if (subject.node_kind === "event") {
    return <GraphEventInspector
      key={`${tenantId ?? ""}:${subject.canonical_event_id ?? subject.node_id}`}
      tenantId={tenantId}
      subject={subject}
      model={model}
      eventSessions={eventSessions}
      contextNote={scope === "catalog" && subject.shared_event_count !== null
        ? `Representative of ${subject.shared_event_count.toLocaleString()} shared ${subject.shared_event_count === 1 ? "event" : "events"}.`
        : undefined}
      onSelectNode={onSelectNode}
      onHoverNode={onHoverNode}
      onFocusEntity={onFocusEntity}
      onEntitySelect={onEntitySelect}
      onTopicSelect={onTopicSelect}
    />;
  }

  if (subject.node_kind === "topic") {
    const eventTotal = model.events.length;
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
            <span className="sr-only">{closeLabel}</span>
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
    ? entityFrameActivity(appearances, detailModel.truncated.events)
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
      ? detailModel.topics.map((topic) => topic.label)
      : [];
  const snapshots = socialProfileCards(detail);
  const snapshotKeys = new Set(identityLinks(null, [], snapshots.map(({ source }) => source.source_url))
    .map(identityLinkKey));
  const profileLinks = identityProfileLinks.filter((link) => !snapshotKeys.has(identityLinkKey(link)));
  const profileKeys = new Set(identityProfileLinks.map(identityLinkKey));
  const publicSources = sourceGroups.public.filter((source) => {
    const link = identityLinks(null, [], [source.source_url])[0];
    return !link || !profileKeys.has(identityLinkKey(link));
  });

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
            {scope !== "catalog" && subject.shared_event_count !== null
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
            <span className="sr-only">{closeLabel}</span>
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
        {TABS.map((entry) => (
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
          </dl>

          {isEgo ? (
            <div className="entity-collaborators">
              {/*
                "Shares events with", not "Recurring together": the collaborator floor is one shared
                event, and a panel headed "Recurring" filled with single-event pairs would be false.
              */}
              <h3>Shares events with</h3>
              {detailModel.peers.length ? (
                <ul>
                  {detailModel.peers.map((peer) => (
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
          {scope === "catalog" ? (
            <p className="entity-graph-inspector__provenance">
              Representative appearances in this graph. Open the entity&rsquo;s graph for its event history.
            </p>
          ) : null}
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
          {profileLinks.length ? (
            <section className="entity-graph-identity">
              <h3>Profiles</h3>
              <EntityIdentityLinks links={profileLinks} showHandles />
            </section>
          ) : null}
          {detailLoading ? <p className="entity-loading"><LoaderCircle className="spin" />Loading profiles</p> : null}
          {detailError ? <p className="workspace-error" role="alert">{detailError}</p> : null}

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

          {snapshots.map(({ source, description, followers, avatar }) => (
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

          {publicSources.length || (canRefresh && subject.identity_status === "profile_verified") ? (
            <section>
              <div className="entity-graph-sources__heading">
                <h3>Sources</h3>
                {canRefresh && subject.identity_status === "profile_verified" ? (
                  <button type="button" className="entity-graph-refresh"
                    onClick={() => void refreshSources()} disabled={refreshing}>
                    {refreshing ? <><LoaderCircle className="spin" aria-hidden="true" />Refreshing</>
                      : <><RefreshCw aria-hidden="true" />Refresh</>}
                  </button>
                ) : null}
              </div>
              {publicSources.length ? (
                <ul className="entity-source-list">
                  {publicSources.map((source) => renderSource(source, entitySourcePresentation(source.provider_key)))}
                </ul>
              ) : null}
            </section>
          ) : null}
          {!identityProfileLinks.length && !snapshots.length && !publicSources.length
            && !detailLoading && !detailError ? <p className="entity-empty-copy">No public profiles available.</p> : null}
        </div>
      ) : null}

    </aside>
  );
}
