"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useSession } from "@/components/session-provider";
import { readableError } from "@/lib/api";
import { SettingsHelp } from "@/components/settings/settings-help";
import {
  MUSE_URL, museInstruction, createMuseConnection, getMuseConnection, revokeMuseConnection, type MuseConnection,
} from "@/lib/muse";
import styles from "@/components/muse.module.css";

export function MusePanel() {
  const { config } = useSession();
  if (config?.muse_enabled !== true) return <section className="settings-panel">
    <header className="settings-panel__head"><h1>Muse signups</h1><p>Unavailable.</p></header>
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
      setMessage("Key revoked. Stop running tasks in Muse.");
    } catch (problem) { setError(readableError(problem, "Could not revoke the connection key.")); }
    finally { setBusy(false); }
  }
  async function copy(value: string, label: string) {
    try { await navigator.clipboard.writeText(value); setMessage(label); }
    catch { setError("Clipboard unavailable. Copy the field manually."); }
  }
  const schema = `${origin}/v1/muse/openapi.json`;
  const localPreview = /^https?:\/\/(localhost|127\.0\.0\.1|\[::1\])(?::|$)/.test(origin);
  return <section className="settings-panel">
    <header className="settings-panel__head"><h1>Muse signups</h1></header>
    {error ? <p role="alert" className="settings-error">{error}</p> : null}
    {message ? <p role="status" className="settings-notice">{message}</p> : null}
    <section className="settings-card"><h2 className="settings-card__title">Connection</h2>
      <dl className={`settings-facts ${styles.facts}`}>
        <div><dt>Connection key <SettingsHelp label="Connection key">The key lets Muse read and update your registrations for 30 days. An active key does not verify the connection.</SettingsHelp></dt>
          <dd>{connection ? connection.connected ? "Active" : "No active key" : error ? "Unavailable" : "Loading…"}
            {!connection && error ? <button type="button" className={styles.textButton} onClick={() => void refresh()}>Retry</button> : null}</dd></div>
        {connection?.connected && connection.expires_at ? <div><dt>Expires</dt><dd>{new Date(connection.expires_at).toLocaleDateString()}</dd></div> : null}
        <div><dt>Hosting <SettingsHelp label="Hosting">Muse requires a public HTTPS address. Local previews are unavailable to Muse.</SettingsHelp></dt>
          <dd>{origin ? localPreview ? "Local preview" : origin.startsWith("https://") ? "HTTPS" : "HTTPS required" : "Loading…"}</dd></div>
      </dl>
      <div className={styles.settingsActions}>
        <button type="button" className="button button-primary" disabled={busy || !connection} onClick={createKey}>
          {connection?.connected ? "Replace key" : "Create key"}</button>
        <SettingsHelp label="Key actions">Replacing or revoking the key removes access with the old key. Stop any running tasks separately in Muse.</SettingsHelp>
        {connection?.connected ? <button type="button" className="button" disabled={busy} onClick={revoke}>Revoke key</button> : null}
        <a className="button" href={MUSE_URL} target="_blank" rel="noopener noreferrer">Open Muse</a>
      </div>
      {token ? <div className={`settings-field ${styles.keyReveal}`}>
        <label htmlFor="muse-connection-key">Connection key <span className="settings-fixed">Shown once</span></label>
        <input id="muse-connection-key" type="password" aria-label="Muse connection key" autoComplete="off" readOnly value={token} />
        <div className={styles.settingsActions}>
          <button type="button" className="button" onClick={() => copy(token, "Key copied.")}>Copy connection key</button>
          <button type="button" className="button" onClick={() => setToken(null)}>Hide key</button>
        </div>
      </div> : null}
    </section>
    <details className={`settings-card ${styles.setup}`}>
      <summary>Connector setup</summary>
      <div className="settings-field">
        <div className={styles.fieldLabel}><label htmlFor="muse-connector-url">Connector URL</label>
          <SettingsHelp label="Connector URL">Add this URL in Muse’s connector settings. It describes the registration tools Muse can use.</SettingsHelp></div>
        <div className={styles.copyField}>
          <input id="muse-connector-url" readOnly value={schema} />
          <button type="button" className="button" onClick={() => copy(schema, "URL copied.")}>Copy URL</button>
        </div>
      </div>
      <dl className={`settings-facts ${styles.facts}`}><div><dt>Authentication <SettingsHelp label="Authentication">Paste the connection key into Muse’s Bearer token field.</SettingsHelp></dt><dd>Bearer token</dd></div></dl>
    </details>
    <MuseInstructions />
    <Link className={styles.registrationLink} href="/?registrations=1">Registrations <span aria-hidden="true">→</span></Link>
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
  return <details className={`settings-card ${styles.setup}`}>
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
