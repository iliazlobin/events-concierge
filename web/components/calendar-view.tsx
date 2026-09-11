"use client";

import { ChevronLeft, ChevronRight, LoaderCircle, Search, X } from "lucide-react";
import { Fragment, useEffect, useMemo, useRef, useState } from "react";

import { EventList } from "@/components/event-list";
import {
  calendarRangeLabel,
  calendarSummaryByDate,
  calendarSummaryDayCategories,
  calendarSummaryLegendTopics,
  calendarWeekSpanLabel,
} from "@/lib/calendar";
import { addDays, localDateKey, parseLocalDate } from "@/lib/date";
import { eventTopicTone } from "@/lib/event-topics";
import type {
  CalendarMode,
  CatalogDay,
  CatalogDaySummary,
  CatalogTopic,
  EventEntityReference,
  EventItem,
} from "@/lib/types";

interface CalendarViewProps {
  /**
   * Per-day counts for the whole visible range. The grid renders totals and
   * topic chips only, so it is served by one aggregate rather than by paging
   * every event in the range through the browser.
   */
  summary: CatalogDaySummary | null;
  /**
   * Whether `summary` accounts for every day on screen. A range still being read
   * is not a range with no events, so the grid must not render a confident zero
   * over an answer that has not arrived.
   */
  summaryCovered: boolean;
  /** The selected day's events, the only events the calendar actually lists. */
  dayEvents: EventItem[];
  /** Up to a handful of events per day, used by week mode's day columns. */
  dayPreviews: Map<string, EventItem[]>;
  onDaySelect: (dayKey: string | null) => void;
  focusDate?: string;
  mode: CalendarMode;
  onRangeChange: (anchor: Date, mode: CalendarMode) => void;
  loading: boolean;
  dayLoading: boolean;
  error: string | null;
  dayHasMore: boolean;
  dayLoadingMore: boolean;
  onDayLoadMore: () => void;
  historicalWindow?: boolean;
  topics: CatalogTopic[];
  activeTopics: string[];
  onSourceSelect: (sourceKey: string) => void;
  onFacetSelect: (value: string) => void;
  onEntitySelect: (reference: EventEntityReference) => void;
  onEventTopicSelect?: (topic: string) => void;
  onTopicSelect: (topic: string) => void;
  onTopicsClear: () => void;
}

function firstOfMonth(value: Date): Date {
  return new Date(value.getFullYear(), value.getMonth(), 1);
}

function firstOfWeek(value: Date): Date {
  return addDays(value, -value.getDay());
}

function addMonths(value: Date, months: number): Date {
  return new Date(value.getFullYear(), value.getMonth() + months, 1);
}

function visibleRange(anchor: Date, mode: CalendarMode): { start: Date; end: Date } {
  if (mode === "week") {
    const start = firstOfWeek(anchor);
    return { start, end: addDays(start, 6) };
  }
  const start = firstOfMonth(anchor);
  if (mode === "six-months") {
    return { start, end: new Date(start.getFullYear(), start.getMonth() + 6, 0) };
  }
  return { start, end: new Date(start.getFullYear(), start.getMonth() + 1, 0) };
}

function formatMonth(value: Date): string {
  return new Intl.DateTimeFormat(undefined, { month: "long", year: "numeric" }).format(value);
}

function formatCalendarRange(anchor: Date, mode: CalendarMode): string {
  const range = visibleRange(anchor, mode);
  return calendarRangeLabel(range.start, range.end, mode);
}

function formatWeekEventTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Time TBD";
  return new Intl.DateTimeFormat(undefined, { hour: "numeric", minute: "2-digit" }).format(date);
}

interface CalendarDayCellProps {
  date: Date;
  day: CatalogDay | undefined;
  month: Date;
  counted: boolean;
  counting: boolean;
  selected: boolean;
  compact: boolean;
  onSelect: (key: string) => void;
}

