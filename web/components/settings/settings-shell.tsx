"use client";

import { ArrowLeft, LoaderCircle } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { useSession } from "@/components/session-provider";

const TABS = [
  { href: "/settings", label: "Profile" },
  { href: "/settings/taste", label: "Interests" },
  { href: "/settings/saved-filters", label: "Saved filters" },
  { href: "/settings/activity", label: "Activity" },
  { href: "/settings/api-keys", label: "API keys" },
  { href: "/settings/security", label: "Security" },
  { href: "/settings/account", label: "Account and data" },
];

/**
 * Two-column settings chrome.
 *
 * The header is a sibling above the grid rather than a row inside it: a sticky box is clamped to its
 * containing block, and a grid item's containing block is its own track -- a header sized exactly to
 * its own height would have no slack and would simply scroll away.
 */
export function SettingsShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const { status, me, error, refresh } = useSession();

  return (
    <div className="settings-shell">
      <header className="settings-header">
        <a className="settings-back" href="/">
          <ArrowLeft aria-hidden="true" />
          <span>Back to events</span>
        </a>
        <span className="brand-symbol" aria-hidden="true">
          <i />
          <i />
        </span>
        <span className="settings-header__who">{me?.notify_email ?? ""}</span>
      </header>

      <div className="settings-grid">
        <nav className="settings-nav" aria-label="Settings sections">
          {TABS.map((tab) => {
            const active = pathname === tab.href;
            return (
              <Link
                key={tab.href}
                href={tab.href}
                className={active ? "is-active" : ""}
                aria-current={active ? "page" : undefined}
              >
                {tab.label}
              </Link>
            );
          })}
        </nav>

        <main className="settings-main">
          {status === "booting" ? (
            <div className="settings-state" role="status">
              <LoaderCircle className="spin" aria-hidden="true" />
              <p>Loading your account…</p>
            </div>
          ) : null}

          {status === "failed" ? (
            <div className="settings-state" role="alert">
              <p>{error ?? "Settings are briefly unavailable."}</p>
              <button type="button" className="button button-primary" onClick={refresh}>
                Try again
              </button>
            </div>
          ) : null}

          {status === "ready" ? children : null}
        </main>
      </div>
    </div>
  );
}
