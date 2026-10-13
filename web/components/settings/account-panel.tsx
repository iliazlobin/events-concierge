"use client";

import { LoaderCircle } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { useSession } from "@/components/session-provider";
import {
  ApiError,
  clearSession,
  readableError,
  requestAccountErasure,
  startReauthentication,
} from "@/lib/api";
import { clearCatalogCache } from "@/lib/catalog-cache";
import { clearEntityGraphCache } from "@/lib/entity-graph-cache";
import { accountDeletionUnavailable } from "@/lib/sign-in";
import type { AccountErasureReceipt } from "@/lib/types";
import { SettingsHelp } from "./settings-help";

const CONFIRMATION = "DELETE MY ACCOUNT";
const ERASURE_REQUEST_KEY = "events-concierge.erasure-request.v1";

/**
 * One erasure request id per browser, replayed on retry.
 *
 * A fresh id against a live erasure is a conflict, not a second attempt, so the id has to survive a
 * lost response. It is deliberately session-scoped: it should not outlive the tab that started it.
 */
function erasureRequestId(): string {
  try {
    const existing = window.sessionStorage.getItem(ERASURE_REQUEST_KEY);
    if (existing) return existing;
    const minted = crypto.randomUUID();
    window.sessionStorage.setItem(ERASURE_REQUEST_KEY, minted);
    return minted;
  } catch {
    return crypto.randomUUID();
  }
}

export function AccountPanel() {
  const { me, config, tenantId } = useSession();
  const [confirmation, setConfirmation] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<AccountErasureReceipt | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const deletionUnavailable = accountDeletionUnavailable(config);

  const erase = async () => {
    if (deletionUnavailable) return;
    setBusy(true);
    setError(null);
    try {
      const result = await requestAccountErasure(erasureRequestId(), tenantId);
      // The server clears its own cookies but cannot reach browser storage; leaving the tenant
      // reference behind would let the next visitor to this browser resume a deleted account.
      clearSession();
      clearCatalogCache();
      clearEntityGraphCache();
      setReceipt(result);
      setDialogOpen(false);
    } catch (eraseError) {
      if (eraseError instanceof ApiError && eraseError.status === 428 && config?.reauth_url) {
        try {
          const hop = await startReauthentication(config.reauth_url, "/settings/account");
          window.location.assign(hop.authorization_url);
          return;
        } catch (hopError) {
          setError(readableError(hopError, "Sign in again to continue."));
        }
      } else {
        setError(readableError(eraseError, "The request was not accepted."));
      }
    } finally {
      setBusy(false);
    }
  };

  if (receipt) {
    return (
      <div className="settings-panel">
        <header className="settings-panel__head">
          <h1>{receipt.status === "completed" ? "Account deleted" : "Deleting account"}</h1>
          <p className="settings-hint">
            {receipt.status === "completed"
              ? "Cleanup completed."
              : "Request accepted. Cleanup continues."}
          </p>
        </header>
        <section className="settings-card">
          <dl className="settings-facts">
            <div>
              <dt>Concierge work cancelled</dt>
              <dd>
                {receipt.workflows_cancelled} of {receipt.workflow_targets}
              </dd>
            </div>
            <div>
              <dt>Calendar entries removed</dt>
              <dd>
                {receipt.calendar_deleted} of {receipt.calendar_targets}
              </dd>
            </div>
            <div>
              <dt>Sessions revoked</dt>
              <dd>{receipt.browser_sessions_revoked ? "Yes" : "In progress"}</dd>
            </div>
            <div>
              <dt>Stored credentials purged</dt>
              <dd>{receipt.credential_vault_purged ? "Yes" : "In progress"}</dd>
            </div>
            <div>
              <dt>Records kept for audit</dt>
              <dd>{receipt.retained_audit_rows}</dd>
            </div>
          </dl>
        </section>
        <a className="button button-primary" href="/">
          Done
        </a>
      </div>
    );
  }

  return (
    <div className="settings-panel">
      <header className="settings-panel__head">
        <h1>Account</h1>
        <Link className="settings-guide-link" href="/docs/account#access">Guide <span aria-hidden="true">→</span></Link>
      </header>

      <section className="settings-card">
        <h2 className="settings-card__title">Sign-in</h2>
        <dl className="settings-facts">
          <div>
            <dt>Email</dt>
            <dd>{me?.notify_email ?? "—"}</dd>
          </div>
          <div>
            <dt className="settings-field-label">Sign-in method
              {config?.auth_mode === "deployment_session" ? <SettingsHelp label="Session">Sessions last up to eight hours and do not extend as you use them. Account deletion requires a recent identity check when supported.</SettingsHelp> : null}
            </dt>
            <dd>
              {config?.auth_mode === "deployment_session"
                ? config.auth_provider === "google" ? "Google" : "Single sign-on"
                : "Local demo session"}
            </dd>
          </div>
        </dl>
      </section>

      <section className="settings-card settings-card--danger">
        <div className="settings-title-row"><h2 className="settings-card__title">Delete account</h2>
          <Link className="settings-guide-link" href="/docs/account#deletion">Details <span aria-hidden="true">→</span></Link>
        </div>
        <p className="settings-hint">This cannot be undone.</p>
        {deletionUnavailable ? (
          <div className="settings-field-label"><p className="settings-hint" id="deletion-unavailable">Deletion unavailable.</p>
            <SettingsHelp label="Account deletion">Deletion is unavailable with this Google sign-in configuration. The required identity check is not supported; signing in again will not enable deletion.</SettingsHelp>
          </div>
        ) : null}
        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}
        {dialogOpen ? (
          <div className="danger-confirm">
            <p className="settings-hint">This ends account access, revokes sessions and connections, cancels pending work and starts deletion of account data. A limited audit record may remain.</p>
            <label htmlFor="erasure-confirm">
              Type <code>{CONFIRMATION}</code> to confirm.
            </label>
            <input
              id="erasure-confirm"
              type="text"
              autoComplete="off"
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
            />
            <div className="danger-confirm__actions">
              <button
                type="button"
                className="button button-quiet"
                onClick={() => {
                  setDialogOpen(false);
                  setConfirmation("");
                }}
              >
                Cancel
              </button>
              <button
                type="button"
                className="button button-quiet-danger"
                disabled={confirmation !== CONFIRMATION || busy || deletionUnavailable}
                onClick={erase}
              >
                {busy ? <LoaderCircle className="spin" aria-hidden="true" /> : null}
                Delete account
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            className="button button-quiet-danger"
            disabled={deletionUnavailable}
            aria-describedby={deletionUnavailable ? "deletion-unavailable" : undefined}
            onClick={() => setDialogOpen(true)}
          >
            Delete account…
          </button>
        )}
      </section>
    </div>
  );
}