function CalendarDayCell({
  date,
  day,
  month,
  counted,
  counting,
  selected,
  compact,
  onSelect,
}: CalendarDayCellProps) {
  const key = localDateKey(date);
  const dayEventCount = day?.event_count ?? 0;
  // A day with no entry in a range that has not been read is unknown, not empty.
  // Saying "no events" about it would be a claim the calendar cannot yet make.
  const unknown = !counted && !day;
  const categories = calendarSummaryDayCategories(day);
  const visibleCategories = categories.slice(0, 4);
  const compactCategories = categories.slice(0, 3);
  const hiddenCategoryCount = categories.length - visibleCategories.length;
  const outside = date.getMonth() !== month.getMonth();
  const dateLabel = new Intl.DateTimeFormat(undefined, {
    weekday: "long",
    month: "long",
    day: "numeric",
  }).format(date);
  const categoryLabel = categories
    .map((category) => `${category.label} ${category.eventCount}`)
    .join(", ");
  if (compact && outside) {
    return <span className="calendar-cell-placeholder" role="gridcell" />;
  }
  return (
    <button
      type="button"
      role="gridcell"
      className={[
        outside ? "is-outside" : "",
        selected ? "is-selected" : "",
        dayEventCount ? "has-events" : "",
        unknown && counting ? "is-counting" : "",
      ].filter(Boolean).join(" ")}
      aria-selected={selected}
      aria-label={`${dateLabel}: ${unknown
        ? "Counting events"
        : dayEventCount
          ? `${dayEventCount} event${dayEventCount === 1 ? "" : "s"}${categoryLabel ? `; ${categoryLabel}` : ""}`
          : "No events"}`}
      onClick={() => onSelect(key)}
    >
      <span className="calendar-cell__top">
        <span>{date.getDate()}</span>
        {dayEventCount ? <small>{dayEventCount}</small> : null}
      </span>
      {/*
        A six-month cell is about the width of two digits. Numbering each
        topic there produced overlapping pills that spilled across day
        borders, so the day's own total carries the number and the mix is
        shown as tones. Every count stays available in the cell's
        aria-label and in each tone's title.
      */}
      {compact && compactCategories.length ? (
        <span className="calendar-cell__compact-counts" aria-hidden="true">
          {compactCategories.map((category) => (
            <span
              className="calendar-cell__compact-count"
              data-topic-tone={eventTopicTone(category.topic)}
              title={`${category.label}: ${category.eventCount}`}
              key={category.topic}
            />
          ))}
        </span>
      ) : null}
      {!compact && categories.length ? (
        <span className="calendar-cell__categories" aria-hidden="true">
          {visibleCategories.map((category) => (
            <span
              className="calendar-cell__category"
              data-topic-tone={eventTopicTone(category.topic)}
              key={category.topic}
            >
              <span>{category.label}</span>
              <strong>{category.eventCount}</strong>
            </span>
          ))}
          {hiddenCategoryCount ? (
            <span className="calendar-cell__more">
              +{hiddenCategoryCount} more categor{hiddenCategoryCount === 1 ? "y" : "ies"}
            </span>
          ) : null}
        </span>
      ) : null}
    </button>
  );
}

interface CalendarMonthGridProps {
  month: Date;
  byDate: Map<string, CatalogDay>;
  /** False while the counts for this month are still being read. */
  counted: boolean;
  /** False once a read has failed, so an unknown day stops claiming to count. */
  counting: boolean;
  selectedKey: string | null;
  compact?: boolean;
  onSelect: (key: string) => void;
  /** Opens this month on its own. Given only where the month is one of several. */
  onMonthSelect?: (month: Date) => void;
  /** Opens one week of this month on its own. */
  onWeekSelect?: (weekStart: Date) => void;
}

const WEEKS_IN_GRID = 6;

/**
 * Rows of the two-column colour key the panel shows before it is opened out.
 *
 * Five is about a third of a long catalog's types and leaves the agenda in view;
 * opening the key extends the panel rather than scrolling a letterbox.
 */
const CALENDAR_LEGEND_ROWS = 5;


