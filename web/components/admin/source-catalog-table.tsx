"use client";

import {
  ArrowUpRight,
  CalendarPlus,
  Check,
  ChevronLeft,
  ChevronRight,
  CircleAlert,
  Info,
  LoaderCircle,
  MapPin,
  Search,
  X,
} from "lucide-react";
import { Fragment, useEffect, useMemo, useState } from "react";

import { getAdminSourceEvents } from "@/lib/admin-api";
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
  AdminCatalogEventPage,
} from "@/lib/admin-types";

interface SourceCatalogTableProps {
  sourceKey: string;
  sourceTotal: number;
  refreshToken: string | null;
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

function cursorFrom(page: AdminCatalogEventPage): AdminCatalogEventCursor | null {
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
  sourceTotal,
  refreshToken,
}: SourceCatalogTableProps) {
  const [query, setQuery] = useState("");
  const [settledQuery, setSettledQuery] = useState("");
  const [cursor, setCursor] = useState<AdminCatalogEventCursor | null>(null);
  const [cursorHistory, setCursorHistory] = useState<
    Array<AdminCatalogEventCursor | null>
  >([]);
  const [page, setPage] = useState<AdminCatalogEventPage | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [expandedEventId, setExpandedEventId] = useState<string | null>(null);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSettledQuery(query.trim());
      setCursor(null);
      setCursorHistory([]);
      setExpandedEventId(null);
    }, 240);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    void getAdminSourceEvents(sourceKey, settledQuery, cursor, PAGE_SIZE)
      .then((next) => {
        if (cancelled) return;
        setPage(next);
        setError(null);
      })
      .catch((nextError) => {
        if (!cancelled) setError(readableError(nextError));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [cursor, refreshToken, reloadNonce, settledQuery, sourceKey]);

  const nextCursor = useMemo(() => (page ? cursorFrom(page) : null), [page]);
  const pageNumber = cursorHistory.length + 1;
  const displayedTotal = page?.source_total ?? sourceTotal;

  const showNext = () => {
    if (!nextCursor) return;
    setCursorHistory((current) => [...current, cursor]);
    setCursor(nextCursor);
    setExpandedEventId(null);
  };

  const showPrevious = () => {
    if (!cursorHistory.length) return;
    const previous = cursorHistory.at(-1) ?? null;
    setCursorHistory((current) => current.slice(0, -1));
    setCursor(previous);
    setExpandedEventId(null);
  };

  return (
    <section className="admin-detail-section admin-catalog-section">
      <div className="admin-section-heading admin-section-heading--catalog">
        <div>
          <span>Parsed output</span>
          <h2>Current catalog evidence</h2>
        </div>
        <p>
          Latest successful non-fixture projection. Published record fields and provenance
          only—no raw provider payloads.
        </p>
      </div>

      <div className="admin-catalog-toolbar">
        <label className="admin-catalog-search">
          <Search aria-hidden="true" />
          <span className="sr-only">Search parsed source events</span>
          <input
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
        <div className="admin-catalog-count" aria-live="polite">
          <strong>{formatNumber(displayedTotal)}</strong>
          <span>current</span>
          {settledQuery ? <small>· filtered view</small> : null}
        </div>
      </div>

      {error ? (
        <div className="admin-inline-error admin-catalog-error">
          <CircleAlert aria-hidden="true" />
          <span>{error}</span>
          <button type="button" onClick={() => setReloadNonce((value) => value + 1)}>
            Retry
          </button>
        </div>
      ) : null}

      <div className={`admin-catalog-table-shell${loading ? " is-loading" : ""}`}>
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
                  <th>Provenance</th>
                  <th><span className="sr-only">Open event</span></th>
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
                          <button
                            className={`admin-field-coverage${
                              readiness.ready ? "" : " has-gaps"
                            }`}
                            type="button"
                            aria-expanded={expanded}
                            title="Inspect published-event readiness and optional metadata"
                            onClick={() => setExpandedEventId(
                              expanded ? null : event.canonical_event_id,
                            )}
                          >
                            {readiness.ready
                              ? <Check aria-hidden="true" />
                              : <Info aria-hidden="true" />}
                            {readiness.label}
                          </button>
                          <small>{readiness.detail}</small>
                          <code>
                            {enrichment.length
                              ? enrichment.join(" · ")
                              : "No optional metadata published"}
                          </code>
                        </td>
                        <td>
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
                        <td>
                          {url ? (
                            <a
                              className="admin-catalog-link"
                              href={url}
                              target="_blank"
                              rel="noreferrer"
                              aria-label={`Open ${event.title}`}
                            >
                              <ArrowUpRight aria-hidden="true" />
                            </a>
                          ) : (
                            <span className="admin-catalog-link is-disabled">—</span>
                          )}
                        </td>
                      </tr>
                      {expanded ? (
                        <tr className="admin-catalog-detail-row">
                          <td colSpan={6}>
                            <article className="admin-catalog-event-detail">
                              <div className="admin-catalog-event-detail__main">
                                <div className="admin-catalog-event-detail__heading">
                                  <div>
                                    <code>canonical/{shortRevision(event.canonical_event_id)}</code>
                                    <h3>{event.title}</h3>
                                  </div>
                                  {url ? (
                                    <a href={url} target="_blank" rel="noreferrer">
                                      Provider page
                                      <ArrowUpRight aria-hidden="true" />
                                    </a>
                                  ) : null}
                                </div>
                                <p className={description ? "" : "is-missing"}>
                                  {description || "No description was retained from the provider."}
                                </p>
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
        ) : (
          <div className="admin-empty-state admin-empty-state--compact">
            <Search aria-hidden="true" />
            <p>
              {settledQuery
                ? "No current parsed events match this search."
                : "This source has no current browseable events."}
            </p>
          </div>
        )}
      </div>

      <div className="admin-catalog-pager">
        <span>
          Page {pageNumber}
          {page ? ` · ${formatNumber(page.items.length)} loaded` : ""}
        </span>
        <div>
          <button
            type="button"
            disabled={!cursorHistory.length || loading}
            onClick={showPrevious}
            aria-label="Previous parsed events page"
          >
            <ChevronLeft aria-hidden="true" />
            Prev
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
