import Link from "next/link";
import styles from "./docs.module.css";

export default function DocsPage() {
  return <>
    <h1>Documentation</h1>
    <p>Guides for browsing events, managing your account and registering with Muse.</p>
    <div className={styles.cards}>
      <Link className={styles.card} href="/docs/browsing"><strong>Find events</strong><span>Search, filters, map, calendar and sources.</span></Link>
      <Link className={styles.card} href="/docs/muse"><strong>Muse signups</strong><span>Connect Muse, start registrations and track results.</span></Link>
      <Link className={styles.card} href="/docs/account"><strong>Your account</strong><span>Sign-in, saved filters and connection keys.</span></Link>
      <Link className={styles.card} href="/docs/how-it-works"><strong>How it works</strong><span>Catalog data, event roles and registration tasks.</span></Link>
    </div>
  </>;
}
