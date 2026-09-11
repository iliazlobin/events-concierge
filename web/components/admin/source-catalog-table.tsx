"use client";

import {
  ArrowUpRight,
  CalendarPlus,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  Copy,
  Info,
  LoaderCircle,
  MapPin,
  Search,
  X,
} from "lucide-react";
import { Fragment, type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { getAdminCatalogEvents } from "@/lib/admin-api";
import {
  canonicalReadiness,
  catalogEnrichmentSummary,
  catalogMetadataGroups,
  READINESS_FIELDS,
} from "@/lib/admin-catalog-coverage";
import {
  formatDateTime,
  formatNumber,
  safeHttpUrl,
  shortRevision,
} from "@/lib/admin-presentation";
import type {
  AdminCatalogEvent,
  AdminCatalogEventCursor,
  AdminCatalogListingPage,
  AdminRunFilters,
} from "@/lib/admin-types";

import { CatalogInsights } from "./catalog-insights";
import styles from "./source-catalog-table.module.css";

interface SourceCatalogTableProps {
  sourceKey: string;
  runKey?: string;
  dateScope: AdminCatalogListingPage["date_scope"];
  priceScope: AdminCatalogListingPage["price_status"];
  filters?: ReactNode;
  onOpenRuns: (filters: AdminRunFilters) => void;
  refreshToken: string | null;
  investigation?: SourceCatalogInvestigation;
}

export interface SourceCatalogInvestigationChange {
  query?: string;
  cursor?: AdminCatalogEventCursor | null;
  eventId?: string | null;
}

export interface SourceCatalogInvestigation {
  query: string;
  cursor: AdminCatalogEventCursor | null;
  eventId: string | null;
  onChange: (changes: SourceCatalogInvestigationChange) => void;
  onOpenRun?: (sourceKey: string, runKey: string) => void;
  onOpenSource?: (sourceKey: string) => void;
}

const PAGE_SIZE = 20;

function readableError(error: unknown): string {
  return error instanceof Error ? error.message : "Could not load parsed catalog output.";
}

function compactText(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function locationText(event: AdminCatalogEvent): string {
  return [event.venue_name, event.city].filter(Boolean).join(" · ") || "No parsed location";
}

function geoText(event: AdminCatalogEvent): string {
  if (event.latitude === null || event.longitude === null) return "No coordinates";
  return `${event.latitude.toFixed(4)}, ${event.longitude.toFixed(4)}`;
}

function cursorFrom(page: AdminCatalogListingPage): AdminCatalogEventCursor | null {
  if (!page.next_start_at || !page.next_canonical_event_id) return null;
  return {
    startAt: page.next_start_at,
    canonicalEventId: page.next_canonical_event_id,
  };
}

function googleDate(value: string): string {
  return new Date(value).toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
}

function googleCalendarUrl(event: AdminCatalogEvent): string {
  const start = new Date(event.start_at);
  const fallbackEnd = new Date(start.getTime() + 60 * 60 * 1_000);
  const end = event.end_at ? new Date(event.end_at) : fallbackEnd;
  const query = new URLSearchParams({
    action: "TEMPLATE",
    text: event.title,
    dates: `${googleDate(start.toISOString())}/${googleDate(end.toISOString())}`,
  });
  const location = locationText(event);
  if (location !== "No parsed location") query.set("location", location);
  const details = compactText(event.description);
  const sourceUrl = event.registration_url ? safeHttpUrl(event.registration_url) : null;
  query.set(
    "details",
    [details || null, sourceUrl ? `Event page: ${sourceUrl}` : null]
      .filter(Boolean)
      .join("\n\n"),
  );
  return `https://calendar.google.com/calendar/render?${query.toString()}`;
}

function googleMapsUrl(event: AdminCatalogEvent): string | null {
  const query = event.latitude !== null && event.longitude !== null
    ? `${event.latitude},${event.longitude}`
    : [event.venue_name, event.city].filter(Boolean).join(", ");
  if (!query) return null;
  return `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(query)}`;
}

export function SourceCatalogTable({
  sourceKey,
  runKey,
  dateScope,
  priceScope,
  filters,
  onOpenRuns,
  refreshToken,
  investigation,
}: SourceCatalogTableProps) {
  const [local, setLocal] = useState({ sourceKey, query: "", cursor: null as AdminCatalogEventCursor | null, eventId: null as string | null });
  const localState = local.sourceKey === sourceKey ? local : { sourceKey, query: "", cursor: null, eventId: null };
  const state = investigation ?? localState;
  const settledQuery = state.query.trim();
  const expandedEventId = state.eventId;
  const cursorStart = state.cursor?.startAt ?? null;
  const cursorId = state.cursor?.canonicalEventId ?? null;
  const cursor = useMemo(() => cursorStart && cursorId ? { startAt: cursorStart, canonicalEventId: cursorId } : null, [cursorStart, cursorId]);
  const historyScope = JSON.stringify([sourceKey, runKey, dateScope, priceScope, settledQuery]);
  const draftKey = JSON.stringify([historyScope, cursorStart, cursorId, expandedEventId]);
  const [draft, setDraft] = useState<{ key: string; value: string } | null>(null);
  const draftVersion = useRef(0);
  const activeDraftKey = useRef(draftKey);
  activeDraftKey.current = draftKey;
  const query = draft?.key === draftKey ? draft.value : state.query;
  const searchPending = query.trim() !== settledQuery;
  const onChange = useRef(investigation?.onChange);
  onChange.current = investigation?.onChange;
  const update = useCallback((changes: SourceCatalogInvestigationChange) => {
    if (onChange.current) onChange.current(changes);
    else setLocal(current => ({ ...(current.sourceKey === sourceKey ? current : { sourceKey, query: "", cursor: null, eventId: null }), ...changes }));
  }, [sourceKey]);
  const setQuery = (value: string) => {
    draftVersion.current += 1;
    setDraft({ key: draftKey, value });
  };
  const setExpandedEventId = (eventId: string | null) => update({ eventId });
  const [cursorHistory, setCursorHistory] = useState<{ scope: string; entries: Array<{ from: AdminCatalogEventCursor | null; to: AdminCatalogEventCursor }> }>({ scope: "", entries: [] });
  const [result, setResult] = useState<{ key: string; page: AdminCatalogListingPage | null; loading: boolean; error: string | null }>({ key: "", page: null, loading: true, error: null });
  const [reloadNonce, setReloadNonce] = useState(0);
  const [copyStatus, setCopyStatus] = useState<{ key: string; message: string } | null>(null);
  const requestKey = JSON.stringify([sourceKey, runKey, dateScope, priceScope, settledQuery, cursorStart, cursorId, refreshToken, reloadNonce]);
  // Bind visible rows and errors to the exact request before effects run. A new source,
  // search, cursor or refresh must never display the previous request's rows as its result.
  const currentResult = !searchPending && result.key === requestKey ? result : null;
  const page = currentResult?.page ?? null;
  const loading = searchPending || !currentResult || currentResult.loading;
  const error = currentResult?.error ?? null;

  useEffect(() => {
    // Retire drafts when controlled navigation changes the investigation. They must
    // not become active again if Back later restores the draft's original scope.
    setDraft(current => current && current.key !== draftKey ? null : current);
  }, [draftKey]);

  const controlled = Boolean(investigation);
  useEffect(() => {
    if (!controlled) return;
    const discardDraft = () => {
      draftVersion.current += 1;
      setDraft(null);
    };
    window.addEventListener("popstate", discardDraft);
    return () => window.removeEventListener("popstate", discardDraft);
  }, [controlled]);

  useEffect(() => {
    // Only typing starts a new search. Initial URL state and browser navigation preserve
    // their cursor and selected event instead of being cleared by a mount effect.
    if (!searchPending) return;
    const version = draftVersion.current;
    const timer = window.setTimeout(() => {
      if (version !== draftVersion.current || activeDraftKey.current !== draftKey) return;
      update({ query: query.trim(), cursor: null, eventId: null });
      // The parent commits controlled state synchronously. React batches retirement
      // with that update, so the old query is never revived between the two.
      setDraft(current => current?.key === draftKey && current.value === query ? null : current);
    }, 240);
    return () => window.clearTimeout(timer);
  }, [query, searchPending, draftKey, update]);

  useEffect(() => {
    if (searchPending) return;
    let cancelled = false;
    const controller = new AbortController();
    setResult({ key: requestKey, page: null, loading: true, error: null });
    void getAdminCatalogEvents(sourceKey, settledQuery, cursor, dateScope, priceScope, PAGE_SIZE, controller.signal, runKey)
      .then((next) => {
        if (cancelled) return;
        if ((next.source_key ?? "") !== sourceKey || (next.run_key ?? "") !== (runKey ?? "")
          || next.date_scope !== dateScope || next.price_status !== priceScope || (next.query ?? "") !== settledQuery
          || next.items.some(event => !event.source_key || (sourceKey && event.source_key !== sourceKey) || (runKey && event.refresh_run_key !== runKey))) {
          throw new Error("The returned events do not match the selected catalog filters. Refresh to retry.");
        }
        setResult({ key: requestKey, page: next, loading: false, error: null });
      })
      .catch((nextError) => {
        if (!cancelled) setResult({ key: requestKey, page: null, loading: false, error: readableError(nextError) });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [cursor, requestKey, searchPending, settledQuery, sourceKey, runKey, dateScope, priceScope]);

  const nextCursor = useMemo(() => (page ? cursorFrom(page) : null), [page]);
  const history = cursorHistory.scope === historyScope ? cursorHistory.entries : [];
  const historyIndex = cursor ? history.findLastIndex(entry => entry.to.startAt === cursor.startAt && entry.to.canonicalEventId === cursor.canonicalEventId) : -1;
  const pageNumber = !cursor ? 1 : historyIndex >= 0 && history[0]?.from === null ? historyIndex + 2 : null;
  const missingEvent = !loading && page && expandedEventId && !page.items.some(event => event.canonical_event_id === expandedEventId);

  const showNext = () => {
    if (!nextCursor || loading) return;
    setCursorHistory({ scope: historyScope, entries: [...history.slice(0, historyIndex + 1), { from: cursor, to: nextCursor }] });
    update({ cursor: nextCursor, eventId: null });
  };

  const showPrevious = () => {
    if (!cursor || loading) return;
    update({ cursor: historyIndex >= 0 ? history[historyIndex].from : null, eventId: null });
  };

  const copyIdentifier = async (event: AdminCatalogEvent, run = false) => {
    const key = `${sourceKey}:${event.canonical_event_id}`;
    try {
      await navigator.clipboard.writeText(run ? event.refresh_run_key : event.canonical_event_id);
      setCopyStatus({ key, message: run ? "Run key copied." : "Event ID copied." });
    } catch {
      setCopyStatus({ key, message: "Copy unavailable. Select the identifier below to copy it." });
    }
  };

  return (
    <section className={`admin-detail-section admin-catalog-section ${styles.section}`}>
      <CatalogInsights page={page} sourceKey={sourceKey} refreshToken={refreshToken} onOpenRuns={onOpenRuns} onOpenSource={investigation?.onOpenSource} />
      <div className="admin-catalog-toolbar">
        <label className="admin-catalog-search">
          <Search aria-hidden="true" />
          <span className="sr-only">Search catalog events</span>
          <input
            aria-label="Search catalog events"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Search title, venue, city, organizer, host, speaker…"
            maxLength={160}
          />
          {query ? (
            <button type="button" aria-label="Clear event search" onClick={() => setQuery("")}>
              <X aria-hidden="true" />
            </button>
          ) : null}
        </label>
        {filters}
      </div>

      {error ? (
        <div className="admin-inline-error admin-catalog-error" role="alert">
          <CircleAlert aria-hidden="true" />
          <span>{error}</span>
          <button type="button" onClick={() => setReloadNonce((value) => value + 1)}>
            Retry
          </button>
        </div>
      ) : null}

      {missingEvent ? (
        <div className="admin-inline-error admin-catalog-error" role="status">
          <Info aria-hidden="true" />
          <span>
            Selected event <code>{expandedEventId}</code> is not in this loaded page.
            It may be on another page or outside the current filters.
          </span>
          <button type="button" onClick={() => setExpandedEventId(null)}>Clear selection</button>
        </div>
      ) : null}

      <div className={`admin-catalog-table-shell${loading ? " is-loading" : ""}`} aria-busy={loading}>
        {page?.items.length ? (
          <div className="admin-table-scroll admin-catalog-table-scroll">
            <table className="admin-table admin-table--catalog">
              <thead>
                <tr>
                  <th>Published event</th>
                  <th>Schedule</th>
                  <th>Parsed location</th>
                  <th>
                    <span className="admin-coverage-heading">
                      Discovery readiness
                      <Info aria-hidden="true" />
                      <span>
                        Availability on the published event after merge—not source-specific
                        extraction.
                      </span>
                    </span>
                  </th>
                  <th>Latest source</th>
                </tr>
              </thead>
              <tbody>
                {page.items.map((event) => {
                  const description = compactText(event.description);
                  const url = event.registration_url
                    ? safeHttpUrl(event.registration_url)
                    : null;
                  const sourceUrl = safeHttpUrl(event.source_event_id);
                  const mapsUrl = googleMapsUrl(event);
                  const readiness = canonicalReadiness(event);
                  const enrichment = catalogEnrichmentSummary(event);
                  const metadataGroups = catalogMetadataGroups(event);
                  const expanded = expandedEventId === event.canonical_event_id;
                  return (
                    <Fragment key={event.canonical_event_id}>
                      <tr className={expanded ? "is-expanded" : ""}>
                        <td>
                          <button
                            className="admin-event-title"
                            type="button"
                            title={event.title}
                            aria-expanded={expanded}
                            onClick={() => setExpandedEventId(
                              expanded ? null : event.canonical_event_id,
                            )}
                          >
                            <span>{event.title}</span>
                            <ChevronRight aria-hidden="true" />
                          </button>
                          <small
                            className={description ? "" : "is-missing"}
                            title={description || undefined}
                          >
                            {description || "No parsed description"}
                          </small>
                          <code title={event.canonical_event_id}>
                            cid:{shortRevision(event.canonical_event_id)}
                          </code>
                        </td>
                        <td>
                          <a
                            className="admin-catalog-text-link"
                            href={googleCalendarUrl(event)}
                            target="_blank"
                            rel="noreferrer"
                            title="Add this event to Google Calendar"
                          >
                            <CalendarPlus aria-hidden="true" />
                            <strong>{formatDateTime(event.start_at)}</strong>
                          </a>
                          <small>
                            {event.end_at ? `ends ${formatDateTime(event.end_at)}` : "No end time"}
                          </small>
                          <code>{event.price_status} · {event.event_status}</code>
                        </td>
                        <td>
                          {mapsUrl ? (
                            <a
                              className="admin-catalog-text-link"
                              href={mapsUrl}
                              target="_blank"
                              rel="noreferrer"
                              title="Open parsed location in Google Maps"
                            >
                              <MapPin aria-hidden="true" />
                              <strong>{locationText(event)}</strong>
                            </a>
                          ) : (
                            <strong className="is-missing">No parsed location</strong>
                          )}
                          <small>{geoText(event)}</small>
                        </td>
                        <td>
                          <span
                            className={`admin-field-coverage${
                              readiness.ready ? "" : " has-gaps"
                            }`}
                          >
                            {readiness.ready
                              ? <Check aria-hidden="true" />
                              : <Info aria-hidden="true" />}
                            {readiness.label}
                          </span>
                          <small>{readiness.detail}</small>
                          <code>
                            {enrichment.length
                              ? enrichment.join(" · ")
                              : "No optional metadata published"}
                          </code>
                        </td>
                        <td>
                          {investigation?.onOpenSource ? <button type="button" className="admin-catalog-text-link" aria-label={`Manage source for ${event.title}`} onClick={() => investigation.onOpenSource?.(event.source_key)}><strong>{event.source_display_name}</strong><ArrowUpRight aria-hidden="true" /></button> : <strong>{event.source_display_name}</strong>}
                          {sourceUrl ? (
                            <a
                              className="admin-provenance-link"
                              href={sourceUrl}
                              target="_blank"
                              rel="noreferrer"
                              title={event.source_event_id}
                              aria-label={`Open source record for ${event.title}`}
                            >
                              {event.source_event_id}
                            </a>
                          ) : (
                            <strong title={event.source_event_id}>{event.source_event_id}</strong>
                          )}
                          <small>seen {formatDateTime(event.last_seen_at)}</small>
                          <code title={event.refresh_run_key}>
                            run:{shortRevision(event.refresh_run_key)}
                          </code>
                        </td>
                      </tr>
                      {expanded ? (
                        <tr className="admin-catalog-detail-row">
                          <td colSpan={5}>
                            <article className="admin-catalog-event-detail">
                              <div className="admin-catalog-event-detail__main">
                                <div className="admin-catalog-event-detail__heading">
                                  <div>
                                    <code>{investigation ? event.canonical_event_id : `canonical/${shortRevision(event.canonical_event_id)}`}</code>
                                  </div>
                                  {url ? (
                                    <a className={styles.textLink} href={url} target="_blank" rel="noreferrer">
                                      Provider page
                                      <ArrowUpRight aria-hidden="true" />
                                    </a>
                                  ) : null}
                                </div>
                                <p className={description ? "" : "is-missing"}>
                                  {description || "No description was retained from the provider."}
                                </p>
                                {event.description_length > Array.from(event.description).length ? (
                                  <p>
                                    Showing {formatNumber(Array.from(event.description).length)} of {formatNumber(event.description_length)} description characters.
                                  </p>
                                ) : null}
                                {investigation ? (
                                  <div className={styles.identifiers}>
                                    <span role="status">
                                      {copyStatus?.key === `${sourceKey}:${event.canonical_event_id}` ? copyStatus.message : "Published record identifiers"}
                                    </span>
                                    <div className={styles.detailActions}>
                                      <button className={styles.textLink} type="button" onClick={() => void copyIdentifier(event)}><Copy aria-hidden="true" />Copy event ID</button>
                                      <button className={styles.textLink} type="button" onClick={() => void copyIdentifier(event, true)}><Copy aria-hidden="true" />Copy run key</button>
                                      {investigation.onOpenRun ? (
                                        <button className={styles.textLink} type="button" title="Open this source’s run history" onClick={() => investigation.onOpenRun?.(event.source_key, event.refresh_run_key)}>
                                          Publishing run<ArrowUpRight aria-hidden="true" />
                                        </button>
                                      ) : null}
                                    </div>
                                  </div>
                                ) : null}
                                <div className="admin-catalog-metadata-groups">
                                  {metadataGroups.map((group) => (
                                    <section key={group.id}>
                                      <h4>{group.label}</h4>
                                      <dl>
                                        {group.facts.map((fact) => (
                                          <div className={`is-${fact.state}`} key={fact.id}>
                                            <dt>
                                              <span>{fact.label}</span>
                                              <small>
                                                {fact.state === "not_applicable"
                                                  ? "N/A"
                                                  : fact.state === "unknown"
                                                    ? "Unknown"
                                                    : "Present"}
                                              </small>
                                            </dt>
                                            <dd title={fact.value}>
                                              {fact.links?.length ? (
                                                <span className="admin-catalog-profile-links">
                                                  {fact.links.map((link) => (
                                                    <a
                                                      href={link.href}
                                                      key={link.href}
                                                      target="_blank"
                                                      rel="noreferrer"
                                                    >
                                                      {link.label}
                                                      <ArrowUpRight aria-hidden="true" />
                                                    </a>
                                                  ))}
                                                </span>
                                              ) : fact.id === "last_observed" ? (
                                                formatDateTime(fact.value)
                                              ) : (
                                                fact.value
                                              )}
                                            </dd>
                                          </div>
                                        ))}
                                      </dl>
                                    </section>
                                  ))}
                                </div>
                                {investigation ? (
                                  <details>
                                    <summary>Published record fields</summary>
                                    <p>Current API projection, including bounded description text. This is not the raw provider response.</p>
                                    <pre style={{ maxHeight: 320, overflow: "auto", fontSize: 11, whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}>
                                      {JSON.stringify(event, null, 2)}
                                    </pre>
                                  </details>
                                ) : null}
                              </div>
                              <aside className="admin-catalog-coverage">
                                <div>
                                  <strong>Published-event discovery readiness</strong>
                                  <span>
                                    Availability on the published event after merge used by search,
                                    calendar, maps, and handoff. It does not attribute extraction to
                                    this source and is not a content-quality score.
                                  </span>
                                </div>
                                <ul>
                                  {READINESS_FIELDS.map((field) => {
                                    const present = !event.quality_issues.includes(field.issue);
                                    return (
                                      <li className={present ? "is-present" : "is-missing"} key={field.issue}>
                                        {present ? <Check aria-hidden="true" /> : <X aria-hidden="true" />}
                                        <span>{field.label}</span>
                                      </li>
                                    );
                                  })}
                                </ul>
                              </aside>
                            </article>
                          </td>
                        </tr>
                      ) : null}
                    </Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : loading && !page ? (
          <div className="admin-catalog-loading">
            <LoaderCircle className="spin" aria-hidden="true" />
            <span>Loading parsed events…</span>
          </div>
        ) : error ? null : (
          <div className="admin-empty-state admin-empty-state--compact">
            <Search aria-hidden="true" />
            <p>
              {runKey
                ? page && page.total > 0
                  ? "No records on this page. Attribution may have changed; return to the first page."
                  : settledQuery
                    ? "No records attributed to this run match this search."
                    : "No current records remain attributed to this run."
                : settledQuery
                ? "No published events match these filters."
                : "No published events match these filters."}
            </p>
          </div>
        )}
      </div>

      <div className="admin-catalog-pager">
        <span>
          {pageNumber === null ? "Later page" : `Page ${pageNumber}`}
          {page ? ` · ${formatNumber(page.items.length)} loaded` : ""}
        </span>
        <div>
          <button
            type="button"
            disabled={!cursor || loading}
            onClick={showPrevious}
            aria-label={cursor && historyIndex < 0 ? "First parsed events page" : "Previous parsed events page"}
          >
            <ChevronLeft aria-hidden="true" />
            {cursor && historyIndex < 0 ? "First page" : "Prev"}
          </button>
          <button
            type="button"
            disabled={!page?.has_more || !nextCursor || loading}
            onClick={showNext}
            aria-label="Next parsed events page"
          >
            Next
            <ChevronRight aria-hidden="true" />
          </button>
        </div>
      </div>
    </section>
  );
}
