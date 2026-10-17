"use client";

import { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { ChevronDown, ExternalLink, RefreshCw, Settings, Trash2, Undo2, X } from "lucide-react";
import { formatEventDate, formatEventTime } from "@/lib/date";
import { googleCalendarUrl } from "@/lib/event-links";
import { formatCity } from "@/lib/presentation";
import { registrationActivity, registrationFilterCount, registrationFilters, registrationMatchesFilter, registrationProgress,
  type RegistrationFilter, type RegistrationStatusCounts, type RegistrationTask } from "@/lib/registrations";
import type { RegistrationProvider } from "./registration-providers";
import styles from "./registration-workspace.module.css";

export interface RegistrationProviderWorkspace {
  provider: RegistrationProvider;
  items: RegistrationTask[];
  total: number;
  unread: number;
  counts: RegistrationStatusCounts | null;
  loading: boolean;
  error: string | null;
  hasMore: boolean;
  onRefresh: () => void;
  onLoadMore: () => void;
  onSeen: (items: RegistrationTask[]) => void;
  onManage: (items: RegistrationTask[], removed: boolean, all?: boolean) => Promise<boolean>;
  managing: boolean;
}

/** Each executor supplies its own queue adapter; the panel owns only presentation and viewed versions. */
export function RegistrationWorkspace({ providers, selectedId, onClose }: {
  providers: RegistrationProviderWorkspace[]; selectedId: string | null; onClose: () => void;
}) {
  const [providerKey, setProviderKey] = useState(providers[0]?.provider.key);
  const [selection, setSelection] = useState<Set<string>>(new Set());
  const [confirmRemoval, setConfirmRemoval] = useState<RegistrationTask[] | "all" | null>(null);
  const [filter, setFilter] = useState<RegistrationFilter>("all");
  const workspace = providers.find(item => item.provider.key === providerKey) ?? providers[0];
  const dialog = useRef<HTMLDialogElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const confirmation = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const didScroll = useRef(false);
  const openedSelection = useRef<string | null>(null);
  const items = workspace?.items ?? [];
  const visible = items.filter(item => registrationMatchesFilter(item, filter));
  useEffect(() => {
    const previous = document.activeElement;
    const element = dialog.current;
    element?.showModal();
    return () => { element?.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    if (!confirmRemoval) return;
    const previous = document.activeElement;
    confirmation.current?.focus();
    confirmation.current?.scrollIntoView({ block: "nearest" });
    return () => { if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, [confirmRemoval]);
  useEffect(() => { setSelection(new Set()); setConfirmRemoval(null); }, [providerKey, filter]);
  useEffect(() => {
    if (selectedId !== openedSelection.current) {
      const item = items.find(item => item.event.canonical_event_id === selectedId);
      if (item) { openedSelection.current = selectedId; if (item.removed_at) setFilter("removed"); }
    }
  }, [selectedId, items]);
  useEffect(() => { didScroll.current = false; }, [providerKey, filter, selectedId]);
  useEffect(() => {
    if (!selectedId || didScroll.current) return;
    const selected = content.current?.querySelector<HTMLElement>(`[data-registration="${CSS.escape(selectedId)}"]`);
    if (selected) { selected.scrollIntoView({ block: "nearest" }); didScroll.current = true; }
  }, [items, selectedId, filter]);
  useEffect(() => {
    const element = content.current;
    if (!element || !workspace) return;
    const byId = new Map(visible.map(item => [item.event.canonical_event_id, item]));
    const observer = new IntersectionObserver(entries => {
      const observed = entries.filter(entry => entry.isIntersecting).flatMap(entry => {
        const id = (entry.target as HTMLElement).dataset.registration;
        const item = id ? byId.get(id) : null;
        return item?.unread ? [item] : [];
      });
      if (observed.length) workspace.onSeen(observed);
    }, { root: element, threshold: 0.25 });
    element.querySelectorAll("[data-registration]").forEach(item => observer.observe(item));
    return () => observer.disconnect();
  }, [items, filter, workspace?.onSeen]);
  if (!workspace) return null;
  const { provider } = workspace;
  const chosen = visible.filter(item => selection.has(item.event.canonical_event_id));
  const selectable = visible.slice(0, 100);
  const allSelected = selectable.length > 0 && selectable.every(item => selection.has(item.event.canonical_event_id));
  const listCount = filter === "removed" ? workspace.counts?.removed ?? 0 : workspace.total - (workspace.counts?.removed ?? 0);
  const matchedTotal = workspace.counts ? registrationFilterCount(workspace.counts, filter) : null;
  return <dialog ref={dialog} className={styles.drawer} aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); onClose(); }}
    onClick={event => { if (event.target === event.currentTarget) {
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
    } }}>
    <div className={styles.content} ref={content}>
      <header className={styles.heading}>
        <div className={styles.selectors}>
          <h2 id={titleId} className={styles.headingTitle}>Registrations</h2>
          <label className={`${styles.dropdown} ${styles.providerDropdown}`}>
            <select aria-label="Registration provider" value={provider.key}
              onChange={event => {
                const next = providers.find(candidate => candidate.provider.key === event.target.value);
                if (next) { setProviderKey(next.provider.key); setFilter("all"); }
              }}>
              {providers.map(candidate => <option key={candidate.provider.key} value={candidate.provider.key}>
                {candidate.provider.label}{candidate.unread > 0 ? ` (${candidate.unread} unread)` : ""}
              </option>)}
            </select><ChevronDown aria-hidden="true" />
          </label>
        </div>
        <div className={styles.headingActions}>
          <a className={styles.iconButton} href={provider.openUrl} target="_blank" rel="noopener noreferrer"
            aria-label={`Open ${provider.label}`} title={`Open ${provider.label}`}><provider.Icon /></a>
          <Link className={styles.iconButton} href={provider.settingsUrl}
            aria-label={`${provider.label} settings`} title={`${provider.label} settings`}><Settings aria-hidden="true" /></Link>
          <button type="button" className={styles.iconButton} onClick={onClose} aria-label="Close registrations" title="Close registrations"><X aria-hidden="true" /></button>
        </div></header>
      <div className={styles.filters} aria-label="Registration status filters">{registrationFilters.map(({ key, label }) => <button type="button"
        key={key} aria-pressed={filter === key} onClick={() => setFilter(key)}>
        {label}<span>{workspace.counts ? registrationFilterCount(workspace.counts, key) : "–"}</span>
      </button>)}</div>
      <div className={styles.listHeading}><span>{listCount} {filter === "removed" ? "removed" : listCount === 1 ? "registration" : "registrations"}</span>
        <button type="button" className={styles.iconButton} onClick={workspace.onRefresh} disabled={workspace.loading} aria-label="Refresh registrations">
          <RefreshCw className={workspace.loading ? "spin" : undefined} aria-hidden="true" /></button></div>
      <div className={styles.management}>
        <label><input type="checkbox" aria-label="Select loaded registrations" title="Select up to 100 loaded registrations" checked={allSelected}
          disabled={workspace.managing || !selectable.length} onChange={() => setSelection(allSelected ? new Set()
            : new Set(selectable.map(item => item.event.canonical_event_id)))} />Select loaded</label>
        {chosen.length > 0 ? <button type="button" className="button" disabled={workspace.managing}
          onClick={() => filter === "removed" ? void workspace.onManage(chosen, false).then(ok => { if (ok) setSelection(new Set()); })
            : setConfirmRemoval(chosen)}>{filter === "removed" ? "Restore" : "Remove selected"} ({chosen.length})</button> : null}
        {filter !== "removed" ? <button type="button" className="button" disabled={workspace.managing || workspace.total === (workspace.counts?.removed ?? 0)}
          onClick={() => setConfirmRemoval("all")}>Remove all</button> : null}
      </div>
      {confirmRemoval ? <div ref={confirmation} tabIndex={-1} className={styles.confirmation} role="group" aria-label="Confirm removal">
        <p>Remove {confirmRemoval === "all" ? "all registrations across every page and status" : `${confirmRemoval.length} selected registration${confirmRemoval.length === 1 ? "" : "s"}`}?</p>
        <p>Queued work stops. Started work may finish. Provider RSVPs stay unchanged. You can restore items from Removed.</p>
        <div><button type="button" className="button" disabled={workspace.managing} onClick={() => setConfirmRemoval(null)}>Cancel</button>
          <button type="button" className="button" disabled={workspace.managing} onClick={() => {
            void workspace.onManage(confirmRemoval === "all" ? [] : confirmRemoval, true, confirmRemoval === "all")
              .then(ok => { if (ok) { setConfirmRemoval(null); setSelection(new Set()); } });
          }}>{workspace.managing ? "Removing…" : "Remove"}</button></div>
      </div> : null}
      <p className={styles.progressHint}>Saved {provider.label} updates · refreshes every 15s. Browser steps aren’t shared.</p>
      {workspace.error ? <p role="alert" className="workspace-error">{workspace.error}</p> : null}
      {workspace.loading && !items.length ? <p role="status">Loading registrations…</p> : null}
      {!workspace.loading && !visible.length && !workspace.error ? <p className={styles.empty}>
        {workspace.hasMore && (matchedTotal === null || matchedTotal > 0) ? "More registrations available." : filter === "all" ? "No registrations yet." : "No matching registrations."}
      </p> : null}
      <ol className={styles.tasks}>{visible.map(item => {
        const date = formatEventDate(item.event.start_at);
        const eventUrl = provider.evidenceUrl(item.event.registration_url);
        const calendarUrl = googleCalendarUrl({ ...item.event, latitude: null, longitude: null, description: "",
          registration_urls: eventUrl ? [eventUrl] : [], sources: [] });
        const evidence = item.outcome?.evidence_url ? provider.evidenceUrl(item.outcome.evidence_url) : null;
        return <li key={item.event.canonical_event_id} data-registration={item.event.canonical_event_id}
          className={selectedId === item.event.canonical_event_id ? styles.selectedTask : undefined}>
          <div className={styles.taskTitle}>
            <input type="checkbox" aria-label={`Select ${item.event.title}`} checked={selection.has(item.event.canonical_event_id)}
              disabled={workspace.managing || (!selection.has(item.event.canonical_event_id) && selection.size >= 100)}
              onChange={() => setSelection(old => { const next = new Set(old); const id = item.event.canonical_event_id;
                if (next.has(id)) next.delete(id); else next.add(id); return next; })} />
            {calendarUrl ? <a className={styles.date} href={calendarUrl} target="_blank" rel="noopener noreferrer"
            aria-label={`Add ${item.event.title} to Google Calendar`} title="Add to Google Calendar"><span>{date.month}</span><b>{date.day}</b></a> : null}
            <div><a className={styles.eventTitle} href={eventUrl ?? undefined} target="_blank" rel="noopener noreferrer">{item.event.title}<ExternalLink aria-hidden="true" /></a>
              <p className={styles.taskDate}>{date.weekday} · {formatEventTime(item.event.start_at, item.event.end_at)}{item.event.city ? ` · ${formatCity(item.event.city)}` : ""}</p></div>
            <button type="button" className={styles.iconButton} disabled={workspace.managing}
              title={item.removed_at ? "Restore registration" : "Remove registration"}
              aria-label={`${item.removed_at ? "Restore" : "Remove"} ${item.event.title}`}
              onClick={() => item.removed_at ? void workspace.onManage([item], false) : setConfirmRemoval([item])}>
              {item.removed_at ? <Undo2 aria-hidden="true" /> : <Trash2 aria-hidden="true" />}</button>
            {item.unread ? <span className={styles.unreadDot} aria-label="Unread update" /> : null}</div>
          <div className={styles.statusLine}>
            <p className={styles.status} data-status={item.status} title={item.registration_account ? `Registration account: ${item.registration_account.expected_email ?? "current browser account"}` : undefined}><span aria-hidden="true" />{item.removed_at ? "Removed · " : ""}{provider.statusLabels[item.status]}</p>
            <span className={styles.updated}>Updated <RegistrationTime value={item.updated_at} /></span>
          </div>
          <p className={styles.note}>{item.removed_at ? (item.status === "queued" ? "Removed from queue. Restore to resume." : "Removed from list. Provider RSVP unchanged.") : item.outcome?.note.trim() || registrationProgress[item.status]}</p>
          {item.outcome?.confirmation_reference ? <p className={styles.note}>Confirmation: {item.outcome.confirmation_reference}</p> : null}
          <details className={styles.activity}>
            <summary>Activity</summary>
            <ol aria-label={`Activity for ${item.event.title}`}>
              {registrationActivity(item).map(entry => <li key={entry.status}>
                <span>{entry.status === "queued" ? "Added to queue" : entry.status === "in_progress"
                  ? "Picked up by Muse" : provider.statusLabels[entry.status]}</span>
                <RegistrationTime value={entry.at} />
              </li>)}
            </ol>
          </details>
          {evidence ? <div className={styles.taskLinks}><a href={evidence} target="_blank" rel="noopener noreferrer">Provider evidence</a></div> : null}
        </li>;
      })}</ol>
      {workspace.hasMore ? <button type="button" className="button" disabled={workspace.loading} onClick={workspace.onLoadMore}>
        {workspace.loading ? "Loading…" : "Load more registrations"}</button> : null}
    </div>
  </dialog>;
}

function RegistrationTime({ value }: { value: string }) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return <span>Time unavailable</span>;
  const text = date.toLocaleString(undefined, {
    month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short",
  });
  return <time dateTime={value} title={date.toLocaleString(undefined, { timeZoneName: "short" })}>{text}</time>;
}