function CalendarMonthGrid({
  month,
  byDate,
  counted,
  counting,
  selectedKey,
  compact = false,
  onSelect,
  onMonthSelect,
  onWeekSelect,
}: CalendarMonthGridProps) {
  const start = addDays(month, -month.getDay());
  const monthLabel = formatMonth(month);
  // The rail shares the grid rather than sitting beside it, so a row that grows
  // to fit a busy day carries its own label down with it.
  const railed = Boolean(onWeekSelect);
  const weeks = Array.from({ length: WEEKS_IN_GRID }, (_, week) => ({
    start: addDays(start, week * 7),
    days: Array.from({ length: 7 }, (_, day) => addDays(start, week * 7 + day)),
  }));
  return (
    <section
      className={[
        "calendar-month",
        compact ? "is-compact" : "",
        railed ? "has-week-rail" : "",
      ].filter(Boolean).join(" ")}
    >
      {compact ? (
        <h3>
          {onMonthSelect ? (
            <button
              className="calendar-month__open"
              type="button"
              aria-label={`Open ${monthLabel}`}
              onClick={() => onMonthSelect(month)}
            >
              {monthLabel}
            </button>
          ) : monthLabel}
        </h3>
      ) : null}
      <div className="calendar-weekdays" aria-hidden="true">
        {railed ? <span className="calendar-weekdays__rail" /> : null}
        {["S", "M", "T", "W", "T", "F", "S"].map((day, index) => (
          <span key={`${day}-${index}`}>{day}</span>
        ))}
      </div>
      <div className="calendar-grid" role="grid" aria-label={monthLabel}>
        {weeks.map((week) => (
          <Fragment key={localDateKey(week.start)}>
            {railed ? (
              <button
                className="calendar-week-rail"
                type="button"
                role="rowheader"
                /*
                  A six-month cell is about thirty pixels wide, so the span does
                  not fit beside it. The rail keeps its place and its gesture there
                  and shows a chevron instead: a number in that column read as one
                  more column of dates, which is the one thing a calendar gutter
                  must not do. The span stays in the tooltip and the label, where
                  it costs no width.
                */
                title={`Open the week of ${calendarWeekSpanLabel(week.start)}`}
                aria-label={`Open the week of ${calendarWeekSpanLabel(week.start)}`}
                onClick={() => onWeekSelect?.(week.start)}
              >
                {compact
                  ? <ChevronRight aria-hidden="true" />
                  : calendarWeekSpanLabel(week.start)}
              </button>
            ) : null}
            {week.days.map((date) => (
              <CalendarDayCell
                date={date}
                day={byDate.get(localDateKey(date))}
                month={month}
                counted={counted}
                counting={counting}
                selected={localDateKey(date) === selectedKey}
                compact={compact}
                onSelect={onSelect}
                key={localDateKey(date)}
              />
            ))}
          </Fragment>
        ))}
      </div>
    </section>
  );
}

/**
 * The colour key, rendered identically wherever it appears.
 *
 * The panel shows a first screenful and the dialog shows the rest; both are the
 * same control, so a topic filters the same way and reads the same way in either
 * place.
 */
