"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import styles from "@/app/docs/docs.module.css";

export const DOC_LINKS = [
  { href: "/docs", label: "Overview" },
  { href: "/docs/browsing", label: "Find events" },
  { href: "/docs/muse", label: "Muse signups" },
  { href: "/docs/account", label: "Your account" },
  { href: "/docs/how-it-works", label: "How it works" },
];

export function DocsNav() {
  const pathname = usePathname();
  return <nav className={styles.nav} aria-label="Documentation">
    {DOC_LINKS.map(link => <Link key={link.href} href={link.href}
      aria-current={pathname === link.href ? "page" : undefined}>{link.label}</Link>)}
  </nav>;
}
