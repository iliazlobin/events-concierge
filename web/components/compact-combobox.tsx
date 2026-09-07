"use client";

import { Check, ChevronDown } from "lucide-react";
import {
  type FocusEvent,
  type KeyboardEvent,
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
} from "react";

export interface CompactComboboxOption {
  value: string;
  label: string;
  searchText?: string;
  meta?: string;
}

interface CompactComboboxProps {
  controlId?: string;
  label: string;
  value: string;
  options: CompactComboboxOption[];
  className: string;
  onChange: (value: string) => void;
}

function normalize(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function optionScore(option: CompactComboboxOption, query: string): number {
  const needle = normalize(query);
  if (!needle) return 1;
  const label = normalize(option.label);
  const searchable = normalize(`${option.label} ${option.searchText ?? ""}`);
  if (label === needle) return 120;
  if (label.startsWith(needle)) return 100;
  if (searchable.includes(needle)) return 80;
  const words = needle.split(" ");
  return words.every((word) => searchable.split(" ").some((part) => part.startsWith(word)))
    ? 60
    : 0;
}

export function CompactCombobox({
  controlId,
  label,
  value,
  options,
  className,
  onChange,
}: CompactComboboxProps) {
  const labelId = useId();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(0);
  const selected = options.find((option) => option.value === value) ?? options[0];
  const visibleOptions = useMemo(
    () => options
      .map((option, index) => ({ option, index, score: optionScore(option, query) }))
      .filter(({ score }) => score > 0)
      .sort((left, right) => right.score - left.score || left.index - right.index)
      .map(({ option }) => option),
    [options, query],
  );

  useLayoutEffect(() => {
    if (!open) return;
    inputRef.current?.focus();
  }, [open]);

  useEffect(() => {
    setActiveIndex(0);
  }, [query]);

  const close = (restoreFocus = false) => {
    setOpen(false);
    setQuery("");
    setActiveIndex(0);
    if (restoreFocus) {
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    }
  };

  const choose = (option: CompactComboboxOption) => {
    onChange(option.value);
    close(true);
  };

  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    const control = event.currentTarget;
    window.setTimeout(() => {
      if (!control.contains(document.activeElement)) close();
    }, 0);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      close(true);
      return;
    }
    if (!visibleOptions.length) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((current) => (
        current >= visibleOptions.length - 1 ? 0 : current + 1
      ));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((current) => (
        current <= 0 ? visibleOptions.length - 1 : current - 1
      ));
    } else if (event.key === "Home") {
      event.preventDefault();
      setActiveIndex(0);
    } else if (event.key === "End") {
      event.preventDefault();
      setActiveIndex(visibleOptions.length - 1);
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(visibleOptions[Math.min(activeIndex, visibleOptions.length - 1)]);
    }
  };

  return (
    <div className={`filter-compact-control ${className}`} onBlur={handleBlur}>
      <span id={labelId}>{label}</span>
      <div className={`filter-combobox ${open ? "is-open" : ""}`}>
        {open ? (
          <input
            id={controlId}
            ref={inputRef}
            value={query}
            placeholder={selected?.label ?? `Choose ${label.toLowerCase()}`}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={handleKeyDown}
            role="combobox"
            aria-labelledby={labelId}
            aria-controls={listId}
            aria-expanded="true"
            aria-autocomplete="list"
            aria-activedescendant={
              visibleOptions.length
                ? `${listId}-option-${Math.min(activeIndex, visibleOptions.length - 1)}`
                : undefined
            }
          />
        ) : (
          <button
            id={controlId}
            ref={triggerRef}
            className="filter-combobox__trigger"
            type="button"
            aria-labelledby={labelId}
            aria-haspopup="listbox"
            aria-expanded="false"
            onClick={() => {
              setQuery("");
              setActiveIndex(0);
              setOpen(true);
            }}
          >
            <span>{selected?.label ?? "Choose"}</span>
            <ChevronDown aria-hidden="true" />
          </button>
        )}

        {open ? (
          <div
            className="filter-combobox__menu"
            id={listId}
            role="listbox"
            aria-labelledby={labelId}
          >
            {visibleOptions.map((option, index) => (
              <button
                id={`${listId}-option-${index}`}
                key={`${option.value}:${option.label}`}
                className={activeIndex === index ? "is-active" : ""}
                type="button"
                role="option"
                aria-selected={option.value === value}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setActiveIndex(index)}
                onClick={() => choose(option)}
              >
                <Check
                  className={option.value === value ? "is-selected" : ""}
                  aria-hidden="true"
                />
                <span>{option.label}</span>
                {option.meta ? <small>{option.meta}</small> : null}
              </button>
            ))}
            {!visibleOptions.length ? (
              <p>No matching {label.toLowerCase()}</p>
            ) : null}
          </div>
        ) : null}
      </div>
    </div>
  );
}
