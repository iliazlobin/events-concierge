"use client";

import { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { ExternalLink, RefreshCw, X } from "lucide-react";
import { formatEventDate, formatEventTime } from "@/lib/date";
import { googleCalendarUrl } from "@/lib/event-links";
import { formatCity } from "@/lib/presentation";
import { registrationFilterCount, registrationFilters, registrationMatchesFilter,
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
}

/** Each executor supplies its own queue adapter; the panel owns only presentation and viewed versions. */
export function RegistrationWorkspace({ providers, selectedId, onClose }: {
  providers: RegistrationProviderWorkspace[]; selectedId: string | null; onClose: () => void;
}) {
  const [providerKey, setProviderKey] = useState(providers[0]?.provider.key);
  const [filter, setFilter] = useState<RegistrationFilter>("all");
  const workspace = providers.find(item => item.provider.key === providerKey) ?? providers[0];
  const dialog = useRef<HTMLDialogElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const titleId = useId();
  const didScroll = useRef(false);
  const items = workspace?.items ?? [];
  const visible = items.filter(item => registrationMatchesFilter(item, filter));
  useEffect(() => {
    const previous = document.activeElement;
    const element = dialog.current;
    element?.showModal();
    return () => { element?.close(); if (previous instanceof HTMLElement && previous.isConnected) previous.focus(); };
  }, []);
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
  const matchedTotal = workspace.counts ? registrationFilterCount(workspace.counts, filter) : null;
  return <dialog ref={dialog} className={styles.drawer} aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); onClose(); }}
    onClick={event => { if (event.target === event.currentTarget) {
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
    } }}>
    <div className={styles.content} ref={content}>
      <header className={styles.heading}><div><p className={styles.eyebrow}>{provider.label}</p><h2 id={titleId}>Registrations</h2></div>
        <button type="button" className={styles.iconButton} onClick={onClose} aria-label="Close registrations"><X aria-hidden="true" /></button></header>
      <div className={styles.actions}>
        <a className="button button-primary" href={provider.openUrl} target="_blank" rel="noopener noreferrer"><provider.Icon />Open {provider.label}</a>
        <Link className="button" href={provider.settingsUrl}>{provider.label} settings</Link>
      </div>
      <div className={styles.filters} aria-label="Registration status filters">{registrationFilters.map(({ key, label }) => <button type="button"
        key={key} aria-pressed={filter === key} onClick={() => setFilter(key)}>
        {label}<span>{workspace.counts ? registrationFilterCount(workspace.counts, key) : "–"}</span>
      </button>)}</div>
      <div className={styles.listHeading}><span>{workspace.total} {workspace.total === 1 ? "registration" : "registrations"}</span>
        <button type="button" className={styles.iconButton} onClick={workspace.onRefresh} disabled={workspace.loading} aria-label="Refresh registrations">
          <RefreshCw className={workspace.loading ? "spin" : undefined} aria-hidden="true" /></button></div>
      {workspace.error ? <p role="alert" className="workspace-error">{workspace.error}</p> : null}
      {workspace.loading && !items.length ? <p role="status">Loading registrations…</p> : null}
      {!workspace.loading && !visible.length && !workspace.error ? <p className={styles.empty}>
        {workspace.hasMore && (matchedTotal === null || matchedTotal > 0) ? "Load more to see other matching registrations." : filter === "all" ? "No registrations yet. Queue an event from its highlights." : "No registrations in this status."}
      </p> : null}
      <ol className={styles.tasks}>{visible.map(item => {
        const date = formatEventDate(item.event.start_at);
        const eventUrl = provider.evidenceUrl(item.event.registration_url);
        const calendarUrl = googleCalendarUrl({ ...item.event, latitude: null, longitude: null, description: "",
          registration_urls: eventUrl ? [eventUrl] : [], sources: [] });
        const evidence = item.outcome?.evidence_url ? provider.evidenceUrl(item.outcome.evidence_url) : null;
        return <li key={item.event.canonical_event_id} data-registration={item.event.canonical_event_id}
          className={selectedId === item.event.canonical_event_id ? styles.selectedTask : undefined}>
          <div className={styles.taskTitle}>{calendarUrl ? <a className={styles.date} href={calendarUrl} target="_blank" rel="noopener noreferrer"
            aria-label={`Add ${item.event.title} to Google Calendar`} title="Add to Google Calendar"><span>{date.month}</span><b>{date.day}</b></a> : null}
            <div><a className={styles.eventTitle} href={eventUrl ?? undefined} target="_blank" rel="noopener noreferrer">{item.event.title}<ExternalLink aria-hidden="true" /></a>
              <p className={styles.taskDate}>{date.weekday} · {formatEventTime(item.event.start_at, item.event.end_at)}{item.event.city ? ` · ${formatCity(item.event.city)}` : ""}</p></div>
            {item.unread ? <span className={styles.unreadDot} aria-label="Unread update" /> : null}</div>
          <p className={styles.status} data-status={item.status}><span aria-hidden="true" />{provider.statusLabels[item.status]}</p>
          {item.status === "queued" ? <p className={styles.note}>Waiting for you to start {provider.label}.</p> : null}
          {item.status === "uncertain" ? <p className={styles.note}>Check provider confirmation before retrying.</p> : null}
          {item.outcome?.note ? <p className={styles.note}>{item.outcome.note}</p> : null}
          {item.outcome?.confirmation_reference ? <p className={styles.note}>Confirmation: {item.outcome.confirmation_reference}</p> : null}
          <div className={styles.taskLinks}><Link href={`/?view=events&when=all&city=&q=${encodeURIComponent(item.event.title)}&event=${encodeURIComponent(item.event.canonical_event_id)}`}>Find in catalog</Link>
            {evidence ? <a href={evidence} target="_blank" rel="noopener noreferrer">Provider evidence</a> : null}</div>
        </li>;
      })}</ol>
      {workspace.hasMore ? <button type="button" className="button" disabled={workspace.loading} onClick={workspace.onLoadMore}>
        {workspace.loading ? "Loading…" : "Load more registrations"}</button> : null}
    </div>
    <nav className={styles.providerNav} aria-label="Registration providers">{providers.map(candidate => <button type="button"
      key={candidate.provider.key} aria-label={`${candidate.provider.label} registrations`} title={candidate.provider.label}
      aria-pressed={workspace.provider.key === candidate.provider.key}
      onClick={() => { setProviderKey(candidate.provider.key); setFilter("all"); }}>
      <candidate.provider.Icon /><span>{candidate.provider.label}</span>
      {candidate.unread > 0 ? <b>{candidate.unread}</b> : null}
    </button>)}</nav>
  </dialog>;
}
