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

import {
  CATALOG_LOCATION_SCOPES,
  formatLocationSelection,
} from "@/lib/location-scopes";
import { formatCity } from "@/lib/presentation";
import type { LocationScope } from "@/lib/types";

interface LocationOption {
  value: string;
  label: string;
  group: "Areas" | "Neighborhoods" | "Cities";
  searchText: string;
  scope?: LocationScope;
}

interface LocationMultiComboboxProps {
  controlId?: string;
  cities: string[];
  scopes: LocationScope[];
  availableCities: string[];
  onChange: (cities: string[], scopes: LocationScope[]) => void;
}

function normalize(value: string): string {
  return value
    .normalize("NFKD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .trim();
}

function optionMatches(option: LocationOption, query: string): boolean {
  const needle = normalize(query);
  if (!needle) return true;
  const searchable = normalize(`${option.label} ${option.searchText}`);
  return needle
    .split(" ")
    .every((word) => searchable.split(" ").some((part) => part.startsWith(word)));
}

export function LocationMultiCombobox({
  controlId,
  cities,
  scopes,
  availableCities,
  onChange,
}: LocationMultiComboboxProps) {
  const labelId = useId();
  const listId = useId();
  const inputRef = useRef<HTMLInputElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [activeIndex, setActiveIndex] = useState(-1);
  const options = useMemo<LocationOption[]>(() => [
    ...CATALOG_LOCATION_SCOPES.map((scope): LocationOption => ({
      value: scope.value,
      label: scope.label,
      group: scope.kind === "Area" ? "Areas" : "Neighborhoods",
      searchText: scope.aliases.join(" "),
      scope: scope.value,
    })),
    ...availableCities.map((city): LocationOption => ({
      value: city,
      label: formatCity(city),
      group: "Cities",
      searchText: city.replace(/[_-]+/g, " "),
    })),
  ], [availableCities]);
  const visibleOptions = useMemo(
    () => options.filter((option) => optionMatches(option, query)),
    [options, query],
  );
  const selectedCount = cities.length + scopes.length;
  const triggerLabel = formatLocationSelection(cities, scopes, formatCity);

  useLayoutEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  useEffect(() => {
    setActiveIndex(query ? 0 : -1);
  }, [query]);

  const close = (restoreFocus = false) => {
    setOpen(false);
    setQuery("");
    setActiveIndex(-1);
    if (restoreFocus) {
      window.requestAnimationFrame(() => triggerRef.current?.focus());
    }
  };

  const selected = (option: LocationOption): boolean => (
    option.scope ? scopes.includes(option.scope) : cities.includes(option.value)
  );

  const toggle = (option: LocationOption) => {
    if (option.scope) {
      onChange(
        cities,
        scopes.includes(option.scope)
          ? scopes.filter((scope) => scope !== option.scope)
          : [...scopes, option.scope],
      );
    } else {
      onChange(
        cities.includes(option.value)
          ? cities.filter((city) => city !== option.value)
          : [...cities, option.value],
        scopes,
      );
    }
    setQuery("");
    window.requestAnimationFrame(() => inputRef.current?.focus());
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
    const includesEverywhere = !query;
    if (!visibleOptions.length && !includesEverywhere) return;
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setActiveIndex((current) => (
        current >= visibleOptions.length - 1 ? 0 : current + 1
      ));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setActiveIndex((current) => {
        if (current === 0 && includesEverywhere) return -1;
        if (current < 0) return visibleOptions.length - 1;
        return current - 1;
      });
    } else if (event.key === "Enter") {
      event.preventDefault();
      if (activeIndex < 0 && includesEverywhere) {
        onChange([], []);
      } else if (visibleOptions.length) {
        toggle(visibleOptions[Math.min(activeIndex, visibleOptions.length - 1)]);
      }
    }
  };

  let previousGroup: LocationOption["group"] | null = null;
  return (
    <div
      className="filter-compact-control filter-compact-control--city"
      onBlur={handleBlur}
    >
      <span id={labelId}>Place</span>
      <div className={`filter-combobox location-combobox ${open ? "is-open" : ""}`}>
        {open ? (
          <input
            id={controlId}
            ref={inputRef}
            value={query}
            placeholder={triggerLabel}
            onChange={(event) => setQuery(event.target.value)}
            onKeyDown={handleKeyDown}
            role="combobox"
            aria-labelledby={labelId}
            aria-controls={listId}
            aria-expanded="true"
            aria-autocomplete="list"
            aria-activedescendant={
              activeIndex < 0 && !query
                ? `${listId}-option-everywhere`
                : visibleOptions.length
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
            onClick={() => setOpen(true)}
          >
            <span>{triggerLabel}</span>
            <ChevronDown aria-hidden="true" />
          </button>
        )}

        {open ? (
          <div
            className="filter-combobox__menu location-combobox__menu"
            id={listId}
            role="listbox"
            aria-labelledby={labelId}
            aria-multiselectable="true"
          >
            {!query ? (
              <button
                id={`${listId}-option-everywhere`}
                className={activeIndex < 0 ? "is-active" : ""}
                type="button"
                role="option"
                aria-selected={selectedCount === 0}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setActiveIndex(-1)}
                onClick={() => {
                  onChange([], []);
                  setQuery("");
                }}
              >
                <Check
                  className={selectedCount === 0 ? "is-selected" : ""}
                  aria-hidden="true"
                />
                <span>Everywhere</span>
              </button>
            ) : null}
            {visibleOptions.map((option, index) => {
              const showGroup = option.group !== previousGroup;
              previousGroup = option.group;
              return (
                <div className="location-combobox__option" key={`${option.group}:${option.value}`}>
                  {showGroup ? <p>{option.group}</p> : null}
                  <button
                    id={`${listId}-option-${index}`}
                    className={activeIndex === index ? "is-active" : ""}
                    type="button"
                    role="option"
                    aria-selected={selected(option)}
                    onMouseDown={(event) => event.preventDefault()}
                    onMouseEnter={() => setActiveIndex(index)}
                    onClick={() => toggle(option)}
                  >
                    <Check
                      className={selected(option) ? "is-selected" : ""}
                      aria-hidden="true"
                    />
                    <span>{option.label}</span>
                    {selected(option) ? <small>Selected</small> : null}
                  </button>
                </div>
              );
            })}
            {!visibleOptions.length ? (
              <p>No known place matches that search.</p>
            ) : null}
            <footer>
              <span>{selectedCount ? `${selectedCount} selected` : "Everywhere"}</span>
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
