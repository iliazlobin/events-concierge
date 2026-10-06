"use client";

import { LoaderCircle } from "lucide-react";
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
import { releaseProfile } from "@/lib/release-profile";
import { accountDeletionUnavailable } from "@/lib/sign-in";
import type { AccountErasureReceipt } from "@/lib/types";

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
          <h1>Account erasure is underway</h1>
          <p>
            {receipt.status === "completed"
              ? "Everything below has been removed."
              : "We have accepted the request and started work. It continues without this browser."}
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
        <h1>Account and data</h1>
        <p>Where your session stands, and how to end it for good.</p>
      </header>

      <section className="settings-card">
        <h2 className="settings-card__title">Sign-in</h2>
        <dl className="settings-facts">
          <div>
            <dt>Email on file</dt>
            <dd>{me?.notify_email ?? "—"}</dd>
          </div>
          <div>
            <dt>Sign-in method</dt>
            <dd>
              {config?.auth_mode === "deployment_session"
                ? config.auth_provider === "google" ? "Google" : "Single sign-on"
                : "Local demo session"}
            </dd>
          </div>
        </dl>
        {config?.auth_mode === "deployment_session" ? (
          <p className="settings-hint">
            Sessions last up to eight hours and do not extend as you use them.
            {!deletionUnavailable ? " Destructive actions ask you to sign in again first." : null}
          </p>
        ) : null}
      </section>

      {releaseProfile(config) === "full" ? <section className="settings-card">
        <h2 className="settings-card__title">How far the concierge goes</h2>
        <ol className="control-ladder">
          <li>
            <strong>Show me options.</strong> A read-only preview. It learns from what catches your
            eye and registers nothing.
          </li>
          <li>
            <strong>Find and handle it.</strong> A durable request. It may act inside allowed
            free-RSVP lanes and hands off anything else to you.
          </li>
          <li>
            <strong>Your calendar stays true.</strong> Confirmed registrations are reconciled when
            organizers change plans or you withdraw.
          </li>
        </ol>
        <p className="settings-hint">
          Paid registration is refused everywhere today — the concierge has no purchase authority.
        </p>
      </section> : <section className="settings-card">
        <h2 className="settings-card__title">Event discovery</h2>
        <p>Browse events, explore the map and calendar, and save your filters. Visit the event provider to register. Google Calendar links let you add an event yourself.</p>
      </section>}

      <section className="settings-card settings-card--danger">
        <h2 className="settings-card__title">Erase this account</h2>
        <p>
          This permanently fences your account, cancels concierge work, removes concierge-owned
          calendar entries, revokes sessions and stored credentials, and starts deletion from
          account-scoped storage. It cannot be undone.
        </p>
        {deletionUnavailable ? (
          <p className="settings-hint" id="deletion-unavailable">
            Account deletion is currently unavailable with Google sign-in. It requires an additional
            identity check that is not available yet. Signing in again will not enable deletion.
          </p>
        ) : null}
        {error ? (
          <p className="settings-error" role="alert">
            {error}
          </p>
        ) : null}
        {dialogOpen ? (
          <div className="danger-confirm">
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
                Erase my account
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
            Erase my account…
          </button>
        )}
      </section>
    </div>
  );
}
