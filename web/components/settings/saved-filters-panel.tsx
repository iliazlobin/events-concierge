"use client";

import { Bookmark, ExternalLink, LoaderCircle, Pencil, Trash2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { useSession } from "@/components/session-provider";
import {
  deleteSavedFilter,
  getCatalogFacets,
  listSavedFilters,
  readableError,
  replaceSavedFilter,
} from "@/lib/api";
import { describeCatalogSelection } from "@/lib/catalog-filter-name";
import { emptyCatalogFilters, initialCatalogFilters } from "@/lib/catalog-filters";
import { consumerHistoryUrl, createConsumerHistorySnapshot } from "@/lib/consumer-history";
import type { CatalogFilters, CatalogProvider, CatalogTopic, SavedFilter } from "@/lib/types";

const DEFAULT_FILTERS = initialCatalogFilters();

/** A payload kept before a filter field existed is layered over the defaults rather than trusted. */
function completeFilters(saved: SavedFilter): CatalogFilters {
  return { ...DEFAULT_FILTERS, ...saved.filters };
}

/**
 * Where opening this selection lands.
 *
 * Built against a fixed base rather than the current location: the settings path is not where the
 * selection belongs, and a link that reads the same on every render is one less thing to hydrate.
 */
function openHref(saved: SavedFilter): string {
  return consumerHistoryUrl(
    createConsumerHistorySnapshot("events", completeFilters(saved), null),
    "http://events.invalid/",
  );
}

function when(value: string | null): string {
  if (!value) return "never";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/**
 * Manage the filter selections kept from the events view.
 *
 * The picker in the filter strip is built for reapplying one in a hurry, which is why it shows a
 * short recency-ordered list and nothing else. This is the other half: the whole set, what each one
 * actually selects, and the two edits -- renaming and deleting -- that do not belong beside a
 * control whose job is to apply.
 */
export function SavedFiltersPanel() {
  const { tenantId } = useSession();
  const [rows, setRows] = useState<SavedFilter[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [providers, setProviders] = useState<CatalogProvider[]>([]);
  const [topics, setTopics] = useState<CatalogTopic[]>([]);
  const [renaming, setRenaming] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const [pendingDelete, setPendingDelete] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const labelsRequested = useRef(false);

  useEffect(() => {
    let cancelled = false;
    void listSavedFilters(tenantId)
      .then((saved) => {
        if (!cancelled) setRows(saved);
      })
      .catch((loadError: unknown) => {
        if (!cancelled) {
          setError(readableError(loadError, "Your saved filters could not be loaded."));
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [tenantId]);

  // Turning a source or topic key into a word costs three whole-catalog aggregations, so it is only
  // paid for by the selections that hold one. A failure leaves the keys showing, which still reads.
  useEffect(() => {
    if (labelsRequested.current) return undefined;
    const needsLabels = rows.some((row) => (
      (row.filters?.sourceKeys?.length ?? 0) > 0 || (row.filters?.topics?.length ?? 0) > 0
    ));
    if (!needsLabels) return undefined;
    labelsRequested.current = true;
    let cancelled = false;
    void getCatalogFacets(tenantId, emptyCatalogFilters())
      .then((page) => {
        if (cancelled) return;
        setProviders(page.providers ?? []);
        setTopics(page.topic_facets ?? []);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [rows, tenantId]);

  const startRename = (entry: SavedFilter) => {
    setPendingDelete(null);
    setRenaming(entry.saved_filter_id);
    setDraft(entry.name);
  };

  const commitRename = async (entry: SavedFilter) => {
    const name = draft.trim();
    if (!name || name === entry.name) {
      setRenaming(null);
      return;
    }
    setBusyId(entry.saved_filter_id);
    setError(null);
    try {
      const updated = await replaceSavedFilter(
        entry.saved_filter_id,
        name,
        entry.filters,
        tenantId,
      );
      setRows((current) => current.map((row) => (
        row.saved_filter_id === updated.saved_filter_id ? updated : row
      )));
      setRenaming(null);
    } catch (renameError) {
      setError(readableError(renameError, "That name could not be saved."));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async (entry: SavedFilter) => {
    setBusyId(entry.saved_filter_id);
    setError(null);
    try {
      await deleteSavedFilter(entry.saved_filter_id, tenantId);
      setRows((current) => current.filter((row) => (
        row.saved_filter_id !== entry.saved_filter_id
      )));
      setPendingDelete(null);
    } catch (deleteError) {
      setError(readableError(deleteError, "That filter could not be deleted."));
    } finally {
      setBusyId(null);
    }
  };

  return (
    <div className="settings-panel">
      <header className="settings-panel__head">
        <h1>Saved filters</h1>
        <p>
          The selections you kept from the events view. Open one, rename it, or delete it for good.
        </p>
      </header>

      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}

      <section className="settings-card">
        <h2 className="settings-card__title">Your saved filters</h2>
        {loading ? (
          <p className="settings-hint">Loading…</p>
        ) : rows.length === 0 ? (
          <p className="settings-empty">
            <Bookmark aria-hidden="true" />
            Nothing saved yet. Keep a selection from the filter strip and it appears here.
          </p>
        ) : (
          <ul className="saved-list">
            {rows.map((entry) => {
              const parts = describeCatalogSelection(
                completeFilters(entry),
                providers,
                topics,
              );
              const busy = busyId === entry.saved_filter_id;
              return (
                <li key={entry.saved_filter_id}>
                  <div className="saved-list__main">
                    {renaming === entry.saved_filter_id ? (
                      <div className="saved-list__rename">
                        <input
                          autoFocus
                          type="text"
                          value={draft}
                          maxLength={80}
                          aria-label={`Rename ${entry.name}`}
                          onChange={(event) => setDraft(event.target.value)}
                          onKeyDown={(event) => {
                            if (event.key === "Enter") {
                              event.preventDefault();
                              void commitRename(entry);
                            } else if (event.key === "Escape") {
                              event.preventDefault();
                              setRenaming(null);
                            }
                          }}
                        />
                        <button
                          type="button"
                          className="button button-primary"
                          disabled={busy || draft.trim() === ""}
                          onClick={() => commitRename(entry)}
                        >
                          {busy ? <LoaderCircle className="spin" aria-hidden="true" /> : null}
                          Save
                        </button>
                        <button
                          type="button"
                          className="button button-quiet"
                          onClick={() => setRenaming(null)}
                        >
                          Cancel
                        </button>
                      </div>
                    ) : (
                      <p className="saved-list__name">{entry.name}</p>
                    )}

                    {parts.length ? (
                      <p className="saved-list__parts">
                        {parts.map((part) => (
                          <span className="saved-list__part" key={part}>
                            {part}
                          </span>
                        ))}
                      </p>
                    ) : (
                      <p className="saved-list__parts">
                        <span className="saved-list__part">Everything, unfiltered</span>
                      </p>
                    )}

                    <p className="saved-list__meta">
                      Saved {when(entry.created_at)} · last used {when(entry.last_used_at)}
                    </p>
                  </div>

                  {pendingDelete === entry.saved_filter_id ? (
                    <div className="saved-list__confirm">
                      <p>Delete for good?</p>
                      <button
                        type="button"
                        className="button button-quiet"
                        onClick={() => setPendingDelete(null)}
                      >
                        Keep
                      </button>
                      <button
                        type="button"
                        className="button button-quiet-danger"
                        disabled={busy}
                        onClick={() => remove(entry)}
                      >
                        {busy ? <LoaderCircle className="spin" aria-hidden="true" /> : null}
                        Delete
                      </button>
                    </div>
                  ) : (
                    <div className="saved-list__actions">
                      <a className="button button-quiet" href={openHref(entry)}>
                        <ExternalLink aria-hidden="true" />
                        Open
                      </a>
                      <button
                        type="button"
                        className="button button-quiet"
                        onClick={() => startRename(entry)}
                      >
                        <Pencil aria-hidden="true" />
                        Rename
                      </button>
                      <button
                        type="button"
                        className="button button-quiet-danger"
                        aria-label={`Delete ${entry.name}`}
                        onClick={() => {
                          setRenaming(null);
                          setPendingDelete(entry.saved_filter_id);
                        }}
                      >
                        <Trash2 aria-hidden="true" />
                        Delete
                      </button>
                    </div>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
