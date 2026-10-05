"use client";

import { LoaderCircle, Search } from "lucide-react";

import type { CatalogEntityIdentityFilter } from "@/lib/entity-graph-api";
import type { CatalogEntityKind } from "@/lib/types";

/** Controlled graph filters; counts must name the population they describe. */
export const ENTITY_SEARCH_DEBOUNCE_MS = 180;

export const ENTITY_KIND_CHIPS: ReadonlyArray<{ value: CatalogEntityKind; label: string }> = [
  { value: "organization", label: "Organizations" },
  { value: "person", label: "People" },
  { value: "unknown", label: "Unclassified" },
];

export const ENTITY_IDENTITY_CHIPS: ReadonlyArray<
  { value: CatalogEntityIdentityFilter; label: string }
> = [
  { value: "all", label: "Any identity" },
  { value: "profile_verified", label: "Direct profile" },
  { value: "source_scoped", label: "Source-scoped" },
];

function count(value: number): string {
  return value.toLocaleString();
}

export interface EntitySearchBoxProps {
  query: string;
  onQueryChange: (value: string) => void;
  /** Drives the spinner inside the box; the box itself never blocks. */
  loading: boolean;
  placeholder?: string;
}

export function EntitySearchBox({
  query,
  onQueryChange,
  loading,
  placeholder = "Search organizers, hosts, speakers, companies…",
}: EntitySearchBoxProps) {
  return (
    <label className="entity-search">
      <Search aria-hidden="true" />
      <span className="sr-only">Search people and organizations</span>
      <input
        value={query}
        onChange={(event) => onQueryChange(event.target.value)}
        placeholder={placeholder}
        maxLength={160}
      />
      {loading ? <LoaderCircle className="spin" aria-hidden="true" /> : null}
    </label>
  );
}

export interface EntityFilterChipsProps {
  kinds: readonly CatalogEntityKind[];
  onKindsChange: (kinds: CatalogEntityKind[]) => void;
  /** Per-kind counts, or null while they are still unknown; a chip with no count renders bare. */
  kindCounts: Readonly<Record<CatalogEntityKind, number>> | null;
  identity: CatalogEntityIdentityFilter;
  onIdentityChange: (identity: CatalogEntityIdentityFilter) => void;
  identityCounts: Readonly<Record<CatalogEntityIdentityFilter, number>> | null;
  /** What population the identity counts describe. Rendered verbatim beside them. */
  identityScope: string;
  label?: string;
}

export function EntityFilterChips({
  kinds,
  onKindsChange,
  kindCounts,
  identity,
  onIdentityChange,
  identityCounts,
  identityScope,
  label = "Narrow the ranking",
}: EntityFilterChipsProps) {
  return (
    <div className="entity-directory-filters" role="group" aria-label={label}>
      {ENTITY_KIND_CHIPS.map((chip) => {
        const active = kinds.includes(chip.value);
        return (
          <button
            key={chip.value}
            type="button"
            className="entity-filter-chip"
            data-state={active ? "active" : "idle"}
            aria-pressed={active}
            onClick={() => onKindsChange(
              active
                ? kinds.filter((value) => value !== chip.value)
                : [...kinds, chip.value],
            )}
          >
            {chip.label}
            {kindCounts ? <small>{count(kindCounts[chip.value])}</small> : null}
          </button>
        );
      })}
      <span className="entity-directory-filters__divider" aria-hidden="true" />
      <span className="entity-directory-filters__scope">{identityScope}</span>
      {ENTITY_IDENTITY_CHIPS.map((chip) => (
        <button
          key={chip.value}
          type="button"
          className="entity-filter-chip"
          data-state={identity === chip.value ? "active" : "idle"}
          aria-pressed={identity === chip.value}
          onClick={() => onIdentityChange(chip.value)}
        >
          {chip.label}
          {identityCounts ? <small>{count(identityCounts[chip.value])}</small> : null}
        </button>
      ))}
    </div>
  );
}
