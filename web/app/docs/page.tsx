import Link from "next/link";
import { GuideBreadcrumb } from "@/components/docs/docs-kit";
import styles from "./docs.module.css";

export default function DocsPage() {
  return <>
    <GuideBreadcrumb title="Overview" />
    <h1>Overview</h1>
    <p>Guides for finding events, managing your account and registrations.</p>
    <div className={styles.cards}>
      <Link className={styles.card} href="/docs/browsing"><strong>Find events</strong><span>Search, filters, map, calendar and sources.</span></Link>
      <Link className={styles.card} href="/docs/muse"><strong>Agentic registrations (Muse)</strong><span>Connect Muse, start registrations and track results.</span></Link>
      <Link className={styles.card} href="/docs/mcp"><strong>MCP (events discovery)</strong><span>Connect an assistant to search and compare catalog events.</span></Link>
      <Link className={styles.card} href="/docs/account"><strong>Account</strong><span>Sign-in, saved filters and connection keys.</span></Link>
      <Link className={styles.card} href="/docs/how-it-works"><strong>How it works</strong><span>Catalog data, event roles and registration tasks.</span></Link>
    </div>
  </>;
}
