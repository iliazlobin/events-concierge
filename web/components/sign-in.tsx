"use client";

import { LoaderCircle } from "lucide-react";
import { useEffect, useState } from "react";

import { getUiConfig } from "@/lib/api";
import { signInHref, signInMessage, type SignInReason } from "@/lib/sign-in";
import type { UiConfig } from "@/lib/types";
import styles from "./sign-in.module.css";

/** Reads public capabilities only. Cancellation, logout and errors must never start another login. */
export function SignIn({ reason }: { reason: SignInReason }) {
  const [config, setConfig] = useState<UiConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [href, setHref] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setFailed(false);
    void getUiConfig().then((next) => {
      if (cancelled) return;
      setConfig(next);
      setHref(signInHref(next, window.location.origin));
    }).catch(() => {
      if (cancelled) return;
      setConfig(null);
      setHref(null);
      setFailed(true);
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => { cancelled = true; };
  }, [attempt]);

  const message = signInMessage(reason);
  const action = config?.local_demo ? "Continue to local demo"
    : config?.auth_provider === "google" ? "Continue with Google" : "Continue to sign in";

  return (
    <main className={styles.page}>
      <section className={styles.card} aria-labelledby="sign-in-title">
        <span className="brand-symbol" aria-hidden="true"><i /><i /></span>
        <p className={styles.brand}>Events Concierge</p>
        <h1 id="sign-in-title">{message.title}</h1>
        <p className={styles.description}>{message.description}</p>
        <div className={styles.actions}>
          {loading ? (
            <p className={styles.loading} role="status"><LoaderCircle className="spin" aria-hidden="true" />Loading sign-in options…</p>
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
      </section>
    </main>
  );
}
