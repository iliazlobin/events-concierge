"use client";

import { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { Check, Clipboard, ExternalLink, RefreshCw, X } from "lucide-react";
import { MUSE_URL, museEventDate, museInstruction, museProviderUrl, museStatusLabel, type MuseRegistration } from "@/lib/muse";
import { MuseIcon } from "./muse-icon";
import styles from "./muse.module.css";

interface Props {
  items: MuseRegistration[];
  total: number;
  loading: boolean;
  error: string | null;
  hasMore: boolean;
  selectedId: string | null;
  onClose: () => void;
  onRefresh: () => void;
  onLoadMore: () => void;
  onSeen: (items: MuseRegistration[]) => void;
}

export function MuseRegistrations({ items, total, loading, error, hasMore, selectedId,
  onClose, onRefresh, onLoadMore, onSeen }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);
  const [showInstruction, setShowInstruction] = useState(false);
  const didScroll = useRef(false);
  useEffect(() => {
    const element = dialog.current;
    const previous = document.activeElement;
    element?.showModal();
    return () => {
      element?.close();
      if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    };
  }, []);
  useEffect(() => {
    if (!selectedId || didScroll.current) return;
    const selected = dialog.current?.querySelector<HTMLElement>(`[data-registration="${CSS.escape(selectedId)}"]`);
    if (selected) { selected.scrollIntoView({ block: "nearest" }); didScroll.current = true; }
  }, [items, selectedId]);
  useEffect(() => {
    const element = dialog.current;
    if (!element) return;
    const byId = new Map(items.map(item => [item.event.canonical_event_id, item]));
    const observer = new IntersectionObserver(entries => {
      const observed = entries.filter(entry => entry.isIntersecting).flatMap(entry => {
        const id = (entry.target as HTMLElement).dataset.registration;
        const item = id ? byId.get(id) : null;
        return item?.unread ? [item] : [];
      });
      if (observed.length) onSeen(observed);
    }, { root: element, threshold: 0.25 });
    element.querySelectorAll("[data-registration]").forEach(item => observer.observe(item));
    return () => observer.disconnect();
  }, [items, onSeen]);
  async function copyInstruction() {
    setCopyError(null);
    try { await navigator.clipboard.writeText(museInstruction()); setCopied(true); }
    catch { setShowInstruction(true); setCopyError("Clipboard unavailable. Select and copy the instruction below."); }
  }
  return <dialog ref={dialog} className={styles.registrations} aria-labelledby={titleId}
    onCancel={event => { event.preventDefault(); onClose(); }}
    onClick={event => { if (event.target === event.currentTarget) {
      const rect = event.currentTarget.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) onClose();
    } }}>
    <header className={styles.heading}>
      <div><p className={styles.eyebrow}>MUSE</p><h2 id={titleId}>Registrations</h2></div>
      <button type="button" className={styles.iconButton} onClick={onClose} aria-label="Close registrations"><X aria-hidden="true" /></button>
    </header>
    <div className={styles.handoff}>
      <p>Your queued events are saved. Copy the instruction into Muse to start or continue. Muse asks you for logins and missing answers.</p>
      <div className={styles.actions}>
        <button type="button" className="button" onClick={copyInstruction} aria-label="Copy Muse instruction">
          {copied ? <Check aria-hidden="true" /> : <Clipboard aria-hidden="true" />}{copied ? "Copied" : "Copy instruction"}
        </button>
        <a className="button button-primary" href={MUSE_URL} target="_blank" rel="noopener noreferrer"><MuseIcon />Open Muse</a>
      </div>
      <p className={styles.connection}><Link href="/settings/muse">Muse connection settings</Link>
        <button type="button" onClick={() => setShowInstruction(current => !current)} aria-expanded={showInstruction}>View instruction</button></p>
      {showInstruction ? <textarea className={styles.instruction} aria-label="Muse signup instruction" readOnly value={museInstruction()} /> : null}
      {copied ? <span role="status" className="sr-only">Muse instruction copied.</span> : null}
      {copyError ? <p role="alert">{copyError}</p> : null}
    </div>
    <div className={styles.listHeading}><span>{total} {total === 1 ? "registration" : "registrations"}</span>
      <button type="button" className={styles.iconButton} onClick={onRefresh} disabled={loading} aria-label="Refresh registrations">
        <RefreshCw className={loading ? "spin" : undefined} aria-hidden="true" />
      </button>
    </div>
    {error ? <p role="alert" className="workspace-error">{error}</p> : null}
    {loading && !items.length ? <p role="status">Loading registrations…</p> : null}
    {!loading && !items.length && !error ? <div className={styles.empty}><MuseIcon /><h3>No registrations yet</h3>
      <p>Use the Muse icon on a free Luma or Meetup event to queue it.</p></div> : null}
    <ol className={styles.tasks}>{items.map(item => <li key={item.event.canonical_event_id}
      data-registration={item.event.canonical_event_id} className={selectedId === item.event.canonical_event_id ? styles.selectedTask : undefined}>
      <div className={styles.taskTitle}><a href={museProviderUrl(item.event.registration_url) ?? undefined} target="_blank" rel="noopener noreferrer">
        {item.event.title}<ExternalLink aria-hidden="true" /></a>
        {item.unread ? <span className={styles.unreadDot} aria-label="Unread update" /> : null}</div>
      <p className={styles.taskDate}>{museEventDate(item.event.start_at)}{item.event.city ? ` · ${item.event.city}` : ""}</p>
      <p className={styles.status} data-status={item.status}><span aria-hidden="true" />{museStatusLabel[item.status]}</p>
      {item.status === "queued" ? <p>Waiting for you to start Muse.</p> : null}
      {item.status === "needs_input" ? <p>Open Muse to provide the requested information.</p> : null}
      {item.status === "uncertain" ? <p>Muse must check the provider before another submission.</p> : null}
      {item.outcome?.note ? <p>{item.outcome.note}</p> : null}
      {item.outcome?.confirmation_reference ? <p>Confirmation: {item.outcome.confirmation_reference}</p> : null}
      <div className={styles.taskLinks}><Link href={`/?view=events&when=all&city=&q=${encodeURIComponent(item.event.title)}&event=${encodeURIComponent(item.event.canonical_event_id)}`}>Event details</Link>
        {item.outcome?.evidence_url && museProviderUrl(item.outcome.evidence_url) ? <a href={item.outcome.evidence_url} target="_blank" rel="noopener noreferrer">Provider evidence</a> : null}
      </div>
      <time className={styles.updated} dateTime={item.updated_at}>Updated {museEventDate(item.updated_at)}</time>
    </li>)}</ol>
    {hasMore ? <button type="button" className="button" disabled={loading} onClick={onLoadMore}>
      {loading ? "Loading…" : "Load more registrations"}</button> : null}
  </dialog>;
}
