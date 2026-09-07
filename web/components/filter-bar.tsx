"use client";

import {
  Bookmark,
  CalendarDays,
  DollarSign,
  MapPin,
  Plus,
  Rss,
  RotateCcw,
  Search,
  Tag,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";

import { DateRangePicker } from "@/components/date-range-picker";
import {
  FilterChipEditor,
  type FilterChipEditorOption,
} from "@/components/filter-chip-editor";
import { SavedFilterPicker } from "@/components/saved-filter-picker";
import { suggestSavedFilterName, uniqueSavedFilterName } from "@/lib/catalog-filter-name";
import {
  PriceFilterEditor,
  type PriceSelection,
} from "@/components/price-filter-editor";
import {
  catalogFilterKey,
  emptyCatalogFilters,
  initialCatalogFilters,
} from "@/lib/catalog-filters";
import { interpretCatalogFilterExpression } from "@/lib/chat";
import {
  dateRangeFilterKey,
  filterDateKeys,
  normalizeDateRangeFilters,
  parseLocalDate,
  semanticDateRangeKeys,
} from "@/lib/date";
import {
  filterSmartFilterSuggestionsByContext,
  getPopularSmartFilterSuggestions,
  getSavedFilterSuggestions,
  getSmartFilterSuggestions,
  getStarterFilterSuggestions,
  parseSmartFilterComposerQuery,
  type AtomicSmartFilterSuggestion,
  type SmartFilterContext,
  type SmartFilterSuggestion,
} from "@/lib/filter-suggestions";
import { CATALOG_LOCATION_SCOPES, locationScope } from "@/lib/location-scopes";
import {
  CATALOG_CITY_VALUES,
  DEFAULT_CATALOG_CITY,
  formatCity,
} from "@/lib/presentation";
import type {
  CatalogFilters,
  CatalogProvider,
  CatalogTopic,
  DateRangeFilter,
  SavedFilter,
} from "@/lib/types";

interface FilterBarProps {
  filters: CatalogFilters;
  providers: CatalogProvider[];
  topics: CatalogTopic[];
  cities: string[];
  resultCount: number;
  loading: boolean;
  savedFilters: SavedFilter[];
  savedFiltersBusy?: boolean;
  savedFiltersError?: string | null;
  onChange: (next: CatalogFilters, history?: "replace" | "push") => void;
  onApplySavedFilter: (saved: SavedFilter) => void;
  onSaveFilters: (name: string) => void;
  onDeleteSavedFilter: (saved: SavedFilter) => void;
}

/**
 * What applying a saved selection layers its payload over.
 *
 * The same base the app applies with, so "is this selection the one on screen" is asked of exactly
 * what answering yes would produce -- a payload kept before a filter field existed must not read as
 * a different selection than the one it becomes.
 */
const SAVED_FILTER_BASE = initialCatalogFilters();

const MAX_DATE_RANGES = 8;
const MAX_CITY_FILTERS = 20;
const MAX_SOURCE_FILTERS = 40;
const MAX_TOPIC_FILTERS = 12;

const AVAILABILITY_OPTIONS: FilterChipEditorOption[] = [
  { id: "available", label: "Available", detail: "Registration is open" },
  { id: "sold_out", label: "Sold out", detail: "No registration spots remain" },
];

type DatePickerTarget = "legacy" | "new" | string;

function suggestionKindLabel(suggestion: SmartFilterSuggestion): string {
  if (suggestion.kind === "combination") return "Starting point";
  if (suggestion.kind === "saved") return "Saved";
  if (suggestion.kind === "city") return "City";
  if (suggestion.kind === "scope") return suggestion.scopeKind;
  if (suggestion.kind === "provider") return "Source";
  if (suggestion.kind === "topic") return "Topic";
  if (suggestion.kind === "date") return "Date";
  return "Price";
}

/**
 * The same kind, drawn instead of spelled.
 *
 * The tile layout the search box opens with gives the kind a 22px square, which no word fits; only
 * the flat list that a typed query produces has a column wide enough to spell it out. The word is
 * still what a screen reader hears -- the tile keeps it beside the glyph, visually hidden.
 */
function suggestionKindIcon(suggestion: SmartFilterSuggestion) {
  if (suggestion.kind === "saved") return <Bookmark aria-hidden="true" />;
  if (suggestion.kind === "city" || suggestion.kind === "scope") {
    return <MapPin aria-hidden="true" />;
  }
  if (suggestion.kind === "provider") return <Rss aria-hidden="true" />;
  if (suggestion.kind === "topic") return <Tag aria-hidden="true" />;
  if (suggestion.kind === "date") return <CalendarDays aria-hidden="true" />;
  return <DollarSign aria-hidden="true" />;
}

function fallbackTopicLabel(topic: string): string {
  return topic
    .split("-")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function friendlyRange(start: string, end: string): string {
  const parsedStart = parseLocalDate(start);
  const parsedEnd = parseLocalDate(end);
  if (!parsedStart || !parsedEnd) return "Custom dates";
  const sameYear = parsedStart.getFullYear() === parsedEnd.getFullYear();
  const startFormatter = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    ...(!sameYear ? { year: "numeric" as const } : {}),
  });
  const endFormatter = new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
  if (start === end) return endFormatter.format(parsedStart);
  return `${startFormatter.format(parsedStart)} – ${endFormatter.format(parsedEnd)}`;
}

/**
 * One entry of the place filter, which mixes two independent server facets:
 * named cities and predefined areas/neighborhoods. Editing a place chip may
 * cross between them, so both are addressed by one option id.
 */
type PlaceChip =
  | { kind: "city"; value: string }
  | { kind: "scope"; value: CatalogFilters["locationScopes"][number] };

