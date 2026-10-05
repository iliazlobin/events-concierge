"use client";

import { TopicGraphView } from "@/components/topic-graph-view";
import {
  Building2,
  CalendarDays,
  List,
  LoaderCircle,
  Map as MapIcon,
  MessageCircle,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";

import { AccountMenu } from "@/components/account-menu";
import { CalendarView } from "@/components/calendar-view";
import { ChatView } from "@/components/chat-view";
import { EventsView } from "@/components/events-view";
import { EntitiesView } from "@/components/entities-view";
import { FilterBar } from "@/components/filter-bar";
import { MapView } from "@/components/map-view";
import { Onboarding } from "@/components/onboarding";
import {
  ApiError,
  clearSession,
  getCatalogDayPage,
  getCatalogFacets,
  createSavedFilter,
  deleteSavedFilter,
  getCatalogPage,
  listSavedFilters,
  recordSavedFilterUse,
  getCatalogSummary,
  getMe,
  getUiConfig,
  logout,
  onboard,
  readSession,
  setCsrfContract,
  resolveCatalogEventEntity,
  writeSession,
} from "@/lib/api";
import {
  catalogSummarySignature,
  clearCatalogCache,
  readCatalogDayEvents,
  readCatalogSummary,
  writeCatalogDayEvents,
  writeCatalogSummary,
} from "@/lib/catalog-cache";
import { clearEntityGraphCache } from "@/lib/entity-graph-cache";
import { clipCalendarSummary } from "@/lib/calendar";
import { streamChatTurn } from "@/lib/agent-stream";
import type { SelectionEntry } from "@/lib/agent-stream";
import { catalogFilterKey, initialCatalogFilters } from "@/lib/catalog-filters";
import { addDays, localDateKey, normalizeDateRangeFilters, parseLocalDate } from "@/lib/date";
import {
  consumerHistorySnapshotFromUrl,
  consumerHistoryUrl,
  createConsumerHistorySnapshot,
  createConsumerHistoryState,
  readConsumerHistorySnapshot,
  type ConsumerHistorySnapshot,
} from "@/lib/consumer-history";
import { releaseHistorySnapshot, releaseHome, releaseProfile, releaseViewAllowed } from "@/lib/release-profile";
import { CATALOG_CITY_VALUES } from "@/lib/presentation";
import { signInFailurePath } from "@/lib/sign-in";
import type {
  CalendarMode,
  CatalogDaySummary,
  CatalogFilters,
  CatalogProvider,
  CatalogTopic,
  ChatTurn,
  EventItem,
  EventEntityReference,
  Me,
  UiConfig,
  ViewName,
  SavedFilter,
} from "@/lib/types";

/** A single day's agenda is read in pages of this size. */
const CALENDAR_DAY_PAGE_SIZE = 100;
/** Bounded skip past events that began earlier and merely continue into a day. */
const MAX_CALENDAR_DAY_SKIP_PAGES = 5;
/** Week columns preview this many events beside the day's authoritative count. */
const CALENDAR_WEEK_PREVIEW_SIZE = 4;

/**
 * Keep only the events that begin on `dayKey`.
 *
 * A day request matches by interval overlap, so a multi-day event that started
 * earlier is live on this day too. The grid counts an event on the day it
 * starts, so the agenda has to agree, or a cell reading "279 events" would open
 * onto a list headed by yesterday's leftovers.
 */
function eventsStartingOn(items: EventItem[], dayKey: string | null): EventItem[] {
  if (!dayKey) return [];
  return dedupeEvents(items).filter((item) => localDateKey(item.start_at) === dayKey);
}

/** The zone the grid buckets days into, so a late event stays on its own day. */
function calendarTimeZone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** The seven local day keys a week view shows, derived from its filter range. */
function weekPreviewDayKeys(filters: CatalogFilters): string[] {
  const start = parseLocalDate(filters.customStart);
  if (!start) return [];
  return Array.from({ length: 7 }, (_, index) => localDateKey(addDays(start, index)));
}

const NAV_ITEMS: Array<{
  value: ViewName;
  label: string;
  icon: typeof MessageCircle;
}> = [
  { value: "chat", label: "Chat", icon: MessageCircle },
  { value: "events", label: "Events", icon: List },
  { value: "map", label: "Map", icon: MapIcon },
  { value: "calendar", label: "Calendar", icon: CalendarDays },
  { value: "entities", label: "Entities", icon: Building2 },
];

// A new session starts somewhere real; Reset still returns to the fully unfiltered catalog.
const DEFAULT_FILTERS = initialCatalogFilters();

type SessionState = "booting" | "onboarding" | "ready";

function dedupeEvents(items: EventItem[]): EventItem[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    if (seen.has(item.canonical_event_id)) return false;
    seen.add(item.canonical_event_id);
    return true;
  });
}

function mergeProviders(
  current: CatalogProvider[],
  incoming: CatalogProvider[],
): CatalogProvider[] {
  const providers = new Map(current.map((provider) => [provider.source_key, provider]));
  for (const provider of incoming) providers.set(provider.source_key, provider);
  return [...providers.values()].sort((left, right) => (
    left.display_name.localeCompare(right.display_name)
  ));
}

function readableError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "Something interrupted the request.";
}

function monthRange(value = new Date()): { start: string; end: string } {
  const first = new Date(value.getFullYear(), value.getMonth(), 1);
  const last = new Date(value.getFullYear(), value.getMonth() + 1, 0);
  return { start: localDateKey(first), end: localDateKey(last) };
}

function weekRange(value = new Date()): { start: string; end: string } {
  const start = new Date(value.getFullYear(), value.getMonth(), value.getDate() - value.getDay());
  const end = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 6);
  return { start: localDateKey(start), end: localDateKey(end) };
}

function sixMonthRange(value = new Date()): { start: string; end: string } {
  const start = new Date(value.getFullYear(), value.getMonth(), 1);
  const end = new Date(value.getFullYear(), value.getMonth() + 6, 0);
  return { start: localDateKey(start), end: localDateKey(end) };
}

function calendarRange(value: Date, mode: CalendarMode): { start: string; end: string } {
  if (mode === "week") return weekRange(value);
  if (mode === "six-months") return sixMonthRange(value);
  return monthRange(value);
}

function calendarRangeFilters(
  filters: CatalogFilters,
  focusDate: string,
  mode: CalendarMode,
): CatalogFilters {
  const focus = parseLocalDate(focusDate) ?? new Date();
  const range = calendarRange(focus, mode);
  return {
    ...filters,
    datePreset: "custom",
    customStart: range.start,
    customEnd: range.end,
    dateRanges: [],
  };
}

