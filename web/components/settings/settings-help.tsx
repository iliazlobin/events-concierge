"use client";

import { CircleHelp } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import styles from "@/components/settings/settings-help.module.css";

/** Short field help, available to pointer, keyboard and touch users. */
export function SettingsHelp({ label, children }: { label: string; children: string }) {
  const id = useId();
  const root = useRef<HTMLSpanElement>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const dismiss = (event: PointerEvent) => {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    };
    const dismissOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") { setOpen(false); event.stopPropagation(); }
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("keydown", dismissOnEscape, true);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("keydown", dismissOnEscape, true);
    };
  }, [open]);
  return <span ref={root} className={styles.help}
    onMouseEnter={() => setOpen(true)}
    onMouseLeave={event => {
      if (!event.currentTarget.contains(document.activeElement)) setOpen(false);
    }}
    onBlur={event => {
      if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
    }}>
    <button type="button" aria-label={`${label} help`} aria-describedby={open ? id : undefined}
      onFocus={() => setOpen(true)} onClick={() => setOpen(true)}>
      <CircleHelp aria-hidden="true" />
    </button>
    {open ? <span id={id} role="tooltip" className={styles.text}>{children}</span> : null}
  </span>;
}
