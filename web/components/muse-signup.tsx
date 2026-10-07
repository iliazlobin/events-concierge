"use client";

import { useEffect, useId, useRef, useState } from "react";
import Link from "next/link";
import { readableError } from "@/lib/api";
import {
  MUSE_URL, getMuseConnection, museInstruction, museStatusLabel, prepareMuseBatch,
  type MuseBatch, type MuseConnection,
} from "@/lib/muse";
import type { EventItem } from "@/lib/types";
import styles from "./muse.module.css";

interface Props {
  events: EventItem[];
  tenantId: string | null;
  signedIn: boolean;
  signInUrl: string;
  onSignIn: () => void;
  onClose: () => void;
  onRemove: (eventId: string) => void;
}

export function MuseSignup({ events, tenantId, signedIn, signInUrl, onSignIn, onClose, onRemove }: Props) {
  const dialog = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const [requestId] = useState(() => crypto.randomUUID());
  const [connection, setConnection] = useState<MuseConnection | null>(null);
  const [batch, setBatch] = useState<MuseBatch | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
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
    let active = true;
    if (signedIn) getMuseConnection(tenantId).then(
      result => { if (active) setConnection(result); },
      problem => { if (active) setError(readableError(problem, "Could not check your Muse connection.")); },
    );
    return () => { active = false; };
  }, [signedIn, tenantId]);
  async function prepare() {
    setBusy(true); setError(null);
    try { setBatch(await prepareMuseBatch(tenantId, requestId, events.map(event => event.canonical_event_id))); }
    catch (problem) { setError(readableError(problem, "Could not prepare your signup batch.")); }
    finally { setBusy(false); }
  }
  async function copyInstruction() {
    if (!batch) return;
    try { await navigator.clipboard.writeText(museInstruction(batch.batch_id)); setCopied(true); }
    catch { setError("Clipboard unavailable. Select and copy the instruction below."); }
  }
  return <dialog ref={dialog} className={styles.dialog} aria-labelledby={titleId} onCancel={onClose}>
    <header className={styles.heading}><h2 id={titleId}>Sign up with Muse</h2>
      <button type="button" className="button" onClick={onClose} aria-label="Close Muse signup">Close</button>
    </header>
    <p>Muse uses its browser to complete these free event signups. Your provider logins stay with Muse.</p>
    <ul className={styles.items}>
      {(batch ? batch.items.map(item => ({ id: item.event.canonical_event_id, title: item.event.title,
        date: item.event.start_at, status: museStatusLabel[item.status] }))
        : events.map(event => ({ id: event.canonical_event_id, title: event.title, date: event.start_at, status: null })))
        .map(item => <li key={item.id}><div><strong>{item.title}</strong>
          <p>{new Date(item.date).toLocaleString(undefined, { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" })}</p>
          {item.status ? <p>{item.status}</p> : null}</div>
          {!batch ? <button type="button" className="button" disabled={busy} onClick={() => onRemove(item.id)}
            aria-label={`Remove ${item.title}`}>Remove</button> : null}
        </li>)}
    </ul>
    {error ? <p role="alert" className="workspace-error">{error}</p> : null}
    {!signedIn ? <Link className="button button-primary" href={signInUrl} onClick={onSignIn}>Sign in to use Muse</Link> : !batch ? <>
      {connection && !connection.connected ? <p><Link href="/settings/muse">Set up your Muse connection</Link> first. You can save this selection now.</p> : null}
      <button type="button" className="button button-primary" disabled={busy || !events.length}
        onClick={prepare}>{busy ? "Preparing…" : "Prepare signup batch"}</button>
    </> : <>
      <p>Batch saved. Copy the instruction and paste it into Muse to start.
        <Link href="/settings/muse"> View progress and connection settings</Link>.</p>
      <textarea className={styles.instruction} aria-label="Muse signup instruction" readOnly value={museInstruction(batch.batch_id)} />
      <div className={styles.actions}>
        <button type="button" className="button" onClick={copyInstruction}>{copied ? "Instruction copied" : "Copy instruction"}</button>
        <a className="button button-primary" href={MUSE_URL} target="_blank" rel="noopener noreferrer">Open Muse</a>
      </div>
    </>}
  </dialog>;
}
