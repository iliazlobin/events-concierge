"use client";

import { Check, Copy, KeyRound, LoaderCircle, Trash2 } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";

import { useSession } from "@/components/session-provider";
import { SettingsHelp } from "@/components/settings/settings-help";
import { createApiKey, getApiKeys, readableError, revokeApiKey } from "@/lib/api";
import { releaseProfile } from "@/lib/release-profile";
import type { ApiKey } from "@/lib/types";

function when(value: string | null): string {
  if (!value) return "never";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) return value;
  return parsed.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/**
 * Issue and revoke API keys.
 *
 * The secret is displayed exactly once, in the response to creation, because only its digest is
 * stored. That single-shot reveal is the whole reason this panel keeps a dedicated piece of state
 * for a freshly created key rather than just refetching the list.
 */
export function ApiKeysPanel() {
  const { tenantId, config } = useSession();
  const fullRelease = releaseProfile(config) === "full";
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [creating, setCreating] = useState(false);
  const [issued, setIssued] = useState<{ secret: string; name: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [revoking, setRevoking] = useState<string | null>(null);

  useEffect(() => {
    if (!fullRelease) return;
    let cancelled = false;
    void getApiKeys(tenantId)
      .then((rows) => {
        if (!cancelled) setKeys(rows);
      })
      .catch((loadError: unknown) => {
        if (!cancelled) setError(readableError(loadError, "Your keys could not be loaded."));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [fullRelease, tenantId]);

  const create = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setError("Enter a key name.");
      return;
    }
    setCreating(true);
    setError(null);
    try {
      const result = await createApiKey(trimmed, tenantId);
      setKeys((current) => [result.key, ...current]);
      setIssued({ secret: result.secret, name: result.key.name });
      setCopied(false);
      setName("");
    } catch (createError) {
      setError(readableError(createError, "That key could not be created."));
    } finally {
      setCreating(false);
    }
  };

  const revoke = async (key: ApiKey) => {
    setRevoking(key.key_id);
    setError(null);
    try {
      const updated = await revokeApiKey(key.key_id, tenantId);
      setKeys((current) => current.map((row) => (row.key_id === updated.key_id ? updated : row)));
    } catch (revokeError) {
      setError(readableError(revokeError, "That key could not be revoked."));
    } finally {
      setRevoking(null);
    }
  };

  const copySecret = async () => {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(issued.secret);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 3000);
    } catch {
      // Clipboard access can be denied; the secret is on screen and selectable regardless.
      setError("Copy was blocked. Select the key and copy it manually.");
    }
  };

  if (!fullRelease) {
    return <div className="settings-panel">
      <h1>Account access</h1>
      <p className="settings-hint">API keys are not enabled.</p>
      <Link href="/settings/account">Account</Link>
    </div>;
  }

  return (
    <div className="settings-panel">
      <header className="settings-panel__head">
        <h1>API keys</h1>
        <Link className="settings-guide-link" href="/docs/account#access">Guide <span aria-hidden="true">→</span></Link>
      </header>
      <p className="settings-hint">Development records. Authentication is not enabled.</p>

      {issued ? (
        <section className="settings-card settings-card--reveal">
          <div className="settings-title-row">
            <h2 className="settings-card__title">{issued.name} <span className="settings-fixed">Shown once</span></h2>
            <SettingsHelp label="New API key">Save this key before hiding it. Only its fingerprint is stored, so the key cannot be retrieved later.</SettingsHelp>
          </div>
          <div className="secret-reveal">
            <code>{issued.secret}</code>
            <button type="button" className="button button-quiet" onClick={copySecret}>
              {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
              {copied ? "Copied" : "Copy"}
            </button>
          </div>
          <button
            type="button"
            className="button button-primary"
            onClick={() => setIssued(null)}
          >
            Hide key
          </button>
        </section>
      ) : null}

      <section className="settings-card">
        <h2 className="settings-card__title">New key</h2>
        <div className="settings-inline-add">
          <input
            type="text"
            value={name}
            maxLength={64}
            placeholder="Key name"
            aria-label="Key name"
            onChange={(event) => setName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                void create();
              }
            }}
          />
          <button
            type="button"
            className="button button-primary"
            disabled={creating || name.trim() === ""}
            onClick={create}
          >
            {creating ? <LoaderCircle className="spin" aria-hidden="true" /> : null}
            Create key
          </button>
        </div>
      </section>

      {error ? (
        <p className="settings-error" role="alert">
          {error}
        </p>
      ) : null}

      <section className="settings-card">
        <h2 className="settings-card__title">Keys</h2>
        {loading ? (
          <p className="settings-hint">Loading…</p>
        ) : keys.length === 0 ? (
          <p className="settings-empty">
            <KeyRound aria-hidden="true" />
            No keys yet.
          </p>
        ) : (
          <ul className="key-list">
            {keys.map((key) => (
              <li key={key.key_id} className={key.active ? "" : "is-revoked"}>
                <div className="key-list__main">
                  <p className="key-list__name">
                    {key.name}
                    {key.active ? null : <span className="key-list__badge">Revoked</span>}
                  </p>
                  <p className="key-list__meta">
                    <code>{key.key_prefix}…</code> · created {when(key.created_at)} · last used{" "}
                    {when(key.last_used_at)}
                  </p>
                </div>
                {key.active ? (
                  <button
                    type="button"
                    className="button button-quiet-danger"
                    disabled={revoking === key.key_id}
                    onClick={() => revoke(key)}
                  >
                    <Trash2 aria-hidden="true" />
                    {revoking === key.key_id ? "Revoking…" : "Revoke"}
                  </button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
