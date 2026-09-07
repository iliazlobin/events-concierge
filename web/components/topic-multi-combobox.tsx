"use client";

import { Check, ChevronDown } from "lucide-react";
import { type FocusEvent, useId, useMemo, useRef, useState } from "react";

import type { CatalogTopic } from "@/lib/types";

interface TopicMultiComboboxProps {
  controlId?: string;
  selected: string[];
  topics: CatalogTopic[];
  onChange: (topics: string[]) => void;
}

function fallbackLabel(topic: string): string {
  return topic
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

export function TopicMultiCombobox({
  controlId,
  selected,
  topics,
  onChange,
}: TopicMultiComboboxProps) {
  const labelId = useId();
  const listId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const options = useMemo(() => {
    const byTopic = new Map(topics.map((topic) => [topic.topic, topic]));
    for (const topic of selected) {
      if (!byTopic.has(topic)) {
        byTopic.set(topic, { topic, label: fallbackLabel(topic), event_count: 0 });
      }
    }
    return [...byTopic.values()].sort((left, right) => (
      left.label.localeCompare(right.label)
    ));
  }, [selected, topics]);
  const selectedSet = new Set(selected);
  const triggerLabel = selected.length === 0
    ? "Any topic"
    : selected.length === 1
      ? options.find((option) => option.topic === selected[0])?.label ?? fallbackLabel(selected[0])
      : `${selected.length} topics`;

  const close = (restoreFocus = false) => {
    setOpen(false);
    if (restoreFocus) window.requestAnimationFrame(() => triggerRef.current?.focus());
  };
  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    const control = event.currentTarget;
    window.setTimeout(() => {
      if (!control.contains(document.activeElement)) close();
    }, 0);
  };
  const toggle = (topic: string) => onChange(
    selectedSet.has(topic)
      ? selected.filter((candidate) => candidate !== topic)
      : [...selected, topic],
  );

  return (
    <div
      className="filter-compact-control filter-compact-control--topic"
      onBlur={handleBlur}
    >
      <span id={labelId}>Topic</span>
      <div className={`filter-combobox topic-combobox ${open ? "is-open" : ""}`}>
        <button
          id={controlId}
          ref={triggerRef}
          className="filter-combobox__trigger"
          type="button"
          aria-labelledby={labelId}
          aria-haspopup="listbox"
          aria-expanded={open}
          onClick={() => setOpen((current) => !current)}
        >
          <span>{triggerLabel}</span>
          <ChevronDown aria-hidden="true" />
        </button>
        {open ? (
          <div
            className="filter-combobox__menu topic-combobox__menu"
            id={listId}
            role="listbox"
            aria-labelledby={labelId}
            aria-multiselectable="true"
          >
            <button
              type="button"
              role="option"
              aria-selected={selected.length === 0}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => onChange([])}
            >
              <Check className={selected.length === 0 ? "is-selected" : ""} aria-hidden="true" />
              <span>Any topic</span>
              <small>{selected.length ? "Clear" : `${topics.length} topics`}</small>
            </button>
            {options.map((option) => (
              <button
                key={option.topic}
                type="button"
                role="option"
                aria-selected={selectedSet.has(option.topic)}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => toggle(option.topic)}
              >
                <Check
                  className={selectedSet.has(option.topic) ? "is-selected" : ""}
                  aria-hidden="true"
                />
                <span>{option.label}</span>
                <small>{option.event_count || ""}</small>
              </button>
            ))}
            <footer>
              <span>{selected.length ? `${selected.length} selected · all required` : "Any topic"}</span>
              <button type="button" onMouseDown={(event) => event.preventDefault()} onClick={() => close(true)}>
                Done
              </button>
            </footer>
          </div>
        ) : null}
      </div>
    </div>
  );
}
