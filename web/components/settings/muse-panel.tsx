"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useSession } from "@/components/session-provider";
import { readableError } from "@/lib/api";
import {
  MUSE_URL, museInstruction, createMuseConnection, getMuseConnection, revokeMuseConnection, type MuseConnection,
} from "@/lib/muse";
import styles from "@/components/muse.module.css";

export function MusePanel() {
  const { config } = useSession();
  if (config?.muse_enabled !== true) return <section className={styles.panel}>
    <header><h1>Muse signups</h1><p>Unavailable.</p></header>
  </section>;
  return <EnabledMusePanel />;
}

function EnabledMusePanel() {
  const { tenantId } = useSession();
  const [connection, setConnection] = useState<MuseConnection | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [origin, setOrigin] = useState("");
  const refresh = useCallback(async () => {
    setError(null);
    try {
      setConnection(await getMuseConnection(tenantId));
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
      setMessage("Access revoked. Stop running tasks in Muse.");
    } catch (problem) { setError(readableError(problem, "Could not revoke the connection key.")); }
    finally { setBusy(false); }
  }
  async function copy(value: string, label: string) {
    try { await navigator.clipboard.writeText(value); setMessage(label); }
    catch { setError("Clipboard unavailable. Copy the field manually."); }
  }
  const schema = `${origin}/v1/muse/openapi.json`;
  return <section className={styles.panel}>
    <header><h1>Muse signups</h1></header>
    {error ? <p role="alert" className="workspace-error">{error}</p> : null}
    {message ? <p role="status">{message}</p> : null}
    <section><h2>Connection</h2>
      <p>Add this URL in Muse’s connector settings with Bearer authentication. Enter the key in credential settings, not chat.</p>
      <label>API description<input aria-label="Muse API description" readOnly value={schema} /></label>
      <p>Requires a public HTTPS URL. Local previews are unavailable to Muse.</p>
      {connection ? <p>{connection.connected
        ? `Key expires ${new Date(connection.expires_at!).toLocaleDateString()}.`
        : "No active key."}</p> : <p role="status">Loading…</p>}
      <div className={styles.actions}>
        <button type="button" className="button button-primary" disabled={busy || !connection} onClick={createKey}>
          {connection?.connected ? "Replace connection key" : "Create connection key"}</button>
        {connection?.connected ? <button type="button" className="button" disabled={busy} onClick={revoke}>Disconnect Muse</button> : null}
        <a className="button" href={MUSE_URL} target="_blank" rel="noopener noreferrer">Open Muse</a>
      </div>
      {token ? <div>
        <p>Access to your registrations for 30 days. Copy now; shown once. Replacing it revokes the old key.</p>
        <input type="password" aria-label="Muse connection key" autoComplete="off" readOnly value={token} />
        <div className={styles.actions}>
          <button type="button" className="button" onClick={() => copy(token, "Key copied.")}>Copy connection key</button>
          <button type="button" className="button" onClick={() => setToken(null)}>Hide key</button>
        </div>
      </div> : null}
    </section>
    <MuseInstructions />
    <section>
      <Link className="button" href="/?registrations=1">Registrations</Link>
    </section>
  </section>;
}

function MuseInstructions() {
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function copyInstruction() {
    setCopied(false); setError(null);
    try { await navigator.clipboard.writeText(museInstruction()); setCopied(true); }
    catch { setError("Clipboard unavailable. Select and copy the instruction below."); }
  }
  return <details className={styles.instructions}>
    <summary>Muse instructions</summary>
    <p>Paste into Muse to start your queue. Opening Muse does not start registrations.</p>
    {error ? <p role="alert">{error}</p> : null}
    <textarea className={styles.instruction} aria-label="Muse signup instruction" readOnly value={museInstruction()} />
    <div className={styles.actions}>
      <button type="button" className="button" aria-label="Copy Muse instruction" onClick={copyInstruction}>Copy instruction</button>
    </div>
    {copied ? <p role="status">Muse instruction copied.</p> : null}
  </details>;
}