function CalendarTopicList({
  topics,
  activeTopics,
  onTopicSelect,
}: {
  topics: CatalogTopic[];
  activeTopics: string[];
  onTopicSelect: (topic: string) => void;
}) {
  return (
    <ul>
      {topics.map((topic) => {
        const active = activeTopics.includes(topic.topic);
        return (
          <li key={topic.topic}>
            <button
              type="button"
              data-topic-tone={eventTopicTone(topic.topic)}
              aria-label={`${active ? "Remove" : "Filter by"} ${topic.label} (${topic.event_count} event${topic.event_count === 1 ? "" : "s"})`}
              aria-pressed={active}
              onClick={() => onTopicSelect(topic.topic)}
            >
              <span className="calendar-topic-key__dot" aria-hidden="true" />
              <span>{topic.label}</span>
              <strong>{topic.event_count}</strong>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

interface CalendarWeekGridProps {
  start: Date;
  byDate: Map<string, CatalogDay>;
  /** False while the counts for this week are still being read. */
  counted: boolean;
  /** False once a read has failed, so an unknown day stops claiming to count. */
  counting: boolean;
  previews: Map<string, EventItem[]>;
  selectedKey: string | null;
  onSelect: (key: string) => void;
}

function CalendarWeekGrid({
  start,
  byDate,
  counted,
  counting,
  previews,
  selectedKey,
  onSelect,
}: CalendarWeekGridProps) {
  const days = Array.from({ length: 7 }, (_, index) => addDays(start, index));
  return (
    <div className="calendar-week-grid" role="grid" aria-label="Week calendar">
      {days.map((date) => {
        const key = localDateKey(date);
        const day = byDate.get(key);
        const dayEventCount = day?.event_count ?? 0;
        const unknown = !counted && !day;
        // The column previews a few events; the count beside it is authoritative
        // for the day, so the overflow is measured against the count, not the
        // preview that happened to arrive.
        const visibleEvents = (previews.get(key) ?? []).slice(0, 4);
        const hiddenEventCount = Math.max(dayEventCount - visibleEvents.length, 0);
        const selected = selectedKey === key;
        const dateLabel = new Intl.DateTimeFormat(undefined, {
          weekday: "long",
          month: "long",
          day: "numeric",
        }).format(date);
        return (
          <button
            type="button"
            role="gridcell"
            key={key}
            className={[
              selected ? "is-selected" : "",
              unknown && counting ? "is-counting" : "",
            ].filter(Boolean).join(" ")}
            aria-selected={selected}
            aria-label={`${dateLabel}: ${unknown
              ? "Counting events"
              : `${dayEventCount} event${dayEventCount === 1 ? "" : "s"}`}`}
            onClick={() => onSelect(key)}
          >
            <span className="calendar-week-day__heading">
              <span>{new Intl.DateTimeFormat(undefined, { weekday: "short" }).format(date)}</span>
              <strong>{date.getDate()}</strong>
              <small>{unknown ? "" : dayEventCount}</small>
            </span>
            <span className="calendar-week-day__events" aria-hidden="true">
              {visibleEvents.map((event) => (
                <span
                  className="calendar-week-event"
                  data-topic-tone={eventTopicTone(event.topics?.[0])}
                  key={event.canonical_event_id}
                >
                  <time>{formatWeekEventTime(event.start_at)}</time>
                  <span>{event.title}</span>
                </span>
              ))}
              {unknown
                ? <span className="calendar-week-counting">{counting ? "Counting…" : "—"}</span>
                : !dayEventCount ? <span className="calendar-week-empty">No events</span> : null}
              {hiddenEventCount ? (
                <span className="calendar-week-more">+{hiddenEventCount} more</span>
              ) : null}
            </span>
          </button>
        );
      })}
    </div>
  );
}

export function CalendarView({
  summary,
  summaryCovered,
  dayEvents,
  dayPreviews,
  onDaySelect,
  focusDate,
  mode,
  onRangeChange,
  loading,
  dayLoading,
  error,
  dayHasMore,
  dayLoadingMore,
  onDayLoadMore,
  historicalWindow = false,
  topics,
  activeTopics,
  onSourceSelect,
  onFacetSelect,
  onEntitySelect,
  onTopicSelect,
  onEventTopicSelect,
  onTopicsClear,
}: CalendarViewProps) {
  const firstSummaryDay = summary?.days[0]?.start_day;
  const firstEventDate = (firstSummaryDay ? parseLocalDate(firstSummaryDay) : null) ?? new Date();
  const [anchor, setAnchor] = useState(firstEventDate);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [legendTerm, setLegendTerm] = useState("");
  const [legendExpanded, setLegendExpanded] = useState(false);
  const legendSearch = useRef<HTMLInputElement>(null);


  useEffect(() => {
    const focused = focusDate ? parseLocalDate(focusDate) : null;
    if (!focused) return;
    const currentRange = visibleRange(anchor, mode);
    const focusedKey = localDateKey(focused);
    if (
      focusedKey >= localDateKey(currentRange.start)
      && focusedKey <= localDateKey(currentRange.end)
    ) return;
    setAnchor(focused);
    setSelectedKey(null);
  }, [anchor, focusDate, mode]);

  const byDate = useMemo(() => calendarSummaryByDate(summary), [summary]);
  /**
   * Whether these counts describe the range on screen.
   *
   * The grid may be rendering a range the summary has not been read for yet -
   * the moment after a mode switch, before the anchor and the request agree.
   * Those days are unknown, and only a range the summary covers may be called
   * empty.
   */
  const counted = Boolean(summary) && summaryCovered;

  useEffect(() => {
    const range = visibleRange(anchor, mode);
    const startKey = localDateKey(range.start);
    const endKey = localDateKey(range.end);
    if (selectedKey && selectedKey >= startKey && selectedKey <= endKey) return;
    const todayKey = localDateKey(new Date());
    if (todayKey >= startKey && todayKey <= endKey) {
      setSelectedKey(todayKey);
      return;
    }
    const firstVisible = [...byDate.keys()].sort()
      .find((key) => key >= startKey && key <= endKey);
    setSelectedKey(firstVisible ?? null);
  }, [anchor, byDate, mode, selectedKey]);

  /**
   * Let the open day follow the events when a filter moves them.
   *
   * Narrowing the types re-answers the range, and the day being read can come
   * back empty while the matches sit a few cells away - the agenda then says
   * "a quiet day" about a calendar that plainly has events in it, which reads as
   * the filter having found nothing. The open day moves to the first one that
   * did match instead.
   *
   * Guarded on the counts themselves changing, so choosing an empty day stays
   * chosen: a click does not re-answer anything, so this cannot fire on one.
   */
  const countedDays = useRef<Map<string, CatalogDay> | null>(null);
  useEffect(() => {
    if (countedDays.current === byDate) return;
    countedDays.current = byDate;
    if (!counted || !selectedKey) return;
    if ((byDate.get(selectedKey)?.event_count ?? 0) > 0) return;
    const range = visibleRange(anchor, mode);
    const startKey = localDateKey(range.start);
    const endKey = localDateKey(range.end);
    const firstWithEvents = [...byDate.keys()].sort().find((key) => (
      key >= startKey && key <= endKey && (byDate.get(key)?.event_count ?? 0) > 0
    ));
    if (firstWithEvents) setSelectedKey(firstWithEvents);
  }, [anchor, byDate, counted, mode, selectedKey]);

  // The selected day is the only day whose events are read, so the owner of the
  // catalog request needs to know it. Previously the calendar paged the entire
  // visible range instead, one request at a time, and discarded all but the
  // counts.
  useEffect(() => {
    onDaySelect(selectedKey);
  }, [onDaySelect, selectedKey]);

  useEffect(() => {
    if (focusDate && parseLocalDate(focusDate)) return;
    const firstDay = summary?.days[0]?.start_day;
    const firstDate = firstDay ? parseLocalDate(firstDay) : null;
    if (!firstDate) return;
    const range = visibleRange(anchor, mode);
    const startKey = localDateKey(range.start);
    const endKey = localDateKey(range.end);
    const currentRangeHasEvent = [...byDate.keys()]
      .some((key) => key >= startKey && key <= endKey);
    if (!currentRangeHasEvent) setAnchor(firstDate);
  }, [anchor, byDate, focusDate, mode, summary]);

  const range = visibleRange(anchor, mode);
  // The grid may render a range the summary has not been read for yet — the
  // moment after a mode switch, before the anchor and the request agree. Those
  // days are unknown, and only a range the summary covers may be called empty.
  // A failed read is not a slow one: cells must stop pretending to count.
  const counting = !counted && !error;
  /**
   * Counts are on screen, but for the filter that was active a moment ago.
   *
   * Clearing them first is what made a filter change blink the page away: the
   * grid emptied, the agenda emptied, and both refilled a second later. They are
   * held instead and dimmed, so the layout never moves and nothing claims the
   * held numbers are current.
   */
  const restating = !counted && Boolean(summary);
  const displayMonths = mode === "six-months"
    ? [0, 1, 2, 3, 4, 5].map((offset) => addMonths(range.start, offset))
    : [];
  const selectedEvents = selectedKey ? dayEvents : [];
  const selectedDay = selectedKey ? byDate.get(selectedKey) : undefined;
  const selectedDayCount = selectedDay?.event_count ?? 0;
  /** The selected day's count is unknown until its range has been read. */
  const selectedDayCounted = counted || selectedDay !== undefined;
  const selectedDate = selectedKey ? parseLocalDate(selectedKey) : null;
  const selectedHistorical = Boolean(
    selectedKey && historicalWindow && selectedKey < localDateKey(new Date()),
  );
  /**
   * The colour key, held steady across a reload.
   *
   * The key is derived from the range's counts, and choosing a type re-reads the
   * range - so for that moment there are no counts and the key would collapse to
   * the one type just picked, then repopulate. That emptying and refilling is the
   * flicker. The last list anybody actually saw is kept until real counts land,
   * and dimmed meanwhile, so rows change their numbers in place instead of
   * leaving the screen.
   */
  const freshLegendTopics = useMemo(
    () => calendarSummaryLegendTopics(summary, topics, activeTopics),
    [activeTopics, summary, topics],
  );
  const [legendTopics, setLegendTopics] = useState<CatalogTopic[]>(freshLegendTopics);
  useEffect(() => {
    // Only counts that actually describe the visible range may reorder the key.
    // Recomputing it against the previous range's counts re-sorts the rows under
    // the reader mid-pick, which is the churn the hold exists to prevent.
    if (!summary || !summaryCovered) return;
    setLegendTopics(freshLegendTopics);
  }, [freshLegendTopics, summary, summaryCovered]);
  const legendPending = Boolean(summary) && !summaryCovered;
  /**
   * Take a type and hand the search back with its term selected.
   *
   * Naming one type is rarely the whole intent - the picks row exists because
   * people pick several - so the next term has to be typeable immediately.
   * Selecting beats clearing: the word that found this type stays legible, and
   * typing over it replaces it anyway, so nothing is lost either way. Enter and
   * a click go through here together, because two ways of choosing the same
   * thing behaving differently was the bug that started this.
   */
  const chooseLegendTopic = (topic: string) => {
    onTopicSelect(topic);
    if (!legendTerm) return;
    window.requestAnimationFrame(() => {
      legendSearch.current?.focus();
      legendSearch.current?.select();
    });
  };
  const hiddenTopicCount = Math.max(legendTopics.length - CALENDAR_LEGEND_ROWS * 2, 0);
  // A search speaks for itself: it shows everything it matched, however many.
  const showEveryTopic = legendExpanded || Boolean(legendTerm.trim());
  const visibleLegendTopics = useMemo(() => {
    const needle = legendTerm.trim().toLocaleLowerCase();
    if (!needle) return legendTopics;
    return legendTopics.filter((topic) => topic.label.toLocaleLowerCase().includes(needle));
  }, [legendTerm, legendTopics]);

  const shiftRange = (direction: -1 | 1) => {
    const next = mode === "week"
      ? addDays(anchor, direction * 7)
      : addMonths(anchor, direction * (mode === "six-months" ? 6 : 1));
    setAnchor(next);
    setSelectedKey(null);
    onRangeChange(next, mode);
  };
  const changeMode = (nextMode: CalendarMode) => {
    if (nextMode === mode) return;
    const nextAnchor = selectedDate ?? anchor;
    setAnchor(nextAnchor);
    onRangeChange(nextAnchor, nextMode);
  };
  const goToToday = () => {
    const today = new Date();
    setAnchor(today);
    setSelectedKey(localDateKey(today));
    onRangeChange(today, mode);
  };
  /**
   * Drill into one span of the range on screen.
   *
   * Six months of two-digit cells and a month of dense ones are both overviews;
   * the way out of an overview is the part of it you were looking at. The heading
   * of a month and the rail beside a week are those parts, so they open directly
   * rather than making the reader pick the mode and then navigate back to where
   * they already were.
   */
  const openRange = (nextAnchor: Date, nextMode: CalendarMode) => {
    setAnchor(nextAnchor);
    setSelectedKey(null);
    onRangeChange(nextAnchor, nextMode);
  };

  return (
    <section className="workspace calendar-view">
      <header className="workspace-heading">
        <div>
          <p>EXPLORE BY DATE</p>
          <h1>Calendar</h1>
        </div>
        <div className="view-result-state">
          <span>
            {/*
              A range that has not been read has no total to state. Reporting one
              anyway printed "0 events" over a range still arriving, which is the
              same wrong claim the grid used to make in its cells.
            */}
            {!counted || !summary
              ? error ? "Count unavailable" : "Counting events…"
              : `${summary.total_event_count.toLocaleString()} events${
                loading ? " · refreshing…" : " · range complete"
              }`}
          </span>
          {loading ? (
            <LoaderCircle className="spin" aria-hidden="true" />
          ) : null}
        </div>
      </header>

      {error ? <p className="workspace-error" role="alert">{error}</p> : null}

      <div className="calendar-layout">
        <div className={`calendar-grid-shell${restating ? " is-restating" : ""}`}>
          <header className="calendar-toolbar">
            <h2>{formatCalendarRange(anchor, mode)}</h2>
            <div className="calendar-toolbar__actions">
              <button className="calendar-today" type="button" onClick={goToToday}>
                Today
              </button>
              <div className="calendar-mode-switch" aria-label="Calendar view" role="group">
                {([
                  ["week", "Week"],
                  ["month", "Month"],
                  ["six-months", "6 months"],
                ] as const).map(([value, label]) => (
                  <button
                    type="button"
                    key={value}
                    className={mode === value ? "is-active" : ""}
                    aria-pressed={mode === value}
                    onClick={() => changeMode(value)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="calendar-range-navigation">
              <button
                type="button"
                aria-label={`Previous ${mode === "six-months" ? "six months" : mode}`}
                onClick={() => shiftRange(-1)}
              >
                <ChevronLeft aria-hidden="true" />
              </button>
              <button
                type="button"
                aria-label={`Next ${mode === "six-months" ? "six months" : mode}`}
                onClick={() => shiftRange(1)}
              >
                <ChevronRight aria-hidden="true" />
              </button>
              </div>
            </div>
          </header>
          {mode === "week" ? (
            <CalendarWeekGrid
              start={range.start}
              byDate={byDate}
              counted={counted}
              counting={counting}
              previews={dayPreviews}
              selectedKey={selectedKey}
              onSelect={setSelectedKey}
            />
          ) : null}
          {mode === "month" ? (
            <CalendarMonthGrid
              month={range.start}
              byDate={byDate}
              counted={counted}
              counting={counting}
              selectedKey={selectedKey}
              onSelect={setSelectedKey}
              onWeekSelect={(weekStart) => openRange(weekStart, "week")}
            />
          ) : null}
          {mode === "six-months" ? (
            <div className="calendar-six-months">
              {displayMonths.map((displayMonth) => (
                <CalendarMonthGrid
                  month={displayMonth}
                  byDate={byDate}
                  counted={counted}
                  counting={counting}
                  selectedKey={selectedKey}
                  compact
                  onSelect={setSelectedKey}
                  onMonthSelect={(nextMonth) => openRange(nextMonth, "month")}
                  onWeekSelect={(weekStart) => openRange(weekStart, "week")}
                  key={localDateKey(displayMonth)}
                />
              ))}
            </div>
          ) : null}
        </div>

        <aside className={`calendar-agenda${dayLoading && dayEvents.length ? " is-restating" : ""}`}>
          <header>
            <p>{selectedDate
              ? new Intl.DateTimeFormat(undefined, { weekday: "long" }).format(selectedDate)
              : "SELECT A DAY"}</p>
            <h2>{selectedDate
              ? new Intl.DateTimeFormat(undefined, {
                month: "long",
                day: "numeric",
              }).format(selectedDate)
              : "Agenda"}</h2>
            <span>{selectedDayCounted
              ? `${selectedDayCount || "No"} event${selectedDayCount === 1 ? "" : "s"}`
              : counting ? "Counting events…" : "Count unavailable"}</span>
          </header>
          {legendTopics.length ? (
            <section
              className={[
                "calendar-topic-key",
                legendExpanded ? "is-expanded" : "",
                legendPending ? "is-pending" : "",
              ].filter(Boolean).join(" ")}
              aria-labelledby="calendar-topic-key-title"
            >
              <header>
                <div>
                  <p>COLOR KEY + FILTER</p>
                  <h3 id="calendar-topic-key-title">Event types</h3>
                </div>
                {activeTopics.length ? (
                  <button
                    className="calendar-topic-key__clear"
                    type="button"
                    onClick={onTopicsClear}
                  >
                    Clear types
                  </button>
                ) : null}
              </header>
              <div className="calendar-topic-key__options">
                <p className="calendar-topic-key__hint">
                  Select a color to filter this calendar. Counts reflect the visible events.
                </p>
                {/*
                  The search is here from the start rather than behind the expand.
                  Naming a type is the fastest way to reach one, and hiding that
                  behind "show all" made the short list the only thing a reader
                  could act on without a detour.
                */}
                <label className="calendar-topic-key__search">
                  <Search aria-hidden="true" />
                  <span className="sr-only">Search event types</span>
                  <input
                    ref={legendSearch}
                    value={legendTerm}
                    placeholder="Search event types"
                    maxLength={60}
                    onChange={(event) => setLegendTerm(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Escape") {
                        event.preventDefault();
                        setLegendTerm("");
                        return;
                      }
                      // Enter takes the top match and clears the box, so several
                      // types can be named in a row without reaching for the list.
                      if (event.key !== "Enter") return;
                      event.preventDefault();
                      const top = legendTerm.trim() ? visibleLegendTopics[0] : undefined;
                      if (!top) return;
                      chooseLegendTopic(top.topic);
                    }}
                  />
                  {legendTerm ? (
                    <button
                      className="calendar-topic-key__search-clear"
                      type="button"
                      aria-label="Clear the search"
                      onClick={() => setLegendTerm("")}
                    >
                      <X aria-hidden="true" />
                    </button>
                  ) : null}
                </label>
                {/*
                  In a long list the picks scatter, so they are restated as a row
                  that can be unpicked without hunting - the same move the filter
                  chips offer.
                */}
                {activeTopics.length ? (
                  <div className="calendar-topic-key__picks">
                    {activeTopics.map((topic) => {
                      const picked = legendTopics.find((option) => option.topic === topic);
                      return (
                        <button
                          type="button"
                          key={topic}
                          data-topic-tone={eventTopicTone(topic)}
                          aria-label={`Remove ${picked?.label ?? topic}`}
                          onClick={() => onTopicSelect(topic)}
                        >
                          <span>{picked?.label ?? topic}</span>
                          <X aria-hidden="true" />
                        </button>
                      );
                    })}
                  </div>
                ) : null}
                {/*
                  The list is bounded to the same height whichever way it is set,
                  so neither expanding nor searching moves the agenda below it.
                */}
                <div className="calendar-topic-key__list">
                  {visibleLegendTopics.length ? (
                    <CalendarTopicList
                      topics={showEveryTopic
                        ? visibleLegendTopics
                        : visibleLegendTopics.slice(0, CALENDAR_LEGEND_ROWS * 2)}
                      activeTopics={activeTopics}
                      onTopicSelect={chooseLegendTopic}
                    />
                  ) : (
                    <p className="calendar-topic-key__empty">No matching type</p>
                  )}
                </div>
                {/*
                  A search already shows every match, so the toggle only speaks
                  for the unsearched list.
                */}
                {!legendTerm.trim() && hiddenTopicCount ? (
                  <button
                    className="calendar-topic-key__more"
                    type="button"
                    aria-expanded={legendExpanded}
                    onClick={() => setLegendExpanded((current) => !current)}
                  >
                    {legendExpanded ? "Show fewer" : `Show all ${legendTopics.length} types`}
                    {legendExpanded ? null : <span>+{hiddenTopicCount}</span>}
                  </button>
                ) : null}
              </div>
            </section>
          ) : null}
          <EventList
            events={selectedEvents}
            /* Held while the next day is read, so the panel never empties. */
            compact
            expandedId={expandedId}
            onExpandedChange={setExpandedId}
            onSourceSelect={onSourceSelect}
            onFacetSelect={onFacetSelect}
            onEntitySelect={onEntitySelect}
            onTopicSelect={onEventTopicSelect ?? onTopicSelect}
            emptyTitle={!selectedDate
              ? "Select a day"
              : dayLoading
                ? "Loading the agenda…"
                : selectedHistorical ? "No retained events" : "A quiet day"}
            emptyCopy={!selectedDate
              ? "Choose any day in the month to inspect its agenda."
              : dayLoading
                ? "Reading this day's events."
              : selectedHistorical
                ? "No matching event is retained for the selected day. Older listings may have rolled off before archival capture."
                : "Nothing is scheduled for this day. Pick another day or adjust the active filters."}
          />
          {dayHasMore ? (
            <button
              className="calendar-agenda__more"
              type="button"
              disabled={dayLoadingMore}
              onClick={onDayLoadMore}
            >
              {dayLoadingMore ? <LoaderCircle className="spin" aria-hidden="true" /> : null}
              {dayLoadingMore
                ? "Loading"
                : `Show more (${selectedEvents.length} of ${selectedDayCount})`}
            </button>
          ) : null}
        </aside>
      </div>
    </section>
  );
}
