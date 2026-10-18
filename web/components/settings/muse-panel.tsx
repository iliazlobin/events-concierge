"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { useSession } from "@/components/session-provider";
import { readableError } from "@/lib/api";
import { SettingsHelp } from "@/components/settings/settings-help";
import {
  MUSE_URL, createMuseConnection, getMuseConnection, revokeMuseConnection, type MuseConnection,
} from "@/lib/muse";
import styles from "./muse-panel.module.css";

const CLIPBOARD_ERROR = "Clipboard unavailable. Copy the field manually.";

export function MuseConnectionPanel({ purpose = "registrations" }: { purpose?: "registrations" | "discovery" }) {
  const discovery = purpose === "discovery";
  const { tenantId } = useSession();
  const [connection, setConnection] = useState<MuseConnection | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; inline: boolean } | null>(null);
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
      setMessage({ text: "Key revoked.", inline: true });
    } catch (problem) { setError(readableError(problem, "Could not revoke the connection key.")); }
    finally { setBusy(false); }
  }
  async function copy(value: string, label: string) {
    try {
      await navigator.clipboard.writeText(value);
      setError(current => current === CLIPBOARD_ERROR ? null : current);
      setMessage({ text: label, inline: false });
    }
    catch { setMessage(null); setError(CLIPBOARD_ERROR); }
  }
  const schema = origin ? `${origin}/v1/muse/openapi.json` : "";
  const mcp = origin ? `${origin}/v1/muse/mcp` : "";
  return <div className="settings-panel">
    {error ? <p role="alert" className="settings-error">{error}</p> : null}
    <p role="status" aria-live="polite" aria-atomic="true" className={message?.inline ? "settings-notice" : "sr-only"}>{message?.text}</p>
    <section className="settings-card" aria-labelledby="muse-connection-heading">
      <h2 id="muse-connection-heading" className="settings-card__title">Connection</h2>
      <dl className={`settings-facts ${styles.facts}`}>
        <div><dt>Connection key <SettingsHelp label="Connection key">Authenticates catalog discovery and the signup connector for 30 days. An active key does not confirm Muse is connected.</SettingsHelp></dt>
          <dd>{connection ? connection.connected ? "Active" : "No active key" : error ? "Unavailable" : "Loading…"}
            {!connection && error ? <button type="button" className={styles.textButton} onClick={() => void refresh()}>Retry</button> : null}</dd></div>
        {connection?.connected && connection.expires_at ? <div><dt>Expires</dt><dd>{new Date(connection.expires_at).toLocaleDateString()}</dd></div> : null}
      </dl>
      {discovery ? <>
      <div className={`settings-field ${styles.connector}`}>
        <div className={styles.fieldLabel}>
          <label htmlFor="muse-mcp-url">MCP URL</label>
          <SettingsHelp label="MCP URL">For clients supporting Streamable HTTP MCP. Use the same Bearer key. Provides read-only event search and details; signups use the connector above.</SettingsHelp>
        </div>
        <div className={styles.copyField}>
          <input id="muse-mcp-url" readOnly value={mcp} />
          <button type="button" className="button" disabled={!origin} onClick={() => copy(mcp, "MCP URL copied.")}>Copy MCP URL</button>
        </div>
        <Link className={styles.link} href="/docs/mcp#connect">MCP setup <span aria-hidden="true">→</span></Link>
      </div>
      </> : <>
      <div className={`settings-field ${styles.connector}`}>
        <div className={styles.fieldLabel}>
          <label htmlFor="muse-connector-url">Connector URL</label>
          <SettingsHelp label="Connector URL">Use this URL in Muse’s connector settings. A public HTTPS address is required; local previews cannot connect.</SettingsHelp>
        </div>
        <div className={styles.copyField}>
          <input id="muse-connector-url" readOnly value={schema} />
          <button type="button" className="button" disabled={!origin} onClick={() => copy(schema, "URL copied.")}>Copy URL</button>
        </div>
      </div>
      </>}
      <div className={styles.actions}>
        <button type="button" className="button button-primary" disabled={busy || !connection} onClick={createKey}>
          {connection?.connected ? "Replace key" : "Create key"}</button>
        {connection?.connected ? <button type="button" className="button" disabled={busy} onClick={revoke}>Revoke key</button> : null}
        <SettingsHelp label="Key actions">Replacing or revoking a key removes access with the old key. Tasks already running in Muse must be stopped there.</SettingsHelp>
      </div>
      {token ? <div className={`settings-field ${styles.keyReveal}`}>
        <label htmlFor="muse-connection-key">Connection key <span className="settings-fixed">Shown once</span></label>
        <input id="muse-connection-key" type="password" aria-label="Muse connection key" autoComplete="off" readOnly value={token} />
        <div className={styles.actions}>
          <button type="button" className="button" onClick={() => copy(token, "Key copied.")}>Copy connection key</button>
          <button type="button" className="button" onClick={() => setToken(null)}>Hide key</button>
        </div>
      </div> : null}
    </section>
    <nav aria-label="Muse links" className={styles.links}>
      {discovery ? <Link className={styles.link} href="/settings/registrations">Registration settings <span aria-hidden="true">→</span></Link> : <Link className={styles.link} href="/settings/mcp">MCP discovery <span aria-hidden="true">→</span></Link>}
      {!discovery ? <Link className={styles.link} href="/?registrations=1">Registrations <span aria-hidden="true">→</span></Link> : null}
      {!discovery ? <a className={styles.link} href={MUSE_URL} target="_blank" rel="noopener noreferrer">Open Muse <span aria-hidden="true">↗</span></a> : null}
    </nav>
  </div>;
}
