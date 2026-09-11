"use client";

import {
  Fingerprint,
  KeyRound,
  Laptop,
  Lock,
  ShieldCheck,
  Smartphone,
} from "lucide-react";

import { useSession } from "@/components/session-provider";

/**
 * Design preview for the security surface. Nothing here is wired.
 *
 * Every control is inert and labelled as such, and the page deliberately contains **no input
 * fields** -- least of all a password field. A convincing mock that silently discards what someone
 * types is worse than an obviously unfinished one, and a password box is the case where that goes
 * from annoying to harmful.
 *
 * The content is also shaped by a fact worth keeping visible while this gets designed: the product
 * authenticates through OIDC and holds no password of its own. Passwords, MFA enrolment and account
 * recovery belong to the identity provider, so the real implementation is far more likely to be a
 * deep link into that provider's console than a reimplementation of any of it. The delegated shape
 * is what renders when a deployment session is active; the self-managed shape below it is the
 * alternative to react to.
 */

interface PreviewRow {
  icon: typeof Lock;
  title: string;
  detail: string;
  action: string;
  state?: "recommended" | "enabled";
}

const FACTORS: PreviewRow[] = [
  {
    icon: Smartphone,
    title: "Authenticator app",
    detail: "A rotating code from an app on your phone.",
    action: "Set up",
    state: "recommended",
  },
  {
    icon: Fingerprint,
    title: "Passkey or security key",
    detail: "Face, fingerprint, or a hardware key. Phishing resistant.",
    action: "Add",
    state: "recommended",
  },
  {
    icon: KeyRound,
    title: "Recovery codes",
    detail: "One-time codes to use if you lose your other factors.",
    action: "Generate",
  },
];

const SESSIONS = [
  { device: "This browser", where: "San Francisco, US", when: "Active now", current: true },
  { device: "Chrome on macOS", where: "San Francisco, US", when: "Yesterday", current: false },
  { device: "Safari on iPhone", where: "Oakland, US", when: "3 days ago", current: false },
];

const ACTIVITY = [
  { what: "Signed in", when: "Today, 4:12 PM", where: "San Francisco, US" },
  { what: "API key created — “Laptop script”", when: "Today, 4:38 PM", where: "San Francisco, US" },
  { what: "Signed in", when: "Yesterday, 9:02 AM", where: "San Francisco, US" },
];

export function SecurityPanel() {
  const { config } = useSession();
  const delegated = config?.auth_mode === "deployment_session";

  return (
    <div className="settings-panel">
      <header className="settings-panel__head">
        <h1>Security</h1>
        <p>How you prove it&rsquo;s you, and how to shut things down if it isn&rsquo;t.</p>
      </header>

      <p className="preview-banner" role="note">
        <ShieldCheck aria-hidden="true" />
        <span>
          <strong>Design preview.</strong> Nothing on this page is connected yet — the controls are
          inert and no settings will change. It is here to react to, not to use.
        </span>
      </p>

      {delegated ? (
        <section className="settings-card">
          <h2 className="settings-card__title">Where these live</h2>
          <p>
            You sign in through your identity provider, so your password, two-factor methods and
            account recovery are managed there rather than here. The most likely shape for this
            section is a short summary plus a link into that provider&rsquo;s security settings.
          </p>
          <button type="button" className="button button-quiet" disabled>
            Open provider security settings
            <span className="preview-tag">Preview</span>
          </button>
        </section>
      ) : null}

      <section className="settings-card">
        <h2 className="settings-card__title">Password</h2>
        <div className="security-row">
          <Lock className="security-row__icon" aria-hidden="true" />
          <div className="security-row__body">
            <p className="security-row__title">Password</p>
            <p className="security-row__detail">
              {delegated
                ? "Set and changed with your identity provider."
                : "Last changed 4 months ago."}
            </p>
          </div>
          <button type="button" className="button button-quiet" disabled>
            Change
            <span className="preview-tag">Preview</span>
          </button>
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-card__title">Two-factor authentication</h2>
        <p className="settings-hint">
          A second factor helps keep a stolen password from becoming a stolen account.
          Manage your sign-in protection with your identity provider.
        </p>
        <div className="security-rows">
          {FACTORS.map((factor) => {
            const Icon = factor.icon;
            return (
              <div className="security-row" key={factor.title}>
                <Icon className="security-row__icon" aria-hidden="true" />
                <div className="security-row__body">
                  <p className="security-row__title">
                    {factor.title}
                    {factor.state === "recommended" ? (
                      <span className="security-badge">Recommended</span>
                    ) : null}
                  </p>
                  <p className="security-row__detail">{factor.detail}</p>
                </div>
                <button type="button" className="button button-quiet" disabled>
                  {factor.action}
                  <span className="preview-tag">Preview</span>
                </button>
              </div>
            );
          })}
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-card__title">Where you&rsquo;re signed in</h2>
        <div className="security-rows">
          {SESSIONS.map((session) => (
            <div className="security-row" key={session.device}>
              <Laptop className="security-row__icon" aria-hidden="true" />
              <div className="security-row__body">
                <p className="security-row__title">
                  {session.device}
                  {session.current ? <span className="security-badge">This device</span> : null}
                </p>
                <p className="security-row__detail">
                  {session.where} · {session.when}
                </p>
              </div>
              {session.current ? null : (
                <button type="button" className="button button-quiet" disabled>
                  Sign out
                  <span className="preview-tag">Preview</span>
                </button>
              )}
            </div>
          ))}
        </div>
        <div className="security-footer">
          <button type="button" className="button button-quiet-danger" disabled>
            Sign out everywhere
            <span className="preview-tag">Preview</span>
          </button>
          <p className="settings-hint">
            Signing out everywhere should never ask you to prove yourself again first. If someone
            else is in your account, that prompt is the thing standing between you and stopping them.
          </p>
        </div>
      </section>

      <section className="settings-card">
        <h2 className="settings-card__title">Recent security activity</h2>
        <ul className="activity-list">
          {ACTIVITY.map((entry) => (
            <li key={`${entry.what}-${entry.when}`}>
              <p className="activity-list__title">{entry.what}</p>
              <p className="activity-list__meta">
                {entry.when} · {entry.where}
              </p>
            </li>
          ))}
        </ul>
        <p className="settings-hint">Sample entries. Not real activity from your account.</p>
      </section>
    </div>
  );
}