function alignCalendarSnapshot(snapshot: ConsumerHistorySnapshot): ConsumerHistorySnapshot {
  if (snapshot.view !== "calendar" || snapshot.filters.datePreset !== "custom") {
    return snapshot;
  }
  const firstRangeStart = normalizeDateRangeFilters(snapshot.filters.dateRanges)
    .map((range) => range.start)
    .sort()[0];
  const focusDate = firstRangeStart || snapshot.filters.customStart || localDateKey(new Date());
  return createConsumerHistorySnapshot(
    snapshot.view,
    calendarRangeFilters(snapshot.filters, focusDate, snapshot.calendarMode),
    snapshot.expandedId,
    snapshot.calendarMode,
  );
}

function calendarModeForFilters(
  filters: CatalogFilters,
  fallback: CalendarMode,
): CalendarMode {
  if (filters.datePreset === "all" || filters.datePreset === "source") return "month";
  const ranges = normalizeDateRangeFilters(filters.dateRanges);
  const start = parseLocalDate(ranges[0]?.start ?? filters.customStart);
  const end = parseLocalDate(ranges.at(-1)?.end ?? filters.customEnd);
  if (!start || !end) return fallback;
  const days = Math.round((end.getTime() - start.getTime()) / 86_400_000) + 1;
  if (days <= 7) return "week";
  if (days >= 45) return "six-months";
  return "month";
}