function placeOptionId(chip: PlaceChip): string {
  return `${chip.kind}:${chip.value}`;
}

function parsePlaceOptionId(optionId: string): PlaceChip {
  const separator = optionId.indexOf(":");
  const value = optionId.slice(separator + 1);
  return optionId.startsWith("scope:")
    ? { kind: "scope", value: value as CatalogFilters["locationScopes"][number] }
    : { kind: "city", value };
}

/**
 * What a rolling date window is called, when the filter is one.
 *
 * A window like "this week" is the same request every day it is asked, so the chip says what was
 * asked for rather than the dates that answer it today -- and a selection kept with one still
 * means this week in December.
 */
function rollingWindowLabel(preset: CatalogFilters["datePreset"]): string | null {
  if (preset === "today") return "Today";
  if (preset === "week") return "This week";
  if (preset === "nextweek") return "Next week";
  if (preset === "workweek") return "Work week";
  if (preset === "nextworkweek") return "Next work week";
  if (preset === "weekend") return "This weekend";
  if (preset === "nextweekend") return "Next weekend";
  if (preset === "month") return "This month";
  return null;
}

/** The amount half of the price chip, empty when no comparison is selected. */
function priceAmountLabel(filters: CatalogFilters): string {
  const minimum = filters.priceMinDollars.trim();
  const maximum = filters.priceMaxDollars.trim();
  if (filters.priceComparison === "at-most" && maximum) return `≤ $${maximum}`;
  if (filters.priceComparison === "at-least" && minimum) return `≥ $${minimum}`;
  if (filters.priceComparison === "exactly" && minimum) return `= $${minimum}`;
  if (filters.priceComparison === "between" && minimum && maximum) {
    return `$${minimum} – $${maximum}`;
  }
  return "";
}

function priceLabel(filters: CatalogFilters): string {
  const amount = priceAmountLabel(filters);
  if (filters.price === "free") return "Free";
  if (filters.price === "unknown") return "Price unlisted";
  if (filters.price === "paid") return amount ? `Paid · ${amount}` : "Paid";
  return amount || "Any price";
}

/** Whether the price filter is doing anything, and so whether it earns a chip. */
function priceIsActive(filters: CatalogFilters): boolean {
  return filters.price !== "any" || priceAmountLabel(filters) !== "";
}

function availabilityLabel(filters: CatalogFilters): string {
  return filters.availability === "sold_out" ? "Sold out" : "Available";
}

function applyAtomicSuggestion(
  filters: CatalogFilters,
  suggestion: AtomicSmartFilterSuggestion,
): CatalogFilters {
  const next: CatalogFilters = {
    ...filters,
    sourceKeys: [...filters.sourceKeys],
    cities: [...filters.cities],
    locationScopes: [...filters.locationScopes],
    topics: [...filters.topics],
    dateRanges: normalizeDateRangeFilters(filters.dateRanges),
  };
  if (suggestion.kind === "city") {
    if (!suggestion.value) {
      next.city = "";
      next.cities = [];
      next.locationScopes = [];
    } else if (!next.cities.includes(suggestion.value)) {
      next.cities = [...next.cities, suggestion.value];
      next.city = next.cities[0] ?? "";
    }
  }
  if (
    suggestion.kind === "scope"
    && !next.locationScopes.includes(suggestion.value)
  ) {
    next.locationScopes = [...next.locationScopes, suggestion.value];
  }
  if (
    suggestion.kind === "provider"
    && !next.sourceKeys.includes(suggestion.value)
  ) {
    next.sourceKeys = [...next.sourceKeys, suggestion.value];
  }
  if (
    suggestion.kind === "topic"
    && !next.topics.includes(suggestion.value)
  ) {
    next.topics = [...next.topics, suggestion.value];
  }
  if (suggestion.kind === "price") {
    next.price = suggestion.value;
    if (suggestion.maximumDollars !== undefined) {
      next.priceComparison = "at-most";
      next.priceMinDollars = "";
      next.priceMaxDollars = suggestion.maximumDollars;
    } else if (suggestion.value === "free" || suggestion.value === "unknown") {
      next.priceComparison = "any";
      next.priceMinDollars = "";
      next.priceMaxDollars = "";
    }
  }
  if (suggestion.kind === "date") {
    // A named window keeps its name. Resolving "this week" to the dates it means today and
    // pinning those was what made a starting point, a typed phrase, and a kept selection all
    // go stale the moment the week turned -- and left the picker with no window lit, because
    // the filter no longer said which window it was.
    if (suggestion.value !== "custom") {
      next.datePreset = suggestion.value;
      next.customStart = "";
      next.customEnd = "";
      next.dateRanges = [];
      return next;
    }
    const dates = semanticDateRangeKeys(
      "custom",
      suggestion.customStart ?? "",
      suggestion.customEnd ?? "",
    );
    next.datePreset = "custom";
    next.customStart = dates.start;
    next.customEnd = dates.end;
    next.dateRanges = [{
      id: dateRangeFilterKey(dates.start, dates.end),
      start: dates.start,
      end: dates.end,
    }];
  }
  return next;
}

function dateRangeFromSuggestion(
  filters: CatalogFilters,
  suggestion: Extract<AtomicSmartFilterSuggestion, { kind: "date" }>,
): DateRangeFilter {
  const next = applyAtomicSuggestion(
    { ...filters, dateRanges: [] },
    suggestion,
  );
  const range = filterDateKeys(next);
  return {
    id: dateRangeFilterKey(range.start, range.end),
    start: range.start,
    end: range.end,
  };
}

