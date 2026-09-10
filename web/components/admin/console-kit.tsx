"use client";

import type { ReactNode } from "react";

import styles from "./console-kit.module.css";

/**
 * Shared console primitives.
 *
 * Every admin page composes from these, so the vocabulary of chip, metric, sortable header,
 * toolbar and action is defined once. The measurements reproduce what admin.css already
 * established for the source registry; the tokens come from globals.css.
 */

export { styles as kit };

export type Tone = "ok" | "warn" | "bad" | "info" | "neutral";

const CHIP_TONE: Record<Tone, string> = {
  ok: styles.chipOk,
  warn: styles.chipWarn,
  bad: styles.chipBad,
  info: styles.chipInfo,
  neutral: "",
};

const VALUE_TONE: Record<Tone, string> = {
  ok: styles.vOk,
  warn: styles.vWarn,
  bad: styles.vBad,
  info: "",
  neutral: "",
};

export const int = (n: number): string => n.toLocaleString("en-US");

export function compact(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  return int(n);
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  if (value >= 60_000) return `${(value / 60_000).toFixed(1)} min`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)} s`;
  return `${value} ms`;
}

export function age(iso: string | null | undefined): string {
  if (!iso) return "never";
  const delta = Date.now() - new Date(iso).getTime();
  if (!Number.isFinite(delta)) return "—";
  const minutes = delta / 60_000;
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${Math.round(minutes)}m ago`;
  const hours = minutes / 60;
  if (hours < 48) return `${hours.toFixed(1)}h ago`;
  return `${Math.round(hours / 24)}d ago`;
}

export function stamp(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

/* ------------------------------ page ------------------------------ */

export function PageHead({
  eyebrow,
  title,
  sub,
  statValue,
  statLabel,
  actions,
}: {
  eyebrow: string;
  title: string;
  sub?: string;
  statValue?: string;
  statLabel?: string;
  actions?: ReactNode;
}): React.JSX.Element {
  return (
    <div className={styles.head}>
      <div className={styles.headMain}>
        <span className={styles.eyebrow}>{eyebrow}</span>
        <h1 className={styles.title}>{title}</h1>
        {sub ? <p className={styles.sub}>{sub}</p> : null}
      </div>
      {actions || statValue ? <div className={styles.headControls}>
      {statValue ? (
        <div className={styles.headStat}>
          <span className="v">{statValue}</span>
          <span className="k">{statLabel}</span>
        </div>
      ) : null}
      {actions}
      </div> : null}
    </div>
  );
}

export function Section({
  id,
  title,
  scope,
  children,
}: {
  id?: string;
  title: string;
  scope?: string;
  children: ReactNode;
}): React.JSX.Element {
  return (
    <section className={styles.section} id={id}>
      <div className={styles.sectionHead}>
        <h2 className={styles.sectionTitle}>{title}</h2>
        {scope ? <span className={styles.sectionScope}>{scope}</span> : null}
      </div>
      {children}
    </section>
  );
}

/* ------------------------------ controls ------------------------------ */

export function Segment<T extends string>({
  options,
  value,
  onChange,
  label,
}: {
  options: ReadonlyArray<{ value: T; label: string }>;
  value: T;
  onChange: (next: T) => void;
  label: string;
}): React.JSX.Element {
  return (
    <div className={styles.segment} role="group" aria-label={label}>
      {options.map((option) => (
        <button
          key={option.value}
          type="button"
          className={option.value === value ? `${styles.seg} ${styles.segOn}` : styles.seg}
          onClick={() => onChange(option.value)}
          aria-pressed={option.value === value}
        >
          {option.label}
        </button>
      ))}
    </div>
  );
}

export function Chip({
  tone = "neutral",
  children,
}: {
  tone?: Tone;
  children: ReactNode;
}): React.JSX.Element {
  return (
    <span className={`${styles.chip} ${CHIP_TONE[tone]}`}>
      <i />
      {children}
    </span>
  );
}

export function Action({
  children,
  onClick,
  disabled,
  primary,
}: {
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  primary?: boolean;
}): React.JSX.Element {
  return (
    <button
      type="button"
      className={primary ? `${styles.action} ${styles.actionPrimary}` : styles.action}
      onClick={onClick}
      disabled={disabled}
    >
      {children}
    </button>
  );
}

/* ------------------------------ metrics ------------------------------ */

export interface MetricSpec {
  key: string;
  label: string;
  value: string;
  note?: string;
  tone?: Tone;
  /** Only metrics that change the current view should render as controls. */
  interactive?: boolean;
}

export function Metrics({
  items,
  selected,
  onSelect,
}: {
  items: MetricSpec[];
  selected?: string | null;
  onSelect?: (key: string) => void;
}): React.JSX.Element {
  return (
    <div className={styles.metrics}>
      {items.map((item) => {
        const interactive = Boolean(onSelect && item.interactive);
        const body = (
          <>
            <span className={styles.metricLabel}>{item.label}</span>
            <span className={`${styles.metricValue} ${VALUE_TONE[item.tone ?? "neutral"]}`}>{item.value}</span>
            {item.note ? <span className={styles.metricNote}>{item.note}</span> : null}
          </>
        );
        // A metric is only a control when selecting it actually filters something.
        return interactive ? (
          <button
            key={item.key}
            type="button"
            className={`${styles.metric} ${styles.metricBtn} ${
              selected === item.key ? styles.metricOn : ""
            }`}
            data-tone={item.tone ?? "neutral"}
            onClick={() => onSelect?.(item.key)}
            aria-pressed={selected === item.key}
          >
            {body}
          </button>
        ) : (
          <div key={item.key} className={styles.metric} data-tone={item.tone ?? "neutral"}>
            {body}
          </div>
        );
      })}
    </div>
  );
}

/* ------------------------------ sortable table ------------------------------ */

export type SortDir = "asc" | "desc";

export function SortHeader({
  label,
  columnKey,
  active,
  dir,
  onSort,
  align,
}: {
  label: string;
  columnKey: string;
  active: boolean;
  dir: SortDir;
  onSort: (key: string) => void;
  align?: "right";
}): React.JSX.Element {
  return (
    <th className={align === "right" ? styles.num : undefined}>
      <button
        type="button"
        className={active ? `${styles.sortBtn} ${styles.sortOn}` : styles.sortBtn}
        onClick={() => onSort(columnKey)}
        aria-sort={active ? (dir === "asc" ? "ascending" : "descending") : "none"}
      >
        {label}
        <span className={styles.caret}>{active ? (dir === "asc" ? "▲" : "▼") : "◆"}</span>
      </button>
    </th>
  );
}

/** Sort comparator that keeps nulls last regardless of direction. */
export function compare(
  a: string | number | null,
  b: string | number | null,
  dir: SortDir,
): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  const base = typeof a === "number" && typeof b === "number"
    ? a - b
    : String(a).localeCompare(String(b));
  return dir === "asc" ? base : -base;
}

export function LoadState({
  failed,
  onRetry,
  label,
}: {
  failed: boolean;
  onRetry: () => void;
  label: string;
}): React.JSX.Element {
  if (!failed) return <div className={styles.state}>{label}</div>;
  return (
    <div className={`${styles.state} ${styles.stateBad}`}>
      Data is unavailable — this is a load failure, not an empty result.
      <div style={{ marginTop: 12 }}>
        <Action onClick={onRetry}>Retry</Action>
      </div>
    </div>
  );
}
