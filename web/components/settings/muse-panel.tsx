"use client";

import { useCallback, useEffect, useState } from "react";
import { useSession } from "@/components/session-provider";
import { readableError } from "@/lib/api";
import {
  MUSE_URL, createMuseConnection, getMuseBatches, getMuseConnection, museInstruction,
  museProviderUrl, museStatusLabel, revokeMuseConnection, type MuseBatch, type MuseConnection,
} from "@/lib/muse";
import styles from "@/components/muse.module.css";

export function MusePanel() {
  const { tenantId } = useSession();
  const [connection, setConnection] = useState<MuseConnection | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [batches, setBatches] = useState<MuseBatch[]>([]);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [origin, setOrigin] = useState("");
  const refresh = useCallback(async () => {
    setError(null);
    try {
      const [current, latest] = await Promise.all([getMuseConnection(tenantId), getMuseBatches(tenantId)]);
      setConnection(current); setBatches(latest);
    } catch (problem) { setError(readableError(problem, "Could not load Muse settings.")); }
  }, [tenantId]);
  useEffect(() => { setOrigin(window.location.origin); setToken(null); void refresh(); }, [refresh]);
  async function createKey() {
    setBusy(true); setError(null); setMessage(null); setToken(null);
    try {
      const issued = await createMuseConnection(tenantId);
      setToken(issued.token); setConnection({ connected: true, expires_at: issued.expires_at });
    } catch (problem) { setError(readableError(problem, "Could not create the connection key.")); }
    finally { setBusy(false); }
  }
  async function revoke() {
    setBusy(true); setError(null);
    try {
      await revokeMuseConnection(tenantId); setToken(null); setConnection({ connected: false, expires_at: null });
      setMessage("Connector access revoked. Stop any running browser task in Muse separately.");
    } catch (problem) { setError(readableError(problem, "Could not revoke the connection key.")); }
    finally { setBusy(false); }
  }
  async function copy(value: string, label: string) {
    try { await navigator.clipboard.writeText(value); setMessage(label); }
    catch { setError("Clipboard unavailable. Copy the field manually."); }
  }
  const schema = `${origin}/v1/muse/openapi.json`;
  return <section className={styles.panel}>
    <header><h1>Muse signups</h1><p>Select events in Events, then complete signups using Muse's browser.</p></header>
    {error ? <p role="alert" className="workspace-error">{error}</p> : null}
    {message ? <p role="status">{message}</p> : null}
    <section><h2>Connection</h2>
      <p>Ask Muse to create a custom connector from this API description. Enter the connection key only in its secure credential setup, using Bearer authentication.</p>
      <label>API description<input aria-label="Muse API description" readOnly value={schema} /></label>
      <p>This requires the deployed HTTPS address; Muse's cloud browser cannot reach a local preview.</p>
      {connection ? <p>{connection.connected
        ? `Connection key active until ${new Date(connection.expires_at!).toLocaleDateString()}. This does not verify Muse setup.`
        : "No active connection key."}</p> : <p role="status">Loading connection…</p>}
      <div className={styles.actions}>
        <button type="button" className="button button-primary" disabled={busy || !connection} onClick={createKey}>
          {connection?.connected ? "Replace connection key" : "Create connection key"}</button>
        {connection?.connected ? <button type="button" className="button" disabled={busy} onClick={revoke}>Disconnect Muse</button> : null}
        <a className="button" href={MUSE_URL} target="_blank" rel="noopener noreferrer">Open Muse</a>
      </div>
      {token ? <div>
        <p>This key grants access only to your signup batches for 30 days. Copy it now; it cannot be recovered. Replacing it revokes the previous key.</p>
        <input type="password" aria-label="Muse connection key" autoComplete="off" readOnly value={token} />
        <div className={styles.actions}>
          <button type="button" className="button" onClick={() => copy(token, "Connection key copied. Enter it in Muse's secure credential setup.")}>Copy connection key</button>
          <button type="button" className="button" onClick={() => setToken(null)}>Hide key</button>
        </div>
      </div> : null}
    </section>
    <section><div className={styles.heading}><h2>Signup batches</h2>
      <button type="button" className="button" onClick={refresh}>Refresh results</button></div>
      {!batches.length ? <p>No signup batches yet. Choose free Luma or Meetup events to begin.</p> : null}
      {batches.map(batch => <article key={batch.batch_id}>
        <h3>{new Date(batch.created_at).toLocaleString()}</h3>
        <ul className={styles.items}>{batch.items.map(item => <li key={item.event.canonical_event_id}><div>
          <a href={item.event.registration_url} target="_blank" rel="noopener noreferrer">{item.event.title}</a>
          <p>{museStatusLabel[item.status]}</p>
          {item.outcome?.note ? <p>{item.outcome.note}</p> : null}
          {item.outcome?.confirmation_reference ? <p>Confirmation: {item.outcome.confirmation_reference}</p> : null}
          {item.outcome?.evidence_url && museProviderUrl(item.outcome.evidence_url) ?
            <a href={item.outcome.evidence_url} target="_blank" rel="noopener noreferrer">Provider evidence</a> : null}
        </div></li>)}</ul>
        <button type="button" className="button" onClick={() => copy(museInstruction(batch.batch_id), "Signup instruction copied.")}>Copy Muse instruction</button>
      </article>)}
    </section>
  </section>;
}
