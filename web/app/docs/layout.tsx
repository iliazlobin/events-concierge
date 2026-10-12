import type { Metadata } from "next";
import Link from "next/link";
import { DocsNav } from "@/components/docs/docs-nav";
import styles from "./docs.module.css";

export const metadata: Metadata = {
  title: { default: "Documentation · Events Concierge", template: "%s · Events Concierge docs" },
  description: "Events Concierge guides: browsing, accounts and Muse registrations.",
};

/** Public, prerendered guides; no account or backend access is required. */
export default function DocsLayout({ children }: { children: React.ReactNode }) {
  return <div className={styles.shell}>
    <a className={styles.skip} href="#docs-content">Skip to content</a>
    <header className={styles.header}>
      <Link className={styles.brand} href="/">
        <span className="brand-symbol" aria-hidden="true"><i /><i /></span>Events Concierge
      </Link>
      <Link href="/">Back to events</Link>
    </header>
    <div className={styles.grid}>
      <DocsNav />
      <main id="docs-content" className={styles.page} tabIndex={-1}>{children}</main>
    </div>
  </div>;
}