export function ConciergeApp() {
  const router = useRouter();
  const [sessionState, setSessionState] = useState<SessionState>("booting");
  const [config, setConfig] = useState<UiConfig | null>(null);
  const profile = releaseProfile(config);
  const fullRelease = profile === "full";
  const navItems = NAV_ITEMS.filter(item => releaseViewAllowed(item.value, profile));
  const [tenantId, setTenantId] = useState<string | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [signingOut, setSigningOut] = useState(false);
  const [signOutError, setSignOutError] = useState<string | null>(null);
  const [view, setView] = useState<ViewName>("events");
  const [calendarMode, setCalendarMode] = useState<CalendarMode>("month");
  const [filters, setFilters] = useState<CatalogFilters>(DEFAULT_FILTERS);
  const [events, setEvents] = useState<EventItem[]>([]);
  const [providers, setProviders] = useState<CatalogProvider[]>([]);
  const [topicFacets, setTopicFacets] = useState<CatalogTopic[]>([]);
  const [knownCities, setKnownCities] = useState<string[]>([...CATALOG_CITY_VALUES]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [savedFilters, setSavedFilters] = useState<SavedFilter[]>([]);
  const [savedFiltersBusy, setSavedFiltersBusy] = useState(false);
  const [savedFiltersError, setSavedFiltersError] = useState<string | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [summary, setSummary] = useState<CatalogDaySummary | null>(null);
  const [summaryLoading, setSummaryLoading] = useState(false);
  /**
   * Whether `summary` actually accounts for every day of the visible range. A
   * range that has not been read yet is not a range with no events, and the grid
   * must be able to tell those apart instead of painting zeroes over an answer
   * still in flight.
   */
  const [summaryCovered, setSummaryCovered] = useState(false);
  const [selectedDayKey, setSelectedDayKey] = useState<string | null>(null);
  const [dayEvents, setDayEvents] = useState<EventItem[]>([]);
  const [dayCursor, setDayCursor] = useState<string | null>(null);
  const [dayLoading, setDayLoading] = useState(false);
  const [dayLoadingMore, setDayLoadingMore] = useState(false);
  const [dayPreviews, setDayPreviews] = useState<Map<string, EventItem[]>>(new Map());
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  // One id per browser session keeps the server-side transcript and refs together.
  const [chatConversationId] = useState(() => crypto.randomUUID());
  // Refs the user has pulled into context, plus their titles for the composer chips.
  // Keyed by entry id so events, organizers and quoted passages share one selection.
  const [selection, setSelection] = useState<Map<string, SelectionEntry>>(() => new Map());
  const selectedIds = useMemo(() => new Set(selection.keys()), [selection]);

  const handleSelectionChange = useCallback(
    (entries: SelectionEntry[], mode: "toggle" | "range" | "replace") => {
      setSelection((current) => {
        if (mode === "replace") return new Map(entries.map((entry) => [entry.id, entry]));
        const next = new Map(current);
        if (mode === "range") {
          // A drag adds its whole span, so passing back over it cannot half-clear the range.
          entries.forEach((entry) => next.set(entry.id, entry));
          return next;
        }
        entries.forEach((entry) =>
          next.has(entry.id) ? next.delete(entry.id) : next.set(entry.id, entry),
        );
        return next;
      });
    },
    [],
  );
  const [chatBusy, setChatBusy] = useState(false);
  const [chatError, setChatError] = useState<string | null>(null);
  const [onboardingBusy, setOnboardingBusy] = useState(false);
  const [onboardingError, setOnboardingError] = useState<string | null>(null);
  const [historyReady, setHistoryReady] = useState(false);
  const catalogGeneration = useRef(0);
  const summaryGeneration = useRef(0);
  /** The filter the rail's inventory was last read for, so it is read once per filter. */
  const facetsSignature = useRef<string | null>(null);
  const dayGeneration = useRef(0);
  const loadingCursor = useRef<string | null>(null);
  const preserveExpandedOnNextCatalogLoad = useRef(false);

  /**
   * Adopt a filter set, keeping the previous object when nothing about the
   * request changed.
   *
   * Browsing actions rebuild the whole snapshot, so opening an event card handed
   * back a structurally identical filter object with a new identity. That read as
   * a new request: the list was cleared, skeletons replaced the results the
   * reader had just clicked into, and the same page was fetched again.
   */
  const applyFilters = useCallback((nextFilters: CatalogFilters) => {
    setFilters((current) => (
      catalogFilterKey(current) === catalogFilterKey(nextFilters) ? current : nextFilters
    ));
  }, []);

  useEffect(() => {
    let cancelled = false;
    void getUiConfig()
      .then(async (nextConfig) => {
        if (cancelled) return;
        setConfig(nextConfig);
        setView(releaseHome(releaseProfile(nextConfig)));
        // Publish the double-submit contract before any authenticated call, so a mutation can
        // never race an unset contract and fail verification for a reason the UI cannot explain.
        setCsrfContract(nextConfig.csrf_cookie_name, nextConfig.csrf_header_name);
        const localTenant = nextConfig.local_demo ? readSession() : null;
        try {
          const identity = await getMe(localTenant);
          if (cancelled) return;
          setTenantId(localTenant);
          setMe(identity);
          setSessionState("ready");
        } catch (error) {
          if (cancelled) return;
          if (nextConfig.local_demo) {
            clearSession();
            // The cached calendar ranges belong to a session that no longer
            // resolves; never let them outlive it.
            clearCatalogCache();
            clearEntityGraphCache();
            facetsSignature.current = null;
            setSessionState("onboarding");
            return;
          }
          if (nextConfig.anonymous_browsing) {
            setTenantId(null);
            setMe(null);
            clearCatalogCache();
            clearEntityGraphCache();
            setSessionState("ready");
            return;
          }
          window.location.replace(signInFailurePath(error instanceof ApiError ? error.status : null));
        }
      })
      .catch((error) => {
        if (cancelled) return;
        setOnboardingError("Sign-in is temporarily unavailable. Please try again.");
        setSessionState("onboarding");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (sessionState !== "ready" || historyReady) return;
    const snapshot = releaseHistorySnapshot(alignCalendarSnapshot(readConsumerHistorySnapshot(window.history.state)
      ?? consumerHistorySnapshotFromUrl(window.location.href, DEFAULT_FILTERS)
      ?? createConsumerHistorySnapshot(view, filters, expandedId, calendarMode)), profile);
    preserveExpandedOnNextCatalogLoad.current = true;
    setView(snapshot.view);
    setCalendarMode(snapshot.calendarMode);
    applyFilters(snapshot.filters);
    setExpandedId(snapshot.expandedId);
    setSelectedEntityId(snapshot.selectedEntityId);
    window.history.replaceState(
      createConsumerHistoryState(snapshot, window.history.state),
      "",
      consumerHistoryUrl(snapshot, window.location.href),
    );
    setHistoryReady(true);
  }, [applyFilters, calendarMode, expandedId, filters, historyReady, profile, sessionState, view]);

  useEffect(() => {
    if (sessionState !== "ready" || !historyReady) return;
    const handlePopState = (event: PopStateEvent) => {
      const rawSnapshot = readConsumerHistorySnapshot(event.state)
        ?? consumerHistorySnapshotFromUrl(window.location.href, DEFAULT_FILTERS);
      if (!rawSnapshot) return;
      const snapshot = releaseHistorySnapshot(alignCalendarSnapshot(rawSnapshot), profile);
      const safeUrl = consumerHistoryUrl(snapshot, window.location.href);
      window.history.replaceState(
        createConsumerHistoryState(snapshot, window.history.state),
        "",
        safeUrl,
      );
      if (rawSnapshot.view !== snapshot.view || rawSnapshot.selectedEntityId !== snapshot.selectedEntityId) {
        // Next also keeps a queued URL restore. Synchronize its route state so
        // that restore cannot put a server-disabled workspace back in the URL.
        router.replace(safeUrl, { scroll: false });
      }
      preserveExpandedOnNextCatalogLoad.current = true;
      setView(snapshot.view);
      setCalendarMode(snapshot.calendarMode);
      applyFilters(snapshot.filters);
      setExpandedId(snapshot.expandedId);
      setSelectedEntityId(snapshot.selectedEntityId);
    };
    // Canonicalize before the router reads the traversed URL; otherwise its later
    // history restore can overwrite the release-safe URL with a deferred view.
    window.addEventListener("popstate", handlePopState, true);
    return () => window.removeEventListener("popstate", handlePopState, true);
  }, [applyFilters, historyReady, profile, router, sessionState]);

  useEffect(() => {
    if (sessionState !== "ready" || !historyReady) return;
    const snapshot = createConsumerHistorySnapshot(
      view, filters, expandedId, calendarMode, selectedEntityId,
    );
    window.history.replaceState(
      createConsumerHistoryState(snapshot, window.history.state),
      "",
      consumerHistoryUrl(snapshot, window.location.href),
    );
  }, [calendarMode, expandedId, filters, historyReady, selectedEntityId, sessionState, view]);

  const loadCatalog = useCallback(async (nextFilters: CatalogFilters) => {
    if (sessionState !== "ready") return;
    const generation = ++catalogGeneration.current;
    loadingCursor.current = null;
    setCatalogLoading(true);
    setLoadingMore(false);
    setCatalogError(null);
    setEvents([]);
    setNextCursor(null);
    setTopicFacets([]);
    if (preserveExpandedOnNextCatalogLoad.current) {
      preserveExpandedOnNextCatalogLoad.current = false;
    } else {
      setExpandedId(null);
    }
    try {
      const page = await getCatalogPage(tenantId, nextFilters);
      if (generation !== catalogGeneration.current) return;
      setEvents(dedupeEvents(page.items));
      setNextCursor(page.next_cursor);
      setTopicFacets(page.topic_facets ?? []);
      setProviders((current) => (
        nextFilters.sourceKeys.length
          ? mergeProviders(current, page.providers)
          : [...page.providers].sort((left, right) => (
              left.display_name.localeCompare(right.display_name)
            ))
      ));
      const cities = page.items
        .map((item) => item.city?.trim())
        .filter((value): value is string => Boolean(value));
      const facetCities = (page.city_facets ?? [])
        .map((facet) => facet.city.trim())
        .filter(Boolean);
      setKnownCities((current) => (
        [...new Set([...current, ...facetCities, ...cities])].sort()
      ));
    } catch (error) {
      if (generation !== catalogGeneration.current) return;
      setCatalogError(readableError(error));
      setEvents([]);
      setNextCursor(null);
    } finally {
      if (generation === catalogGeneration.current) setCatalogLoading(false);
    }
  }, [sessionState, tenantId]);

  useEffect(() => {
    if (sessionState !== "ready" || !historyReady) return;
    // The calendar never lists a whole range, so it must not page one. It reads
    // counts for the range and events only for the day a person selected.
    if (view === "calendar") return;
    void loadCatalog(filters);
  }, [filters, historyReady, loadCatalog, sessionState, view]);

  /**
   * Read the facet inventory the filter rail offers: the sources, cities, and
   * topics a person can pick from.
   *
   * It is deliberately not part of the calendar's range read. The rail labels
   * these counts "in range", so they do follow the window and are re-read when it
   * moves — but they cost about three times what the day counts cost, and having
   * the grid wait on them is what made every mode switch a round trip. The grid
   * now paints from its own answer while the rail catches up behind it.
   */
  const loadCalendarFacets = useCallback(async (
    nextFilters: CatalogFilters,
    signature: string,
  ) => {
    if (facetsSignature.current === signature) return;
    facetsSignature.current = signature;
    try {
      const facets = await getCatalogFacets(tenantId, nextFilters);
      if (facetsSignature.current !== signature) return;
      setTopicFacets(facets.topic_facets ?? []);
      setProviders((current) => (
        nextFilters.sourceKeys.length
          ? mergeProviders(current, facets.providers)
          : [...facets.providers].sort((left, right) => (
              left.display_name.localeCompare(right.display_name)
            ))
      ));
      const facetCities = (facets.city_facets ?? [])
        .map((facet) => facet.city.trim())
        .filter(Boolean);
      setKnownCities((current) => [...new Set([...current, ...facetCities])].sort());
    } catch {
      // The rail keeps the inventory it already has. A failed facet read must
      // not blank the calendar, whose counts come from their own request.
      // Only this request's own claim is released: a newer one already in flight
      // must keep its claim, or its answer would arrive and be discarded.
      if (facetsSignature.current === signature) facetsSignature.current = null;
    }
  }, [tenantId]);

  /**
   * Load the calendar's range as counts.
   *
   * The cache is keyed by the filter without its date window and records which
   * days it has read, so a range already contained in an earlier read paints
   * from memory and issues no request at all: that is what makes switching
   * between week, month, and six months instant rather than a round trip each.
   * A range only partly known still paints what it has, and the grid is told the
   * rest is unread rather than empty.
   */
  const loadCalendar = useCallback(async (nextFilters: CatalogFilters) => {
    if (sessionState !== "ready") return;
    const generation = ++summaryGeneration.current;
    const timeZone = calendarTimeZone();
    const signature = catalogSummarySignature(nextFilters, timeZone);
    const start = nextFilters.customStart;
    const end = nextFilters.customEnd;
    // The inventory is keyed by the whole filter, window included: its counts are
    // presented as "in range". The day cache is keyed without the window, because
    // a day's count does not depend on it.
    //
    // It is read AFTER the counts, never beside them. Both are catalog-wide
    // aggregations and the database serves them from one pool: issued together,
    // the inventory's own two seconds were added to the wait for the grid, which
    // is the only one of the two anybody is looking at. Deferring it left the
    // grid to finish in its own time and cost the rail nothing a reader sees.
    const readInventory = () => {
      void loadCalendarFacets(nextFilters, catalogFilterKey(nextFilters, { includeSort: false }));
    };
    const cached = start && end ? readCatalogSummary(tenantId, signature, start, end) : null;
    if (cached) {
      setSummary(cached.summary);
      setSummaryCovered(cached.covered);
    } else {
      // Deliberately NOT cleared. Emptying the grid before asking for its
      // replacement is what made every filter change blink the page away and
      // rebuild it. The counts on screen are the previous filter's, so they are
      // no longer authoritative - `summaryCovered` says so, and the grid dims
      // itself - but they hold the layout until the new ones arrive.
      setSummaryCovered(false);
    }
    if (cached?.covered && !cached.stale) {
      // Every day of this range was read recently. Painting it is the whole
      // answer, so the range costs nothing and the inventory has the pool to
      // itself.
      setSummaryLoading(false);
      setCatalogError(null);
      readInventory();
      return;
    }
    // A covered-but-stale range revalidates behind the counts already on screen,
    // so the grid never flickers back to a spinner it does not need.
    setSummaryLoading(!cached?.covered);
    setCatalogError(null);
    try {
      const fetched = await getCatalogSummary(tenantId, nextFilters, timeZone);
      if (generation !== summaryGeneration.current) return;
      // A range read also returns days before it, for events that began earlier
      // and run into it. Those counts describe only the overlap, so the calendar
      // drops them rather than showing a wrong total for a day it did not ask
      // about — and the cache never stores one to repeat later.
      const nextSummary = start && end ? clipCalendarSummary(fetched, start, end) : fetched;
      setSummary(nextSummary);
      setSummaryCovered(true);
      if (start && end) writeCatalogSummary(tenantId, signature, start, end, nextSummary);
    } catch (error) {
      if (generation !== summaryGeneration.current) return;
      setCatalogError(readableError(error));
      // The counts on screen are kept here too. A failed read is a reason to say
      // so - the banner and the header both do - not a reason to take the page
      // away; `summaryCovered` stays false, so nothing calls them current.
      setSummaryCovered(false);
    } finally {
      if (generation === summaryGeneration.current) setSummaryLoading(false);
      readInventory();
    }
  }, [loadCalendarFacets, sessionState, tenantId]);

  useEffect(() => {
    if (sessionState !== "ready" || !historyReady || view !== "calendar") return;
    void loadCalendar(filters);
  }, [filters, historyReady, loadCalendar, sessionState, view]);

  const handleDaySelect = useCallback((dayKey: string | null) => {
    setSelectedDayKey(dayKey);
  }, []);

  // Only the selected day's events are ever read, so a busy month costs one
  // aggregate plus one small page rather than the whole range.
  //
  // A day already opened is served from memory. Re-selecting a day is the common
  // move — every mode switch reselects one — and re-reading it showed the agenda
  // spinner over a list the page was still holding.
  useEffect(() => {
    if (sessionState !== "ready" || view !== "calendar") return;
    const generation = ++dayGeneration.current;
    if (!selectedDayKey) {
      setDayEvents([]);
      setDayCursor(null);
      return;
    }
    const dayKey = selectedDayKey;
    const requestFilters = filters;
    const agendaSignature = catalogSummarySignature(requestFilters, calendarTimeZone());
    const cachedDay = readCatalogDayEvents(tenantId, agendaSignature, dayKey);
    if (cachedDay) {
      setDayEvents(cachedDay.events);
      setDayCursor(cachedDay.cursor);
      setDayLoading(false);
      return;
    }
    // The agenda keeps the day it is showing until the new one arrives, for the
    // same reason the grid does: an empty list followed by a full one reads as
    // the page breaking, not as the page working.
    setDayLoading(true);
    void (async () => {
      try {
        // A day window also matches events that began earlier and are still
        // running. Those sort first and belong to their own start day, so keep
        // reading until this day's own events appear rather than showing an
        // empty agenda beside a cell that counts hundreds.
        let cursor: string | null = null;
        const collected: EventItem[] = [];
        for (let page = 0; page < MAX_CALENDAR_DAY_SKIP_PAGES; page += 1) {
          const next = await getCatalogDayPage(
            tenantId,
            requestFilters,
            dayKey,
            CALENDAR_DAY_PAGE_SIZE,
            cursor,
          );
          if (generation !== dayGeneration.current) return;
          collected.push(...next.items);
          cursor = next.next_cursor;
          if (cursor === null || eventsStartingOn(collected, dayKey).length) break;
        }
        const dayItems = eventsStartingOn(collected, dayKey);
        setDayEvents(dayItems);
        setDayCursor(cursor);
        writeCatalogDayEvents(tenantId, agendaSignature, dayKey, dayItems, cursor);
      } catch (error: unknown) {
        if (generation !== dayGeneration.current) return;
        setCatalogError(readableError(error));
      } finally {
        if (generation === dayGeneration.current) setDayLoading(false);
      }
    })();
  }, [filters, selectedDayKey, sessionState, tenantId, view]);

  const handleDayLoadMore = useCallback(async () => {
    if (!selectedDayKey || !dayCursor || dayLoadingMore) return;
    const generation = dayGeneration.current;
    setDayLoadingMore(true);
    try {
      const page = await getCatalogDayPage(
        tenantId,
        filters,
        selectedDayKey,
        CALENDAR_DAY_PAGE_SIZE,
        dayCursor,
      );
      if (generation !== dayGeneration.current) return;
      const merged = eventsStartingOn([...dayEvents, ...page.items], selectedDayKey);
      setDayEvents(merged);
      setDayCursor(page.next_cursor);
      // Cache what the reader now has, so returning to this day restores the list
      // they expanded rather than collapsing it back to the first page. The write
      // stays outside the state updater: React may run an updater more than once,
      // and a cache write is a side effect that must happen exactly once.
      writeCatalogDayEvents(
        tenantId,
        catalogSummarySignature(filters, calendarTimeZone()),
        selectedDayKey,
        merged,
        page.next_cursor,
      );
    } catch (error) {
      if (generation !== dayGeneration.current) return;
      setCatalogError(readableError(error));
    } finally {
      if (generation === dayGeneration.current) setDayLoadingMore(false);
    }
  }, [dayCursor, dayEvents, dayLoadingMore, filters, selectedDayKey, tenantId]);

  // Week columns preview a few events each; month and six-month cells show only
  // counts, so they need no event read at all.
  //
  // Each column reads a whole page rather than just the handful it shows: a day
  // window also matches events that began earlier and are still running, and
  // those sort first, so a small page can be entirely leftovers and leave a busy
  // column looking empty.
  useEffect(() => {
    if (sessionState !== "ready" || view !== "calendar" || calendarMode !== "week") {
      setDayPreviews(new Map());
      return;
    }
    let cancelled = false;
    const requestFilters = filters;
    const signature = catalogSummarySignature(requestFilters, calendarTimeZone());
    const dayKeys = weekPreviewDayKeys(requestFilters);
    // Columns already read are painted before the first request goes out, so
    // stepping back to a week just visited is immediate.
    const seeded = dayKeys
      .map((dayKey) => (
        [dayKey, readCatalogDayEvents(tenantId, signature, dayKey, "preview")] as const
      ))
      .filter((entry): entry is readonly [string, { events: EventItem[]; cursor: string | null }] => (
        entry[1] !== null
      ))
      .map(([dayKey, cached]) => (
        [dayKey, cached.events.slice(0, CALENDAR_WEEK_PREVIEW_SIZE)] as const
      ));
    // Merged, not replaced: a column the cache cannot answer for keeps whatever
    // it was showing until its own read lands, rather than emptying first.
    setDayPreviews((current) => new Map([...current, ...seeded]));
    const pending = dayKeys.filter(
      (dayKey) => !seeded.some(([seededKey]) => seededKey === dayKey),
    );
    if (!pending.length) return;
    void Promise.all(
      pending.map(async (dayKey) => {
        try {
          const page = await getCatalogDayPage(
            tenantId,
            requestFilters,
            dayKey,
            CALENDAR_DAY_PAGE_SIZE,
          );
          const dayItems = eventsStartingOn(page.items, dayKey);
          // "preview": one page, which may hold none of this day's own events.
          // The agenda must never be served from it.
          writeCatalogDayEvents(
            tenantId, signature, dayKey, dayItems, page.next_cursor, "preview",
          );
          return [dayKey, dayItems.slice(0, CALENDAR_WEEK_PREVIEW_SIZE)] as const;
        } catch {
          // A preview column is decorative; its day count still comes from the
          // summary, so a failed preview must not blank the week.
          return [dayKey, [] as EventItem[]] as const;
        }
      }),
    ).then((entries) => {
      if (cancelled) return;
      setDayPreviews((current) => new Map([...current, ...entries]));
    });
    return () => {
      cancelled = true;
    };
  }, [calendarMode, filters, sessionState, tenantId, view]);

  const handleOnboard = async (email: string) => {
    if (!config?.local_demo) return;
    setOnboardingBusy(true);
    setOnboardingError(null);
    try {
      const account = await onboard(email);
      writeSession(account.tenant_id);
      const identity = await getMe(account.tenant_id);
      setTenantId(account.tenant_id);
      setMe(identity);
      setSessionState("ready");
    } catch (error) {
      setOnboardingError(readableError(error));
    } finally {
      setOnboardingBusy(false);
    }
  };

  const handleLoadMore = useCallback(async () => {
    if (!nextCursor || loadingMore || loadingCursor.current === nextCursor) return;
    const generation = catalogGeneration.current;
    const cursor = nextCursor;
    const requestFilters = filters;
    loadingCursor.current = cursor;
    setLoadingMore(true);
    try {
      const page = await getCatalogPage(tenantId, requestFilters, cursor);
      if (generation !== catalogGeneration.current) return;
      setEvents((current) => dedupeEvents([...current, ...page.items]));
      setNextCursor(page.next_cursor);
      if (page.topic_facets?.length) setTopicFacets(page.topic_facets);
      setProviders((current) => (
        requestFilters.sourceKeys.length
          ? mergeProviders(current, page.providers)
          : [...page.providers].sort((left, right) => (
              left.display_name.localeCompare(right.display_name)
            ))
      ));
      const cities = page.items
        .map((item) => item.city?.trim())
        .filter((value): value is string => Boolean(value));
      const facetCities = (page.city_facets ?? [])
        .map((facet) => facet.city.trim())
        .filter(Boolean);
      setKnownCities((current) => (
        [...new Set([...current, ...facetCities, ...cities])].sort()
      ));
    } catch (error) {
      if (generation !== catalogGeneration.current) return;
      setCatalogError(readableError(error));
    } finally {
      if (loadingCursor.current === cursor) loadingCursor.current = null;
      if (generation === catalogGeneration.current) setLoadingMore(false);
    }
  }, [filters, loadingMore, nextCursor, tenantId]);

  const pushConsumerSnapshot = useCallback((requestedSnapshot: ConsumerHistorySnapshot) => {
    const nextSnapshot = releaseHistorySnapshot(requestedSnapshot, profile);
    const currentSnapshot = createConsumerHistorySnapshot(
      view,
      filters,
      expandedId,
      calendarMode,
      selectedEntityId,
    );
    const currentUrl = consumerHistoryUrl(currentSnapshot, window.location.href);
    const nextUrl = consumerHistoryUrl(nextSnapshot, window.location.href);
    window.history.replaceState(
      createConsumerHistoryState(currentSnapshot, window.history.state),
      "",
      currentUrl,
    );
    if (nextUrl !== currentUrl) {
      window.history.pushState(
        createConsumerHistoryState(nextSnapshot, window.history.state),
        "",
        nextUrl,
      );
    }
    preserveExpandedOnNextCatalogLoad.current = Boolean(nextSnapshot.expandedId);
    setView(nextSnapshot.view);
    setCalendarMode(nextSnapshot.calendarMode);
    applyFilters(nextSnapshot.filters);
    setExpandedId(nextSnapshot.expandedId);
    setSelectedEntityId(nextSnapshot.selectedEntityId);
  }, [calendarMode, expandedId, filters, profile, selectedEntityId, view]);

  const handleChat = async (text: string) => {
    if (!fullRelease || sessionState !== "ready") return;
    // The selection is consumed by this turn: it is stamped onto the user message so the prompt
    // can be recalled with the same context, then cleared. Leaving it live would silently narrow
    // every later question to a set the user has stopped thinking about.
    const sent = [...selection.values()];
    const userTurn: ChatTurn = {
      id: crypto.randomUUID(),
      role: "user",
      text,
      selection: sent,
      createdAt: Date.now(),
    };
    const replyId = crypto.randomUUID();
    setTurns((current) => [
      ...current,
      userTurn,
      {
        id: replyId,
        role: "assistant",
        text: "",
        blocks: [],
        trace: [],
        pending: true,
        createdAt: Date.now(),
      },
    ]);
    setSelection(new Map());
    setChatBusy(true);
    setChatError(null);

    // Every update targets the reply turn by id. A streaming turn spans many renders, so
    // rebuilding the list from a captured copy would drop whatever arrived meanwhile.
    const patch = (change: (turn: ChatTurn) => ChatTurn) => {
      setTurns((current) => current.map((turn) => (turn.id === replyId ? change(turn) : turn)));
    };

    try {
      await streamChatTurn(text, {
        tenantId,
        conversationId: chatConversationId,
        selectedRefs: sent
          .filter((entry) => entry.kind === "event" && entry.ref)
          .map((entry) => entry.ref as string),
        selectedEntityRefs: sent
          .filter((entry) => entry.kind === "entity" && entry.ref)
          .map((entry) => entry.ref as string),
        selectedQuotes: sent
          .filter((entry) => entry.kind === "text" && entry.text)
          .map((entry) => entry.text as string),
        onToolStart: (step) =>
          patch((turn) => ({ ...turn, trace: [...(turn.trace ?? []), step] })),
        onToolEnd: (step) =>
          patch((turn) => {
            const trace = [...(turn.trace ?? [])];
            for (let index = trace.length - 1; index >= 0; index -= 1) {
              if (trace[index].tool === step.tool && trace[index].status === undefined) {
                trace[index] = { ...trace[index], status: step.status, summary: step.summary };
                break;
              }
            }
            return { ...turn, trace };
          }),
        onBlock: (block) => {
          // Remember every ref's title so a context chip can name it, not number it.
          return patch((turn) => ({
            ...turn,
            blocks: [...(turn.blocks ?? []), block],
            // Prose arrives as a block like anything else; mirror it into `text` so the turn
            // still reads correctly for anything that only knows about text.
            text: block.kind === "prose" ? block.text : turn.text,
          }));
        },
        onEnd: (payload) =>
          patch((turn) => ({ ...turn, text: payload.text || turn.text, pending: false })),
        onError: (payload) => {
          setChatError(payload.message);
          patch((turn) => ({
            ...turn,
            pending: false,
            error: payload.message,
            // Say which half failed. "Nothing was acted on" is true either way, but a reader who
            // knows the connection dropped will retry, where a generic apology reads as refusal.
            text:
              turn.text
              || (payload.code === "unreachable" || payload.code === "stream_truncated"
                ? "The connection dropped before I could answer. Nothing was acted on — ask again."
                : "I hit a problem answering that. Nothing was acted on."),
          }));
        },
      });
    } catch (error) {
      const message = readableError(error);
      setChatError(message);
      patch((turn) => ({
        ...turn,
        pending: false,
        error: message,
        text: turn.text || "I hit a problem answering that. Nothing was acted on.",
      }));
    } finally {
      setChatBusy(false);
    }
  };

  /**
   * End the session.
   *
   * A deployment session lives on the server, so local state is cleared only once the server has
   * accepted the revocation: a 503 means the session store could not be reached and the cookie is
   * deliberately preserved, so discarding local state there would strand a still-live session.
   * Local demo holds no server session and simply drops its browser reference.
   */
  const handleSignOut = useCallback(async () => {
    if (signingOut) return;
    setSigningOut(true);
    setSignOutError(null);
    const logoutUrl = config?.logout_url;
    try {
      if (config?.auth_mode === "deployment_session") {
        if (!logoutUrl) throw new Error("Sign-out is temporarily unavailable. Please try again.");
        await logout(logoutUrl);
      }
      clearSession();
      // The cached ranges are tenant-keyed; never let them outlive the session that fetched them.
      clearCatalogCache();
      clearEntityGraphCache();
      window.location.replace(config?.local_demo || config?.anonymous_browsing ? "/" : "/sign-in?reason=signed_out");
    } catch (error) {
      setSigningOut(false);
      setSignOutError("We couldn’t sign you out. Please try again.");
    }
  }, [config, signingOut]);

  const currentProvider = useMemo(
    () => (
      filters.sourceKeys.length === 1
        ? providers.find((provider) => provider.source_key === filters.sourceKeys[0])
        : undefined
    ),
    [filters.sourceKeys, providers],
  );
  const firstExplicitDate = useMemo(() => (
    normalizeDateRangeFilters(filters.dateRanges)
      .map((range) => range.start)
      .sort()[0] ?? ""
  ), [filters.dateRanges]);
  const historicalWindow = useMemo(() => {
    if (filters.datePreset === "source" || filters.datePreset === "all") return true;
    if (firstExplicitDate) return firstExplicitDate < localDateKey(new Date());
    if (filters.datePreset !== "custom" || !filters.customStart) return false;
    return filters.customStart < localDateKey(new Date());
  }, [filters.customStart, filters.datePreset, firstExplicitDate]);
  const calendarFocusDate = useMemo(() => (
    filters.datePreset === "source" && events[0]
      ? localDateKey(events[0].start_at)
      : filters.datePreset === "all" && events[0]
        ? localDateKey(events[0].start_at)
        : firstExplicitDate || filters.customStart
  ), [events, filters.customStart, filters.datePreset, firstExplicitDate]);
  useEffect(() => {
    if (
      sessionState !== "ready"
      || !historyReady
      || view !== "calendar"
      || (filters.datePreset !== "all" && filters.datePreset !== "source")
    ) return;
    setFilters((current) => {
      const next = calendarRangeFilters(current, calendarFocusDate, calendarMode);
      return catalogFilterKey(current) === catalogFilterKey(next) ? current : next;
    });
  }, [calendarFocusDate, calendarMode, filters.datePreset, historyReady, sessionState, view]);
  const changeView = (nextView: ViewName) => {
    const nextCalendarMode = nextView === "calendar"
      ? calendarModeForFilters(filters, calendarMode)
      : calendarMode;
    const nextFilters = nextView === "calendar"
      ? calendarRangeFilters(filters, calendarFocusDate, nextCalendarMode)
      : filters;
    pushConsumerSnapshot(createConsumerHistorySnapshot(
      nextView,
      nextFilters,
      null,
      nextCalendarMode,
    ));
    window.scrollTo({ top: 0, left: 0, behavior: "smooth" });
  };
  const handleFacetSelect = useCallback((value: string) => {
    const query = value.trim();
    if (!query) return;
    const nextSnapshot = createConsumerHistorySnapshot(
      "events",
      { ...filters, query },
      null,
      calendarMode,
    );
    if (document.activeElement instanceof HTMLElement) {
      document.activeElement.blur();
    }
    pushConsumerSnapshot(nextSnapshot);
    window.requestAnimationFrame(() => {
      window.scrollTo({ top: 0, left: 0, behavior: "smooth" });
    });
  }, [calendarMode, filters, pushConsumerSnapshot]);
  const handleEntitySelect = useCallback(async (reference: EventEntityReference) => {
    setCatalogError(null);
    try {
      const resolved = await resolveCatalogEventEntity(
        tenantId,
        reference.canonicalEventId,
        reference.role,
        reference.name,
      );
      pushConsumerSnapshot(createConsumerHistorySnapshot(
        "entities",
        filters,
        null,
        calendarMode,
        resolved.entity_id,
      ));
      window.requestAnimationFrame(() => window.scrollTo({ top: 0, left: 0, behavior: "smooth" }));
    } catch (error) {
      // An unresolvable name is an indexing gap, not an operator error: the catalog only projects
      // entities from admitted observations, so say which name has no entity page yet.
      setCatalogError(
        error instanceof ApiError && error.status === 404
          ? `${reference.name} is not indexed as an entity yet.`
          : readableError(error),
      );
    }
  }, [calendarMode, filters, pushConsumerSnapshot, tenantId]);
  const handleEntityPageSelect = useCallback((entityId: string | null) => {
    pushConsumerSnapshot(createConsumerHistorySnapshot(
      "entities",
      filters,
      null,
      calendarMode,
      entityId,
    ));
    window.requestAnimationFrame(() => window.scrollTo({ top: 0, left: 0, behavior: "smooth" }));
  }, [calendarMode, filters, pushConsumerSnapshot]);
  const handleTopicSelect = useCallback((topic: string) => {
    pushConsumerSnapshot(createConsumerHistorySnapshot("entities", {
      ...filters,
      query: "",
      topics: [topic],
    }, null, calendarMode));
    window.requestAnimationFrame(() => window.scrollTo({ top: 0, left: 0, behavior: "smooth" }));
  }, [calendarMode, filters, pushConsumerSnapshot]);
  const handleTopicFilterSelect = useCallback((topic: string) => {
    const nextTopics = filters.topics.includes(topic)
      ? filters.topics.filter((value) => value !== topic)
      : [...filters.topics, topic];
    pushConsumerSnapshot(createConsumerHistorySnapshot(view === "calendar" ? "calendar" : "events", {
      ...filters, query: "", topics: nextTopics,
    }, null, calendarMode));
  }, [calendarMode, filters, pushConsumerSnapshot, view]);
  const handleTopicsClear = useCallback(() => {
    if (!filters.topics.length) return;
    pushConsumerSnapshot(createConsumerHistorySnapshot(view === "calendar" ? "calendar" : "events", {
      ...filters,
      query: "",
      topics: [],
    }, null, calendarMode));
  }, [calendarMode, filters, pushConsumerSnapshot, view]);
  const handleFiltersChange = useCallback((
    next: CatalogFilters,
    history: "replace" | "push" = "replace",
  ) => {
    let normalized = next;
    if (
        !next.sourceKeys.length
        && filters.sourceKeys.length
        && filters.datePreset === "source"
        && next.datePreset === "source"
      ) {
      normalized = {
          ...next,
          datePreset: "all",
          customStart: "",
          customEnd: "",
          dateRanges: [],
      };
    }
    if (history === "push") {
      pushConsumerSnapshot(createConsumerHistorySnapshot(
        view,
        normalized,
        null,
        calendarMode,
      ));
    } else {
      applyFilters(normalized);
    }
  }, [calendarMode, filters, pushConsumerSnapshot, view]);
  const refreshSavedFilters = useCallback(async () => {
    try {
      setSavedFilters(await listSavedFilters(tenantId));
      setSavedFiltersError(null);
    } catch {
      // A missing saved-filter list is not a reason to break browsing.
      setSavedFilters([]);
    }
  }, [tenantId]);

  useEffect(() => {
    if (sessionState !== "ready" || !me) return;
    void refreshSavedFilters();
  }, [refreshSavedFilters, sessionState, me]);

  const handleApplySavedFilter = useCallback((saved: SavedFilter) => {
    // A saved selection is a filter change like any other, so it goes through the same history
    // transaction; only the recency bookkeeping is extra.
    setSavedFiltersError(null);
    pushConsumerSnapshot(createConsumerHistorySnapshot(
      "events",
      { ...DEFAULT_FILTERS, ...saved.filters },
      null,
      calendarMode,
    ));
    void recordSavedFilterUse(saved.saved_filter_id, tenantId)
      .then((updated) => setSavedFilters((current) => {
        const rest = current.filter((entry) => entry.saved_filter_id !== updated.saved_filter_id);
        return [updated, ...rest];
      }))
      .catch(() => undefined);
  }, [calendarMode, pushConsumerSnapshot, tenantId]);

  const handleSaveFilters = useCallback((name: string) => {
    setSavedFiltersBusy(true);
    setSavedFiltersError(null);
    void createSavedFilter(name, filters, tenantId)
      .then((created) => setSavedFilters((current) => [created, ...current]))
      .catch((error: unknown) => setSavedFiltersError(readableError(error)))
      .finally(() => setSavedFiltersBusy(false));
  }, [filters, tenantId]);

  const handleDeleteSavedFilter = useCallback((saved: SavedFilter) => {
    setSavedFiltersError(null);
    setSavedFilters((current) => (
      current.filter((entry) => entry.saved_filter_id !== saved.saved_filter_id)
    ));
    void deleteSavedFilter(saved.saved_filter_id, tenantId)
      .catch((error: unknown) => {
        setSavedFiltersError(readableError(error));
        void refreshSavedFilters();
      });
  }, [refreshSavedFilters, tenantId]);

  const handleSourceSelect = useCallback((sourceKey: string) => {
    if (!sourceKey.trim()) return;
    pushConsumerSnapshot(createConsumerHistorySnapshot("events", {
      ...filters,
      query: "",
      sourceKeys: [sourceKey],
      datePreset: "source",
      customStart: "",
      customEnd: "",
      dateRanges: [],
      city: "",
      cities: [],
      locationScopes: [],
    }, null, calendarMode));
    window.requestAnimationFrame(() => {
      window.scrollTo({ top: 0, left: 0, behavior: "smooth" });
    });
  }, [calendarMode, filters, pushConsumerSnapshot]);
  const handleCalendarRangeChange = useCallback((anchor: Date, mode: CalendarMode) => {
    pushConsumerSnapshot(createConsumerHistorySnapshot(
      "calendar",
      calendarRangeFilters(filters, localDateKey(anchor), mode),
      null,
      mode,
    ));
  }, [filters, pushConsumerSnapshot]);

  if (sessionState === "booting") {
    return (
      <main className="app-loading">
        <span className="brand-symbol" aria-hidden="true"><i /><i /></span>
        <LoaderCircle className="spin" aria-hidden="true" />
      </main>
    );
  }

  if (sessionState === "onboarding" && !config?.local_demo) {
    return <main className="app-loading"><div role="alert">
      <p>{onboardingError ?? "Sign-in is temporarily unavailable."}</p>
      <button type="button" onClick={() => window.location.reload()}>Try again</button>
    </div></main>;
  }

  if (sessionState === "onboarding") {
    return (
      <Onboarding
        busy={onboardingBusy}
        error={onboardingError}
        onSubmit={handleOnboard}
      />
    );
  }

  return (
    <div className="app-shell">
      <header className="site-header">
        <button className="brand" type="button" onClick={() => changeView(releaseHome(profile))}>
          <span className="brand-symbol" aria-hidden="true"><i /><i /></span>
          <span>Events Concierge</span>
        </button>

        <nav className="site-nav" aria-label="Main navigation">
          {navItems.map(({ value, label }) => (
            <button
              key={value}
              type="button"
              className={view === value ? "is-active" : ""}
              aria-current={view === value ? "page" : undefined}
              onClick={() => changeView(value)}
            >
              {label}
            </button>
          ))}
        </nav>

        <AccountMenu
          me={me}
          config={config}
          tenantId={tenantId}
          returnTo={typeof window === "undefined" ? "/" : window.location.pathname + window.location.search}
          signingOut={signingOut}
          onSignOut={handleSignOut}
        />
      </header>

      {signOutError ? (
        <p className="app-banner app-banner--danger" role="alert">
          {signOutError}
        </p>
      ) : null}

      {view !== "chat" && view !== "entities" ? (
        <FilterBar
          filters={filters}
          providers={providers}
          topics={topicFacets}
          cities={knownCities}
          resultCount={view === "calendar"
            ? summary?.total_event_count ?? 0
            : events.length}
          loading={view === "calendar" ? summaryLoading : catalogLoading}
          signedIn={Boolean(me)}
          savedFilters={savedFilters}
          savedFiltersBusy={savedFiltersBusy}
          savedFiltersError={savedFiltersError}
          onChange={handleFiltersChange}
          onApplySavedFilter={handleApplySavedFilter}
          onSaveFilters={handleSaveFilters}
          onDeleteSavedFilter={handleDeleteSavedFilter}
        />
      ) : null}

      <main className="app-main">
        {fullRelease && view === "chat" ? (
          <ChatView
            turns={turns}
            busy={chatBusy}
            error={chatError}
            onSubmit={handleChat}
            selected={selectedIds}
            selectionEntries={[...selection.values()]}
            onSelectionChange={handleSelectionChange}
            onSourceSelect={handleSourceSelect}
            onFacetSelect={handleFacetSelect}
            onEntitySelect={handleEntitySelect}
            onTopicSelect={handleTopicSelect}
          />
        ) : null}
        {view === "events" ? (
          <EventsView
            broadDiscovery={!filters.query.trim() && !filters.topics.length && !filters.sourceKeys.length}
            events={events}
            sort={filters.sort}
            sourceName={currentProvider?.display_name}
            historicalWindow={historicalWindow}
            loading={catalogLoading}
            error={catalogError}
            expandedId={expandedId}
            nextCursor={nextCursor}
            loadingMore={loadingMore}
            onExpandedChange={(nextExpandedId) => pushConsumerSnapshot(
              createConsumerHistorySnapshot(
                "events",
                filters,
                nextExpandedId,
                calendarMode,
              ),
            )}
            onSortChange={(sort) => pushConsumerSnapshot(
              createConsumerHistorySnapshot(
                "events",
                { ...filters, sort },
                null,
                calendarMode,
              ),
            )}
            onSourceSelect={handleSourceSelect}
            onLoadMore={handleLoadMore}
            onFacetSelect={handleFacetSelect}
            onEntitySelect={handleEntitySelect}
            onTopicSelect={handleTopicSelect}
          />
        ) : null}
        {view === "map" ? (
          <MapView
            events={events}
            cities={filters.cities}
            locationScopes={filters.locationScopes}
            loading={catalogLoading}
            error={catalogError}
            hasMore={Boolean(nextCursor)}
            loadingMore={loadingMore}
            onLoadMore={handleLoadMore}
            onSourceSelect={handleSourceSelect}
            onFacetSelect={handleFacetSelect}
            onEntitySelect={handleEntitySelect}
            onTopicSelect={handleTopicSelect}
          />
        ) : null}
        {view === "calendar" ? (
          <CalendarView
            summary={summary}
            summaryCovered={summaryCovered}
            dayEvents={dayEvents}
            dayPreviews={dayPreviews}
            onDaySelect={handleDaySelect}
            focusDate={calendarFocusDate}
            mode={calendarMode}
            onRangeChange={handleCalendarRangeChange}
            loading={summaryLoading}
            dayLoading={dayLoading}
            error={catalogError}
            dayHasMore={Boolean(dayCursor)}
            dayLoadingMore={dayLoadingMore}
            onDayLoadMore={handleDayLoadMore}
            historicalWindow={historicalWindow}
            topics={topicFacets}
            activeTopics={filters.topics}
            onSourceSelect={handleSourceSelect}
            onFacetSelect={handleFacetSelect}
            onEntitySelect={handleEntitySelect}
            onTopicSelect={handleTopicFilterSelect}
            onEventTopicSelect={handleTopicSelect}
            onTopicsClear={handleTopicsClear}
          />
        ) : null}
        {view === "entities" && !selectedEntityId && filters.topics.length === 1 ? (
          <TopicGraphView key={filters.topics[0]} topic={filters.topics[0]} events={events}
            hasMore={Boolean(nextCursor)} loading={catalogLoading || loadingMore} error={catalogError}
            onLoadMore={handleLoadMore} onEntitySelect={handleEntitySelect} onTopicSelect={handleTopicSelect}/>
        ) : view === "entities" ? (
          <>
          {catalogError ? <p className="workspace-error" role="alert">{catalogError}</p> : null}
          <EntitiesView
            tenantId={tenantId}
            canRefresh={fullRelease}
            selectedEntityId={selectedEntityId}
            onSelectEntity={handleEntityPageSelect}
            onEntitySelect={handleEntitySelect}
            onTopicSelect={handleTopicSelect}
          />
          </>
        ) : null}
      </main>

      <nav className="mobile-nav" aria-label="Main navigation">
        {navItems.map(({ value, label, icon: Icon }) => (
          <button
            key={value}
            type="button"
            className={view === value ? "is-active" : ""}
            aria-current={view === value ? "page" : undefined}
            onClick={() => changeView(value)}
          >
            <Icon aria-hidden="true" />
            <span>{label}</span>
          </button>
        ))}
      </nav>

      <div className="ambient ambient--one" aria-hidden="true" />
      <div className="ambient ambient--two" aria-hidden="true" />
      <span className="sr-only">
        {config?.product_name}; {currentProvider?.display_name ?? "all providers"}
      </span>
    </div>
  );
}
