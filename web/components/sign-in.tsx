"use client";

import { LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";

import { api, getUiConfig } from "@/lib/api";
import type { ConsumerProvider, prepareConsumerIdentity } from "@/lib/consumer-identity";
import { signInHref, signInMessage, type SignInReason } from "@/lib/sign-in";
import type { UiConfig } from "@/lib/types";
import styles from "./sign-in.module.css";

/** Reads public capabilities only. Cancellation, logout and errors must never start another login. */
export function SignIn({ reason, returnTo = "/", reauthenticationState }: {
  reason: SignInReason; returnTo?: string; reauthenticationState?: string;
}) {
  const [config, setConfig] = useState<UiConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [href, setHref] = useState<string | null>(null);
  const [identity, setIdentity] = useState<Awaited<ReturnType<typeof prepareConsumerIdentity>> | null>(null);
  const [challenge, setChallenge] = useState<string | null>(null);
  const [accepted, setAccepted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const legalRequired = config?.consumer_legal_mode !== "deferred";

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFailed(false);
    // A retry may load revised legal documents; previous consent cannot approve them.
    setAccepted(false);
    void getUiConfig().then(async (next) => {
      if (cancelled) return;
      setConfig(next);
      setHref(signInHref(next, window.location.origin));
      const legalConfigured = next.consumer_legal_mode === "deferred"
        ? !next.legal_policy : Boolean(next.legal_policy);
      if (next.auth_provider === "identity_platform" && next.identity_platform && legalConfigured) {
        const sdk = await import("@/lib/consumer-identity");
        const prepared = await sdk.prepareConsumerIdentity(next.identity_platform);
        const transaction = reauthenticationState ? { state: reauthenticationState }
          : await api<{ state: string }>(`/auth/identity/start?return_to=${encodeURIComponent(returnTo)}`);
        if (cancelled) { await prepared.clear(); return; }
        setIdentity(prepared);
        setChallenge(transaction.state);
      }
    }).catch(() => {
      if (cancelled) return;
      setConfig(null);
      setHref(null);
      setFailed(true);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [attempt, returnTo, reauthenticationState]);

  const signIn = async (provider: ConsumerProvider) => {
    if (!identity || !challenge || !config || (legalRequired && !config.legal_policy) || busy) return;
    setBusy(true);
    setError(null);
    try {
      const token = await identity.signIn(provider);
      const result = await api<{ return_to: string }>("/auth/identity/session", {
        method: "POST", bodyJson: {
          id_token: token, state: challenge,
          ...(legalRequired && config.legal_policy ? {
            accepted_terms: reauthenticationState ? false : accepted,
            terms_version: config.legal_policy.terms_version,
            privacy_version: config.legal_policy.privacy_version,
          } : {}),
        },
      });
      window.location.replace(result.return_to);
    } catch (failure) {
      const sdk = await import("@/lib/consumer-identity");
      setError(sdk.identityErrorMessage(failure));
      // Sign-in challenges are one-shot. A normal retry obtains a fresh cookie and state.
      if (!reauthenticationState) setAttempt(value => value + 1);
      else setChallenge(null);
    } finally {
      try { await identity.clear(); }
      catch { setError("Sign-in could not be cleared. Reload this page before trying again."); }
      finally { setBusy(false); }
    }
  };

  const message = signInMessage(reason);
  const title = reauthenticationState ? "Confirm your account"
    : config?.auth_provider === "identity_platform" && reason === null ? "Sign in or create an account" : message.title;
  const description = reauthenticationState ? "Sign in with the same account to confirm this change." : message.description;
  const action = config?.local_demo ? "Continue to local demo"
    : config?.auth_provider === "google" ? "Continue with Google" : "Continue to sign in";

  return (
    <main className={styles.page}>
      <section className={styles.card} aria-labelledby="sign-in-title">
        <span className="brand-symbol" aria-hidden="true"><i /><i /></span>
        <p className={styles.brand}>Events Concierge</p>
        <h1 id="sign-in-title">{title}</h1>
        <p className={styles.description}>{description}</p>
        <div className={styles.actions}>
          {loading ? (
            <p className={styles.loading} role="status"><LoaderCircle className="spin" aria-hidden="true" />Loading sign-in options…</p>
          ) : config?.auth_provider === "identity_platform" ? (
            <>
              {config.legal_policy && !reauthenticationState ? (
                <label className={styles.consent}>
                  <input type="checkbox" checked={accepted} onChange={event => setAccepted(event.target.checked)} />
                  <span>I accept the <a href={config.legal_policy.terms_url} target="_blank" rel="noopener noreferrer">Terms of Service</a> and acknowledge the <a href={config.legal_policy.privacy_url} target="_blank" rel="noopener noreferrer">Privacy Policy</a>.</span>
                </label>
              ) : null}
              <div className={styles.providers}>
                {config.identity_platform?.providers.map(provider => (
                  <button key={provider} className={`button ${styles.continue}`} type="button"
                    disabled={busy || !identity || !challenge || (legalRequired && !reauthenticationState && !accepted)}
                    onClick={() => void signIn(provider)}>
                    {busy ? "Signing in…" : `Continue with ${provider === "apple.com" ? "Apple" : "Google"}`}
                  </button>
                ))}
              </div>
              {error ? <p className={styles.notice} role="alert">{error}</p> : null}
              {!identity && !error ? <p className={styles.notice} role="alert">Sign-in is temporarily unavailable.</p> : null}
              {reauthenticationState && !challenge ? <a href="/settings/account">Return to account settings and try again</a> : null}
            </>
          ) : href ? (
            <a className={`button ${styles.continue}`} href={href} referrerPolicy="no-referrer">{action}</a>
          ) : (
            <>
              <p className={styles.notice} role="alert">
                {failed ? "We couldn’t load sign-in options. Please try again." : "Sign-in isn’t available here yet. Please check again later."}
              </p>
              <button className={`button ${styles.continue}`} type="button" onClick={() => setAttempt((value) => value + 1)}>
                {failed ? "Try again" : "Check again"}
              </button>
            </>
          )}
        </div>
        {config?.auth_provider === "google" && !config.local_demo ? (
          <p className={styles.footnote}>Use the Google account invited to Events Concierge.</p>
        ) : null}
        {config?.auth_provider === "identity_platform" && !reauthenticationState ? (
          <p className={styles.footnote}>New here? Continuing creates your account.</p>
        ) : null}
        <p className={styles.footnote}><a href={returnTo}>Browse without signing in</a></p>
      </section>
    </main>
  );
}
