"use client";

import { Bookmark, BookmarkCheck, Check, Clock, Search, SortAsc, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import type { SavedFilter, SavedFilterSort } from "@/lib/types";

interface SavedFilterPickerProps {
  saved: SavedFilter[];
  /** The selection the strip is currently holding, when it is one of these. */
  appliedId?: string | null;
  /** The name offered when saving the current selection, already deduplicated. */
  suggestedName: string;
  busy?: boolean;
  error?: string | null;
  onApply: (saved: SavedFilter) => void;
  onSave: (name: string) => void;
  onDelete: (saved: SavedFilter) => void;
}

function usedLabel(saved: SavedFilter, now: number): string {
  const at = saved.last_used_at ? Date.parse(saved.last_used_at) : Number.NaN;
  if (Number.isNaN(at)) return "";
  const minutes = Math.max(0, Math.round((now - at) / 60_000));
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return days < 30 ? `${days}d ago` : `${Math.round(days / 30)}mo ago`;
}

/**
 * Save the current filter selection, and reapply one saved earlier.
 *
 * Recency is the default order because the reason to keep a filter is to come back to it, and the
 * one you want next is usually the one you wanted last. Sorting by name is the escape hatch once
 * the list is long enough that recency stops being a memory aid.
 */
export function SavedFilterPicker({
  saved,
  appliedId = null,
  suggestedName,
  busy = false,
  error = null,
  onApply,
  onSave,
  onDelete,
}: SavedFilterPickerProps) {
  const [open, setOpen] = useState(false);
  const [term, setTerm] = useState("");
  const [sort, setSort] = useState<SavedFilterSort>("recent");
  const [draftName, setDraftName] = useState(suggestedName);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const [now, setNow] = useState(0);

  // Relative times are only meaningful once the client has a clock; reading it during render
  // would disagree with the server-rendered markup.
  useEffect(() => setNow(Date.now()), [open]);

  // The trigger is the only part of this on screen most of the time, so it is where "you are on a
  // selection you kept" has to be legible -- the count cannot say that.
  const applied = useMemo(
    () => (appliedId ? saved.find((entry) => entry.saved_filter_id === appliedId) ?? null : null),
    [appliedId, saved],
  );

  const visible = useMemo(() => {
    const needle = term.trim().toLocaleLowerCase();
    const matched = needle
      ? saved.filter((entry) => entry.name.toLocaleLowerCase().includes(needle))
      : [...saved];
    // The server already returns recency order, so only the name order is re-derived here.
    return sort === "name"
      ? matched.sort((left, right) => left.name.localeCompare(right.name))
      : matched;
  }, [saved, sort, term]);

  const openPicker = () => {
    setTerm("");
    setDraftName(suggestedName);
    setOpen(true);
    window.requestAnimationFrame(() => nameInputRef.current?.select());
  };

  const dismiss = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };

  /**
   * Park focus somewhere still mounted, inside the popover.
   *
   * Every control here closes the popover when focus leaves it, and several of them unmount
   * themselves as part of what they do. The name field is the usual landing spot, but it is absent
   * while the current selection is already kept, so the popover itself is the fallback.
   */
  const keepFocusInside = () => {
    if (nameInputRef.current) nameInputRef.current.focus();
    else popoverRef.current?.focus();
  };

  const commitSave = () => {
    const name = draftName.trim();
    // Nothing to save: the strip is already holding one of these.
    if (!name || busy || applied) return;
    onSave(name);
    setOpen(false);
  };

  return (
    <div
      className="filter-chip-editor filter-chip-editor--add saved-filter-picker"
      onBlur={(event) => {
        const control = event.currentTarget;
        window.setTimeout(() => {
          if (!control.contains(document.activeElement)) setOpen(false);
        }, 0);
      }}
    >
      <button
        ref={triggerRef}
        className={`active-filter-add saved-filter-picker__trigger${applied ? " is-applied" : ""}`}
        type="button"
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={applied ? `Saved filters — applied: ${applied.name}` : undefined}
        title={applied ? `Applied: ${applied.name}` : undefined}
        onClick={() => (open ? setOpen(false) : openPicker())}
      >
        {applied ? <BookmarkCheck aria-hidden="true" /> : <Bookmark aria-hidden="true" />}
        <span className="saved-filter-picker__trigger-label">
          {applied
            ? applied.name
            : saved.length ? `Saved · ${saved.length}` : "Save filters"}
        </span>
      </button>

      {open ? (
        <div
          ref={popoverRef}
          className="filter-chip-editor__popover saved-filter-picker__popover"
          role="dialog"
          aria-label="Saved filters"
          tabIndex={-1}
          onKeyDown={(event) => {
            if (event.key !== "Escape") return;
            event.preventDefault();
            event.stopPropagation();
            dismiss();
          }}
        >
          <div className="filter-chip-editor__mode">
            <span>Saved filters</span>
            <small>Reapply or delete a selection you kept</small>
          </div>

          {/* Saving is for a selection that is not kept yet. When the strip already holds one of
              these, a second copy under a second name is not a new selection -- it is the same
              one, twice -- so the control says what is already true instead of offering it. */}
          {applied ? (
            <div className="saved-filter-picker__name saved-filter-picker__kept">
              <span>Save the current filters as</span>
              <p>
                <Check aria-hidden="true" />
                <span>Already saved as</span>
                <strong>{applied.name}</strong>
              </p>
            </div>
          ) : (
            <label className="saved-filter-picker__name">
              <span>Save the current filters as</span>
              <div>
                <input
                  ref={nameInputRef}
                  value={draftName}
                  placeholder="Name this selection"
                  maxLength={80}
                  onChange={(event) => setDraftName(event.target.value)}
                  onKeyDown={(event) => {
                    if (event.key !== "Enter") return;
                    event.preventDefault();
                    commitSave();
                  }}
                />
                <button
                  className="is-primary"
                  type="button"
                  disabled={!draftName.trim() || busy}
                  onClick={commitSave}
                >
                  Save
                </button>
              </div>
            </label>
          )}

          {error ? <p className="saved-filter-picker__error">{error}</p> : null}

          {saved.length ? (
            <>
              <div className="saved-filter-picker__toolbar">
                <label className="filter-chip-editor__search">
                  <Search aria-hidden="true" />
                  <span className="sr-only">Find a saved filter</span>
                  <input
                    value={term}
                    placeholder="Find a saved filter"
                    maxLength={80}
                    onChange={(event) => setTerm(event.target.value)}
                  />
                </label>
                <button
                  type="button"
                  aria-pressed={sort === "name"}
                  title={sort === "recent" ? "Sorted by recent use" : "Sorted by name"}
                  onClick={() => setSort((current) => (current === "recent" ? "name" : "recent"))}
                >
                  {sort === "recent" ? <Clock aria-hidden="true" /> : <SortAsc aria-hidden="true" />}
                  {sort === "recent" ? "Recent" : "Name"}
                </button>
              </div>

              {/* A row carries two actions, so it is a group of controls rather than a listbox of
                  values -- an `option` cannot hold a button of its own. */}
              <div
                className="filter-chip-editor__options saved-filter-picker__options"
                role="group"
                aria-label="Saved filters"
              >
                {visible.map((entry) => {
                  const isApplied = entry.saved_filter_id === appliedId;
                  return (
                    <div
                      className={`saved-filter-picker__row${isApplied ? " is-applied" : ""}`}
                      key={entry.saved_filter_id}
                    >
                      {/* The tick means applied, the way it does in every other picker here.
                          Reapplying the one already on screen is harmless, so the control
                          stays live rather than going disabled. */}
                      <button
                        type="button"
                        aria-current={isApplied ? "true" : undefined}
                        title={isApplied
                          ? `${entry.name} — currently applied`
                          : `Apply ${entry.name}`}
                        onClick={() => {
                          onApply(entry);
                          setOpen(false);
                        }}
                      >
                        <Check aria-hidden="true" />
                        <strong>{entry.name}</strong>
                        <small>{usedLabel(entry, now)}</small>
                      </button>
                      <button
                        className="saved-filter-picker__delete"
                        type="button"
                        aria-label={`Delete ${entry.name}`}
                        title={`Delete ${entry.name}`}
                        onClick={() => {
                          onDelete(entry);
                          // This button unmounts with the row; without handing focus back
                          // inside, the popover reads that as a click away and closes.
                          keepFocusInside();
                        }}
                      >
                        <Trash2 aria-hidden="true" />
                      </button>
                    </div>
                  );
                })}
                {visible.length ? null : (
                  <p className="filter-chip-editor__empty">No saved filter matches</p>
                )}
              </div>
            </>
          ) : (
            <p className="filter-chip-editor__empty">
              Nothing saved yet. Name the current filters above to keep them.
            </p>
          )}

          <footer>
            <button type="button" onClick={dismiss}>
              <X aria-hidden="true" />
              Close
            </button>
          </footer>
        </div>
      ) : null}
    </div>
  );
}