export function FilterBar({
  filters,
  providers,
  topics,
  cities,
  resultCount,
  loading,
  savedFilters,
  savedFiltersBusy = false,
  savedFiltersError = null,
  onChange,
  onApplySavedFilter,
  onSaveFilters,
  onDeleteSavedFilter,
}: FilterBarProps) {
  const [query, setQuery] = useState(filters.query);
  const [searchFocused, setSearchFocused] = useState(false);
  const [suggestionsSuppressed, setSuggestionsSuppressed] = useState(false);
  const [activeSuggestion, setActiveSuggestion] = useState(-1);
  const suggestionListRef = useRef<HTMLDivElement>(null);
  /**
   * Follow the arrow keys down the list.
   *
   * The list scrolls at 300px, so arrowing past the fold moved the selection to
   * somewhere the reader could not see - the highlight was there, just below the
   * edge. `block: "nearest"` scrolls only when it has to, so a selection already
   * in view does not jump the list under the pointer.
   */
  useEffect(() => {
    if (activeSuggestion < 0) return;
    suggestionListRef.current
      ?.querySelector<HTMLElement>(`#smart-filter-suggestion-${activeSuggestion}`)
      ?.scrollIntoView({ block: "nearest" });
  }, [activeSuggestion]);
  const [composerOnly, setComposerOnly] = useState(false);
  const [composerContext, setComposerContext] = useState<SmartFilterContext | null>(null);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const latestFilters = useRef(filters);

  useEffect(() => {
    latestFilters.current = filters;
    setQuery(filters.query);
  }, [filters]);

  const dateRanges = normalizeDateRangeFilters(filters.dateRanges);
  const legacyRange = filterDateKeys(filters);
  const availableCities = useMemo(() => [...new Set([
    ...filters.cities,
    filters.city,
    ...CATALOG_CITY_VALUES,
    ...cities,
  ].filter(Boolean))], [cities, filters.cities, filters.city]);
  const typedComposer = useMemo(
    () => composerContext ? null : parseSmartFilterComposerQuery(query),
    [composerContext, query],
  );
  const effectiveComposerContext = composerContext ?? typedComposer?.context ?? null;
  const composerTerm = composerContext ? query : typedComposer?.term ?? query;
  const suggestions = useMemo(() => {
    const candidates = effectiveComposerContext && !composerTerm.trim()
      ? getPopularSmartFilterSuggestions(
        effectiveComposerContext,
        availableCities,
        providers,
        topics,
        6,
      )
      : composerTerm.trim()
      ? getSmartFilterSuggestions(
        composerTerm,
        availableCities,
        providers,
        topics,
        effectiveComposerContext ? 100 : 6,
        undefined,
        { context: effectiveComposerContext },
      )
      : getStarterFilterSuggestions(filters.cities[0] ?? DEFAULT_CATALOG_CITY);
    const scoped = filterSmartFilterSuggestionsByContext(candidates, effectiveComposerContext);
    // A saved selection is reachable by name from the same box that reaches every other filter.
    const savedMatches = effectiveComposerContext
      ? []
      : getSavedFilterSuggestions(composerTerm, savedFilters);
    return [...savedMatches, ...scoped].slice(0, 6);
  }, [
    availableCities,
    composerTerm,
    effectiveComposerContext,
    filters.cities,
    providers,
    savedFilters,
    topics,
  ]);

  useEffect(() => {
    if (composerOnly || typedComposer) return undefined;
    const timer = window.setTimeout(() => {
      const current = latestFilters.current;
      const expression = interpretCatalogFilterExpression(
        query,
        availableCities,
        providers,
        topics,
        current,
      );
      if (expression.appliedKinds.length >= 2) {
        setQuery(expression.filters.query);
        setActiveSuggestion(-1);
        onChange(expression.filters);
        return;
      }
      if (query !== current.query) onChange({ ...current, query });
    }, 220);
    return () => window.clearTimeout(timer);
  }, [availableCities, composerOnly, onChange, providers, query, topics, typedComposer]);
  const showSuggestions = searchFocused && !suggestionsSuppressed && suggestions.length > 0;
  // Nothing typed means nothing to rank against, so the list becomes a grid of tiles rather than
  // rows -- a layout narrow enough that the kind has to be drawn instead of spelled.
  const startersLayout = !query.trim();
  /**
   * Which kept selection the strip is currently holding, if any.
   *
   * Ordering is left out of the comparison: a saved selection is what it selects, and re-sorting
   * the same events is not a different selection.
   */
  const appliedSavedFilterId = useMemo(() => {
    const current = catalogFilterKey(filters, { includeSort: false });
    const match = savedFilters.find((entry) => (
      catalogFilterKey({ ...SAVED_FILTER_BASE, ...entry.filters }, { includeSort: false })
        === current
    ));
    return match?.saved_filter_id ?? null;
  }, [filters, savedFilters]);
  const hasNonDefaultFilters = Boolean(
    query
    || filters.sourceKeys.length > 0
    || filters.city
    || filters.cities.length > 0
    || filters.locationScopes.length > 0
    || priceIsActive(filters)
    || (filters.availability ?? "any") !== "any"
    || filters.topics.length > 0
    || filters.datePreset !== "all"
    || dateRanges.length > 0
  );
  const topicLabels = useMemo(
    () => new Map(topics.map((topic) => [topic.topic, topic.label])),
    [topics],
  );

  const sourceLabel = (sourceKey: string): string => (
    providers.find((provider) => provider.source_key === sourceKey)?.display_name
    ?? sourceKey
  );

  const rangeDetailSuffix = filters.datePreset === "source"
    ? "retained"
    : filters.datePreset === "all" ? "upcoming" : "in range";
  const placeOptions = useMemo<FilterChipEditorOption[]>(() => [
    ...CATALOG_LOCATION_SCOPES.map((scope) => ({
      id: placeOptionId({ kind: "scope", value: scope.value }),
      label: scope.label,
      group: scope.kind,
    })),
    ...[...availableCities]
      .map((city) => ({
        id: placeOptionId({ kind: "city", value: city }),
        label: formatCity(city),
        group: "City",
      }))
      .sort((left, right) => left.label.localeCompare(right.label)),
  ], [availableCities]);
  const sourceOptions = useMemo<FilterChipEditorOption[]>(
    () => [...providers]
      .sort((left, right) => (
        right.event_count - left.event_count
        || left.display_name.localeCompare(right.display_name)
      ))
      .map((provider) => ({
        id: provider.source_key,
        label: provider.display_name,
        detail: `${provider.event_count} ${rangeDetailSuffix}`,
      })),
    [providers, rangeDetailSuffix],
  );
  const topicOptions = useMemo<FilterChipEditorOption[]>(() => {
    const known = new Set(topics.map((topic) => topic.topic));
    return [
      ...[...topics]
        .sort((left, right) => (
          right.event_count - left.event_count || left.label.localeCompare(right.label)
        ))
        .map((topic) => ({
          id: topic.topic,
          label: topic.label,
          detail: `${topic.event_count} ${rangeDetailSuffix}`,
        })),
      // An active topic can outlive its facet once the rest of the filters
      // narrow past it, and it still has to be editable.
      ...filters.topics.filter((topic) => !known.has(topic)).map((topic) => ({
        id: topic,
        label: topicLabels.get(topic) ?? fallbackTopicLabel(topic),
      })),
    ];
  }, [filters.topics, rangeDetailSuffix, topicLabels, topics]);
  // The add controls offer only what is not already applied, so "Add place"
  // can never produce a duplicate chip or push past a limit the server rejects.
  const placeAddOptions = useMemo(() => {
    const active = new Set([
      ...filters.locationScopes.map((scope) => placeOptionId({ kind: "scope", value: scope })),
      ...filters.cities.map((city) => placeOptionId({ kind: "city", value: city })),
    ]);
    const cityCapReached = filters.cities.length >= MAX_CITY_FILTERS;
    return placeOptions.filter((option) => (
      !active.has(option.id) && !(cityCapReached && option.id.startsWith("city:"))
    ));
  }, [filters.cities, filters.locationScopes, placeOptions]);
  const sourceAddOptions = useMemo(() => {
    if (filters.sourceKeys.length >= MAX_SOURCE_FILTERS) return [];
    const active = new Set(filters.sourceKeys);
    return sourceOptions.filter((option) => !active.has(option.id));
  }, [filters.sourceKeys, sourceOptions]);
  const topicAddOptions = useMemo(() => {
    if (filters.topics.length >= MAX_TOPIC_FILTERS) return [];
    const active = new Set(filters.topics);
    return topicOptions.filter((option) => !active.has(option.id));
  }, [filters.topics, topicOptions]);

  const applyPlaceEdit = (current: PlaceChip, optionId: string) => {
    const next = parsePlaceOptionId(optionId);
    let cities = [...filters.cities];
    let locationScopes = [...filters.locationScopes];
    if (next.kind === "scope") {
      if (current.kind === "scope") {
        locationScopes = locationScopes.map((scope) => (
          scope === current.value ? next.value : scope
        ));
      } else {
        cities = cities.filter((city) => city !== current.value);
        locationScopes = [...locationScopes, next.value];
      }
    } else if (current.kind === "city") {
      cities = cities.map((city) => (city === current.value ? next.value : city));
    } else {
      locationScopes = locationScopes.filter((scope) => scope !== current.value);
      cities = [...cities, next.value];
    }
    cities = [...new Set(cities)];
    locationScopes = [...new Set(locationScopes)];
    onChange({ ...filters, city: cities[0] ?? "", cities, locationScopes }, "push");
  };
  const applySourceEdit = (current: string, next: string) => {
    onChange({
      ...filters,
      sourceKeys: [...new Set(filters.sourceKeys.map((key) => (
        key === current ? next : key
      )))],
    }, "push");
  };
  const addSources = (optionIds: string[]) => {
    onChange({
      ...filters,
      sourceKeys: [...new Set([...filters.sourceKeys, ...optionIds])]
        .slice(0, MAX_SOURCE_FILTERS),
    }, "push");
  };
  const removeSource = (sourceKey: string) => {
    onChange({
      ...filters,
      sourceKeys: filters.sourceKeys.filter((key) => key !== sourceKey),
    }, "push");
  };
  const applyTopicEdit = (current: string, next: string) => {
    onChange({
      ...filters,
      topics: [...new Set(filters.topics.map((topic) => (
        topic === current ? next : topic
      )))],
    }, "push");
  };
  const addPlaces = (optionIds: string[]) => {
    const picked = optionIds.map(parsePlaceOptionId);
    const cities = [...new Set([
      ...filters.cities,
      ...picked.flatMap((place) => (place.kind === "city" ? [place.value] : [])),
    ])].slice(0, MAX_CITY_FILTERS);
    const locationScopes = [...new Set([
      ...filters.locationScopes,
      ...picked.flatMap((place) => (place.kind === "scope" ? [place.value] : [])),
    ])];
    onChange({ ...filters, city: cities[0] ?? "", cities, locationScopes }, "push");
  };
  const addTopics = (optionIds: string[]) => {
    onChange({
      ...filters,
      topics: [...new Set([...filters.topics, ...optionIds])].slice(0, MAX_TOPIC_FILTERS),
    }, "push");
  };
  const priceSelection: PriceSelection = {
    price: filters.price,
    comparison: filters.priceComparison,
    minimum: filters.priceMinDollars,
    maximum: filters.priceMaxDollars,
  };
  const applyPriceEdit = (selection: PriceSelection) => {
    onChange({
      ...filters,
      price: selection.price,
      priceComparison: selection.comparison,
      priceMinDollars: selection.minimum,
      priceMaxDollars: selection.maximum,
    }, "push");
  };
  const clearPrice = () => onChange({
    ...filters,
    price: "any",
    priceComparison: "any",
    priceMinDollars: "",
    priceMaxDollars: "",
  }, "push");
  const applyAvailability = (availability: CatalogFilters["availability"]) => onChange({
    ...filters,
    availability,
  }, "push");
  const clearAvailability = () => applyAvailability("any");

  const suggestionIsActive = (suggestion: SmartFilterSuggestion): boolean => {
    if (suggestion.kind === "combination") {
      return suggestion.filters.every((item) => suggestionIsActive(item));
    }
    // A saved selection is a state the strip can be in: it is applied when the strip already holds
    // exactly what that selection kept.
    if (suggestion.kind === "saved") return suggestion.value === appliedSavedFilterId;
    if (suggestion.kind === "city") {
      return suggestion.value === ""
        ? filters.cities.length === 0 && filters.locationScopes.length === 0
        : filters.cities.includes(suggestion.value);
    }
    if (suggestion.kind === "scope") return filters.locationScopes.includes(suggestion.value);
    if (suggestion.kind === "provider") return filters.sourceKeys.includes(suggestion.value);
    if (suggestion.kind === "topic") return filters.topics.includes(suggestion.value);
    if (suggestion.kind === "price") {
      return suggestion.value === filters.price
        && (
          suggestion.maximumDollars === undefined
          || (
            filters.priceComparison === "at-most"
            && suggestion.maximumDollars === filters.priceMaxDollars
          )
        );
    }
    // A named window is active by name, never by the dates it happens to resolve to: a pinned
    // range covering this week is a different filter from "this week".
    if (suggestion.kind === "date" && suggestion.value !== "custom") {
      return !dateRanges.length && suggestion.value === filters.datePreset;
    }
    if (suggestion.kind === "date" && dateRanges.length) {
      const candidate = dateRangeFromSuggestion(filters, suggestion);
      return dateRanges.some((range) => (
        range.start === candidate.start && range.end === candidate.end
      ));
    }
    if (suggestion.value !== filters.datePreset) return false;
    return suggestion.value !== "custom"
      || (
        suggestion.customStart === filters.customStart
        && suggestion.customEnd === filters.customEnd
      );
  };

  const suggestionDetail = (suggestion: SmartFilterSuggestion): string => {
    if (suggestion.kind === "saved") {
      return suggestionIsActive(suggestion) ? "Applied" : suggestion.description;
    }
    if (suggestion.kind === "provider") {
      const provider = providers.find((candidate) => (
        candidate.source_key === suggestion.value
      ));
      if (provider) return `${provider.event_count} ${rangeDetailSuffix}`;
    }
    if (suggestion.kind === "topic") {
      const topic = topics.find((candidate) => candidate.topic === suggestion.value);
      if (topic) return `${topic.event_count} ${rangeDetailSuffix}`;
    }
    return suggestionIsActive(suggestion) ? "Active" : "Add filter";
  };

  useEffect(() => {
    setActiveSuggestion(-1);
  }, [query]);

  const applySuggestion = (suggestion: SmartFilterSuggestion) => {
    // A saved selection replaces the whole strip, so it never composes with what is active.
    if (suggestion.kind === "saved") {
      const entry = savedFilters.find((item) => item.saved_filter_id === suggestion.value);
      setQuery("");
      setComposerOnly(false);
      setComposerContext(null);
      setSearchFocused(false);
      setSuggestionsSuppressed(true);
      setActiveSuggestion(-1);
      if (entry) onApplySavedFilter(entry);
      return;
    }
    // Starting points are complete recipes, so applying one should not quietly
    // inherit a stale place, topic, price, or date from the current search.
    // Typed suggestions remain additive for the multi-valued filters, which is
    // how people compose two cities, areas, sources, or topics from the search
    // box. Dates are the exception: picking one answers "when", so it replaces
    // the date filter rather than stacking another chip onto it. "Add dates" is
    // the deliberate way to ask for a second window.
    let next: CatalogFilters = suggestion.kind === "combination"
      ? {
        ...filters,
        query: "",
        sourceKeys: [],
        city: "",
        cities: [],
        locationScopes: [],
        topics: [],
        price: "any",
        priceComparison: "any",
        priceMinDollars: "",
        priceMaxDollars: "",
        availability: "any",
        datePreset: "all",
        customStart: "",
        customEnd: "",
        dateRanges: [],
      }
      : { ...filters, query: "" };
    const atomicFilters: AtomicSmartFilterSuggestion[] = suggestion.kind === "combination"
      ? suggestion.filters
      : [suggestion];
    for (const atomic of atomicFilters) {
      next = applyAtomicSuggestion(next, atomic);
    }
    setQuery("");
    setComposerOnly(false);
    setComposerContext(null);
    setSearchFocused(true);
    setSuggestionsSuppressed(true);
    setActiveSuggestion(-1);
    onChange(next, "push");
  };

  const commitFilterExpression = () => {
    const expression = interpretCatalogFilterExpression(
      query,
      availableCities,
      providers,
      topics,
      filters,
    );
    const next = expression.filters;
    setQuery(next.query);
    setComposerOnly(false);
    setComposerContext(null);
    setSearchFocused(false);
    setSuggestionsSuppressed(false);
    setActiveSuggestion(-1);
    onChange(next, "push");
  };

  /** A rolling window replaces whatever the date filter was: it answers "when" on its own. */
  const applyDatePreset = (preset: CatalogFilters["datePreset"]) => {
    onChange({
      ...filters,
      datePreset: preset,
      customStart: "",
      customEnd: "",
      dateRanges: [],
    }, "push");
  };

  const clearDateFilter = (base = filters): CatalogFilters => ({
    ...base,
    datePreset: "all",
    customStart: "",
    customEnd: "",
    dateRanges: [],
  });

  const openFilterComposer = (seed = "", context: SmartFilterContext | null = null) => {
    setComposerOnly(true);
    setComposerContext(context);
    setQuery(seed);
    setSearchFocused(true);
    setSuggestionsSuppressed(false);
    window.requestAnimationFrame(() => {
      searchInputRef.current?.focus();
      searchInputRef.current?.select();
    });
  };

  const applyDateRange = (target: DatePickerTarget, start: string, end: string) => {
    if (target === "legacy") {
      onChange({
        ...filters,
        datePreset: "custom",
        customStart: start,
        customEnd: end,
        dateRanges: [],
      }, "push");
      return;
    }

    let nextRanges = [...dateRanges];
    if (target === "new") {
      if (nextRanges.length >= MAX_DATE_RANGES) return;
      if (!nextRanges.length && filters.datePreset !== "all") {
        nextRanges.push({
          id: dateRangeFilterKey(legacyRange.start, legacyRange.end),
          start: legacyRange.start,
          end: legacyRange.end,
        });
      }
      nextRanges.push({
        id: `${dateRangeFilterKey(start, end)}:${Date.now().toString(36)}`,
        start,
        end,
      });
    } else {
      nextRanges = nextRanges.map((range) => (
        range.id === target ? { ...range, start, end, label: undefined } : range
      ));
    }
    nextRanges = normalizeDateRangeFilters(nextRanges);
    const first = nextRanges[0];
    onChange({
      ...filters,
      datePreset: "custom",
      customStart: first?.start ?? start,
      customEnd: first?.end ?? end,
      dateRanges: nextRanges,
    }, "push");
  };

  const removeDateRange = (target: DatePickerTarget) => {
    if (target === "new") return;
    if (target === "legacy") {
      onChange(clearDateFilter(), "push");
      return;
    }
    const nextRanges = dateRanges.filter((range) => range.id !== target);
    if (!nextRanges.length) {
      onChange(clearDateFilter(), "push");
      return;
    }
    onChange({
      ...filters,
      datePreset: "custom",
      customStart: nextRanges[0].start,
      customEnd: nextRanges[0].end,
      dateRanges: nextRanges,
    }, "push");
  };

  const reset = () => {
    setQuery("");
    setComposerOnly(false);
    setComposerContext(null);
    setSearchFocused(false);
    setSuggestionsSuppressed(false);
    setActiveSuggestion(-1);
    onChange(emptyCatalogFilters(), "push");
  };

  const removeCity = (city: string) => {
    const nextCities = filters.cities.filter((candidate) => candidate !== city);
    onChange({ ...filters, city: nextCities[0] ?? "", cities: nextCities }, "push");
  };
  const removeScope = (scope: CatalogFilters["locationScopes"][number]) => {
    onChange({
      ...filters,
      locationScopes: filters.locationScopes.filter((candidate) => candidate !== scope),
    }, "push");
  };
  const removeTopic = (topic: string) => {
    onChange(
      { ...filters, topics: filters.topics.filter((candidate) => candidate !== topic) },
      "push",
    );
  };

  const dateChips: Array<DateRangeFilter & { legacy?: boolean }> = dateRanges.length
    ? dateRanges
    : filters.datePreset === "all" ? [] : [{
      id: "legacy",
      start: legacyRange.start,
      end: legacyRange.end,
      legacy: true,
    }];
  const today = filterDateKeys({
    ...filters,
    datePreset: "today",
    customStart: "",
    customEnd: "",
    dateRanges: [],
  });
  const addDateStart = filters.datePreset === "all" ? today.start : legacyRange.start;
  const addDateEnd = filters.datePreset === "all" ? today.end : legacyRange.end;
  return (
    <div className="filter-shell">
      <div className="filter-bar">
        <div className="filter-search-wrap">
          <label className="filter-search">
            <Search aria-hidden="true" />
            <span className="sr-only">
              Search events, organizers, hosts, speakers, partners, or add a filter
            </span>
            <input
              ref={searchInputRef}
              value={query}
              onChange={(event) => {
                setQuery(event.target.value);
                setSuggestionsSuppressed(false);
              }}
              onFocus={() => {
                setSearchFocused(true);
                setSuggestionsSuppressed(false);
              }}
              onBlur={() => {
                setSearchFocused(false);
                if (composerOnly) {
                  setQuery(filters.query);
                  setComposerOnly(false);
                  setComposerContext(null);
                }
              }}
              onKeyDown={(event) => {
                if (
                  event.key === "Enter"
                  && effectiveComposerContext
                  && suggestions.length > 0
                  && activeSuggestion < 0
                ) {
                  event.preventDefault();
                  applySuggestion(suggestions[0]);
                  return;
                }
                if (
                  event.key === "Enter"
                  && effectiveComposerContext
                  && activeSuggestion < 0
                ) {
                  event.preventDefault();
                  return;
                }
                if (
                  event.key === "Enter"
                  && query.trim()
                  && (!showSuggestions || activeSuggestion < 0)
                ) {
                  event.preventDefault();
                  commitFilterExpression();
                  return;
                }
                if (!showSuggestions) return;
                if (event.key === "ArrowDown") {
                  event.preventDefault();
                  setActiveSuggestion((current) => (
                    current >= suggestions.length - 1 ? 0 : current + 1
                  ));
                } else if (event.key === "ArrowUp") {
                  event.preventDefault();
                  setActiveSuggestion((current) => (
                    current <= 0 ? suggestions.length - 1 : current - 1
                  ));
                } else if (event.key === "Enter" && activeSuggestion >= 0) {
                  event.preventDefault();
                  applySuggestion(suggestions[activeSuggestion]);
                } else if (event.key === "Escape") {
                  setSearchFocused(false);
                  setSuggestionsSuppressed(false);
                  setActiveSuggestion(-1);
                  if (composerOnly) {
                    setQuery(filters.query);
                    setComposerOnly(false);
                    setComposerContext(null);
                  }
                }
              }}
              placeholder="Search events, or type place, topic, source, date, or price…"
              maxLength={160}
              role="combobox"
              aria-autocomplete="list"
              aria-expanded={showSuggestions}
              aria-controls="smart-filter-suggestions"
              aria-activedescendant={
                activeSuggestion >= 0
                  ? `smart-filter-suggestion-${activeSuggestion}`
                  : undefined
              }
            />
            {query ? (
              <button
                type="button"
                aria-label="Clear search"
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => {
                  setQuery("");
                  setSuggestionsSuppressed(false);
                }}
              >
                <X aria-hidden="true" />
              </button>
            ) : null}
          </label>

          {showSuggestions ? (
            <div
              className={`filter-suggestions${startersLayout ? " is-starters" : ""}`}
              ref={suggestionListRef}
              id="smart-filter-suggestions"
              role="listbox"
              aria-label={effectiveComposerContext
                ? `Suggested ${effectiveComposerContext} filters`
                : startersLayout ? "Useful starting points" : "Suggested filters"}
            >
              <span className="filter-suggestions__heading">
                {effectiveComposerContext
                  ? `Suggested ${effectiveComposerContext === "source" ? "sources" : `${effectiveComposerContext}s`}`
                  : startersLayout ? "Useful starting points" : "Suggested filters"}
              </span>
              {suggestions.map((suggestion, index) => {
                const detail = suggestion.kind === "combination"
                  ? suggestion.description
                  : suggestionDetail(suggestion);
                return (
                <button
                  id={`smart-filter-suggestion-${index}`}
                  key={`${suggestion.kind}:${suggestion.value}:${suggestion.label}`}
                  className={activeSuggestion === index ? "is-active" : ""}
                  data-kind={suggestion.kind}
                  type="button"
                  role="option"
                  aria-selected={activeSuggestion === index}
                  // A tile hides its detail line and clips a long name, so the whole of both stays
                  // reachable by hovering it.
                  title={startersLayout ? `${suggestion.label} · ${detail}` : undefined}
                  onMouseDown={(event) => event.preventDefault()}
                  onMouseEnter={() => setActiveSuggestion(index)}
                  onClick={() => applySuggestion(suggestion)}
                >
                  {suggestion.kind === "combination" ? (
                    <Plus className="filter-suggestion__add" aria-hidden="true" />
                  ) : startersLayout ? (
                    <span className="filter-suggestion__mark">
                      {suggestionKindIcon(suggestion)}
                      <span className="sr-only">{suggestionKindLabel(suggestion)}</span>
                    </span>
                  ) : (
                    <span>{suggestionKindLabel(suggestion)}</span>
                  )}
                  <strong>{suggestion.label}</strong>
                  <small>{detail}</small>
                </button>
                );
              })}
            </div>
          ) : null}
        </div>
        <div className="filter-bar__actions">
          <span aria-live="polite">
            {loading ? "Loading" : `${resultCount} match${resultCount === 1 ? "" : "es"}`}
          </span>
        </div>
      </div>

      <div className="active-filter-strip" aria-label="Active filters">
        {/* A whole selection is not another filter, so it leads the strip and is drawn as its own
            class of control -- everything to its right edits one dimension of what it holds. */}
        <SavedFilterPicker
          saved={savedFilters}
          appliedId={appliedSavedFilterId}
          suggestedName={uniqueSavedFilterName(
            suggestSavedFilterName(filters, providers, topics),
            savedFilters.map((entry) => entry.name),
          )}
          busy={savedFiltersBusy}
          error={savedFiltersError}
          onApply={onApplySavedFilter}
          onSave={onSaveFilters}
          onDelete={onDeleteSavedFilter}
        />
        {filters.sourceKeys.map((sourceKey) => (
          <FilterChipEditor
            key={`source:${sourceKey}`}
            chipLabel="Source"
            summary={sourceLabel(sourceKey)}
            title="Edit source"
            hint="Changes only this source filter"
            options={sourceOptions}
            selectedIds={[sourceKey]}
            searchLabel="Find a source"
            emptyLabel="No source matches"
            clearActionLabel="Remove source"
            removeLabel={`Remove ${sourceLabel(sourceKey)}`}
            onApply={([next]) => applySourceEdit(sourceKey, next)}
            onRemove={() => removeSource(sourceKey)}
          />
        ))}
        {filters.locationScopes.map((scope) => (
          <FilterChipEditor
            key={`scope:${scope}`}
            chipLabel={locationScope(scope)?.kind ?? "Place"}
            summary={locationScope(scope)?.label ?? scope}
            title="Edit place"
            hint="Changes only this place filter"
            options={placeOptions}
            selectedIds={[placeOptionId({ kind: "scope", value: scope })]}
            searchLabel="Find a place"
            emptyLabel="No place matches"
            clearActionLabel="Remove place"
            removeLabel={`Remove ${locationScope(scope)?.label ?? scope}`}
            onApply={([optionId]) => applyPlaceEdit({ kind: "scope", value: scope }, optionId)}
            onRemove={() => removeScope(scope)}
          />
        ))}
        {filters.cities.map((city) => (
          <FilterChipEditor
            key={`city:${city}`}
            chipLabel="Place"
            summary={formatCity(city)}
            title="Edit place"
            hint="Changes only this place filter"
            options={placeOptions}
            selectedIds={[placeOptionId({ kind: "city", value: city })]}
            searchLabel="Find a place"
            emptyLabel="No place matches"
            clearActionLabel="Remove place"
            removeLabel={`Remove ${formatCity(city)}`}
            onApply={([optionId]) => applyPlaceEdit({ kind: "city", value: city }, optionId)}
            onRemove={() => removeCity(city)}
          />
        ))}
        {filters.topics.map((topic) => (
          <FilterChipEditor
            key={`topic:${topic}`}
            chipLabel="Topic"
            summary={topicLabels.get(topic) ?? fallbackTopicLabel(topic)}
            title="Edit topic"
            hint="Changes only this topic filter"
            options={topicOptions}
            selectedIds={[topic]}
            searchLabel="Find a topic"
            emptyLabel="No topic matches"
            clearActionLabel="Remove topic"
            removeLabel={`Remove ${topicLabels.get(topic) ?? topic}`}
            onApply={([next]) => applyTopicEdit(topic, next)}
            onRemove={() => removeTopic(topic)}
          />
        ))}
        {priceIsActive(filters) ? (
          <PriceFilterEditor
            summary={priceLabel(filters)}
            title="Edit price"
            hint="Changes only this price filter"
            selection={priceSelection}
            clearActionLabel="Remove price"
            removeLabel="Remove price filter"
            onApply={applyPriceEdit}
            onRemove={clearPrice}
          />
        ) : null}
        {(filters.availability ?? "any") !== "any" ? (
          <FilterChipEditor
            chipLabel="Availability"
            summary={availabilityLabel(filters)}
            title="Edit availability"
            hint="Registration status"
            options={AVAILABILITY_OPTIONS}
            selectedIds={[filters.availability]}
            searchLabel="Find an availability"
            clearActionLabel="Remove availability"
            removeLabel="Remove availability filter"
            onApply={([next]) => applyAvailability(next as CatalogFilters["availability"])}
            onRemove={clearAvailability}
          />
        ) : null}
        {dateChips.map((range) => {
          const target = range.legacy ? "legacy" : range.id;
          const explicitRange = friendlyRange(range.start, range.end);
          // Only the chip standing in for the whole date filter can *read* as a rolling window;
          // an additive range is two fixed dates by construction.
          const rolling = range.legacy ? rollingWindowLabel(filters.datePreset) : null;
          // But swapping a pinned range for a rolling one is exactly the move worth offering, so
          // the windows are reachable from a single date chip whether or not it is pinned. With
          // several ranges in play there is no single one to swap: choosing a window would drop
          // the others without saying so.
          const swappableForWindow = dateChips.length === 1;
          return (
            <DateRangePicker
              key={`date:${range.id}`}
              variant="chip"
              start={range.start}
              end={range.end}
              summary={rolling ?? explicitRange}
              summaryTitle={rolling ? `${rolling} · ${explicitRange}` : undefined}
              rangeCount={dateChips.length}
              editing
              removable
              removeLabel={`Remove date filter ${rolling ?? explicitRange}`}
              activePreset={range.legacy ? filters.datePreset : undefined}
              onPreset={swappableForWindow ? applyDatePreset : undefined}
              onApply={(start, end) => applyDateRange(target, start, end)}
              onClear={() => removeDateRange(target)}
            />
          );
        })}
        <FilterChipEditor
          variant="add"
          summary="Add source"
          title="Add source"
          hint="Matches any selected source"
          options={sourceAddOptions}
          multiple
          searchLabel="Find a source"
          emptyLabel="No source left to add"
          disabled={!sourceAddOptions.length}
          onApply={addSources}
        />
        <FilterChipEditor
          variant="add"
          summary="Add place"
          title="Add place"
          hint="Matches any selected place"
          options={placeAddOptions}
          multiple
          searchLabel="Find a place"
          emptyLabel="No place left to add"
          disabled={!placeAddOptions.length}
          onApply={addPlaces}
        />
        <FilterChipEditor
          variant="add"
          summary="Add topic"
          title="Add topic"
          hint="Keeps events carrying every topic"
          options={topicAddOptions}
          multiple
          searchLabel="Find a topic"
          emptyLabel="No topic left to add"
          disabled={!topicAddOptions.length}
          onApply={addTopics}
        />
        {priceIsActive(filters) ? null : (
          <PriceFilterEditor
            variant="add"
            summary="Add price"
            title="Add price"
            hint="One category and one amount"
            selection={priceSelection}
            onApply={applyPriceEdit}
          />
        )}
        {(filters.availability ?? "any") === "any" ? (
          <FilterChipEditor
            variant="add"
            summary="Add availability"
            title="Add availability"
            hint="Choose registration status"
            options={AVAILABILITY_OPTIONS}
            searchLabel="Find an availability"
            onApply={([next]) => applyAvailability(next as CatalogFilters["availability"])}
          />
        ) : null}
        <DateRangePicker
          variant="add"
          start={addDateStart}
          end={addDateEnd}
          summary="Add dates"
          rangeCount={dateChips.length}
          activePreset={dateChips.length ? undefined : filters.datePreset}
          onPreset={dateChips.length ? undefined : applyDatePreset}
          editing={false}
          disabled={dateRanges.length >= MAX_DATE_RANGES}
          onApply={(start, end) => applyDateRange("new", start, end)}
          onClear={() => undefined}
        />
        {hasNonDefaultFilters ? (
          <button className="filter-reset" type="button" onClick={reset}>
            <RotateCcw aria-hidden="true" />
            Reset
          </button>
        ) : null}
      </div>

    </div>
  );
}
