"use client";

import { CircleCheck, Clock3, LoaderCircle, TriangleAlert } from "lucide-react";
import { registrationSummary, type RegistrationStatusCounts } from "@/lib/registrations";
import styles from "./registration-workspace.module.css";

export function RegistrationSummary({ counts, unread, loading, open, onOpen }: {
  counts: RegistrationStatusCounts | null; unread: number; loading: boolean; open: boolean; onOpen: () => void;
}) {
  const stats = counts ? registrationSummary(counts) : null;
  const entries = [
    { key: "queued" as const, label: "Queued", Icon: Clock3 },
    { key: "active" as const, label: "In progress", Icon: LoaderCircle },
    { key: "registered" as const, label: "Registered", Icon: CircleCheck },
    { key: "attention" as const, label: "Needs attention", Icon: TriangleAlert },
  ];
  const description = stats ? entries.map(entry => `${stats[entry.key]} ${entry.label.toLowerCase()}`).join(", ") : "Registration counts loading";
  return <button type="button" className={styles.trigger} aria-label="Registrations"
    aria-expanded={open} aria-haspopup="dialog" aria-busy={loading}
    aria-describedby="registration-summary-description" title={description}
    onClick={onOpen}>
    <span className={styles.triggerTitle}>Registrations</span>
    <span className={styles.triggerStats}>{entries.map(({ key, label, Icon }) => <span key={key}
      className={key === "attention" && (stats?.attention ?? 0) > 0 ? styles.attention : undefined} title={label}>
      <Icon aria-hidden="true" /><b>{stats ? stats[key] : "–"}</b><span className="sr-only">{label}</span>
    </span>)}</span>
    {unread > 0 ? <span className={styles.badge} aria-label={`${unread} unread registration updates`}>{unread}</span> : null}
    <span id="registration-summary-description" className="sr-only">{description}{unread ? `; ${unread} unread updates` : ""}</span>
  </button>;
}
