"use client";

import {
  CalendarDays,
  CalendarPlus,
  ChevronLeft,
  ChevronRight,
  Plus,
  X,
} from "lucide-react";
import {
  type FocusEvent,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  addDays,
  localDateKey,
  parseLocalDate,
} from "@/lib/date";
import type { DatePreset } from "@/lib/types";

interface DateRangePickerProps {
  controlId?: string;
  variant?: "control" | "chip" | "add";
  start: string;
  end: string;
  summary: string;
  /** Hover text, where a rolling window puts the dates it resolves to today. */
  summaryTitle?: string;
  rangeCount: number;
  editing: boolean;
  removable?: boolean;
  removeLabel?: string;
  disabled?: boolean;
  /** The rolling window in force, when the filter is one rather than two fixed dates. */
  activePreset?: DatePreset;
  /** Choosing a rolling window instead of pinning two dates. */
  onPreset?: (preset: DatePreset) => void;
  onApply: (start: string, end: string) => void;
  onClear: () => void;
}

/**
 * Windows that mean the same thing every time they are asked.
 *
 * A range picked on the calendar is two fixed dates, which is what you want for "the conference in
 * March" and exactly what you do not want in a selection you keep: saved in August, it still says
 * August in December. These re-resolve against the day they are read.
 */
const ROLLING_WINDOWS: ReadonlyArray<{ value: DatePreset; label: string }> = [
  { value: "today", label: "Today" },
  { value: "week", label: "This week" },
  { value: "workweek", label: "Work week" },
  { value: "weekend", label: "This weekend" },
  { value: "nextweek", label: "Next week" },
  { value: "nextworkweek", label: "Next work week" },
  { value: "nextweekend", label: "Next weekend" },
  { value: "month", label: "This month" },
];

const WEEKDAYS = ["S", "M", "T", "W", "T", "F", "S"];

function monthStart(value: Date): Date {
  return new Date(value.getFullYear(), value.getMonth(), 1);
}

function calendarDays(month: Date): Date[] {
  const first = monthStart(month);
  const start = addDays(first, -first.getDay());
  return Array.from({ length: 42 }, (_, index) => addDays(start, index));
}

function friendlyDate(value: string): string {
  const parsed = parseLocalDate(value);
  if (!parsed) return "Choose";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(parsed);
}

export function DateRangePicker({
  controlId,
  variant = "control",
  start,
  end,
  summary,
  summaryTitle,
  rangeCount,
  editing,
  removable = false,
  removeLabel = "Remove date filter",
  disabled = false,
  activePreset,
  onPreset,
  onApply,
  onClear,
}: DateRangePickerProps) {
  const controlRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [draftStart, setDraftStart] = useState(start);
  const [draftEnd, setDraftEnd] = useState(end);
  const [selectingEnd, setSelectingEnd] = useState(false);
  const [month, setMonth] = useState(() => monthStart(parseLocalDate(start) ?? new Date()));
  const days = useMemo(() => calendarDays(month), [month]);

  const openPicker = () => {
    const parsedStart = parseLocalDate(start) ?? new Date();
    setDraftStart(start || localDateKey(parsedStart));
    setDraftEnd(end || localDateKey(parsedStart));
    setSelectingEnd(false);
    setMonth(monthStart(parsedStart));
    setOpen(true);
  };

  const chooseDay = (date: Date) => {
    const key = localDateKey(date);
    if (!selectingEnd) {
      setDraftStart(key);
      setDraftEnd("");
      setSelectingEnd(true);
      return;
    }
    // The second endpoint completes the range, which is the whole answer -- so
    // it applies and dismisses rather than waiting for a button to repeat it.
    const start = key < draftStart ? key : draftStart;
    const end = key < draftStart ? draftStart : key;
    setDraftStart(start);
    setDraftEnd(end);
    setSelectingEnd(false);
    onApply(start, end);
    setOpen(false);
  };

  const handleBlur = (event: FocusEvent<HTMLDivElement>) => {
    const control = event.currentTarget;
    window.setTimeout(() => {
      if (!control.contains(document.activeElement)) setOpen(false);
    }, 0);
  };

  return (
    <div
      className={[
        "date-range-picker",
        `date-range-picker--${variant}`,
        variant === "chip" ? "active-filter-chip active-filter-chip--date" : "",
      ].filter(Boolean).join(" ")}
      ref={controlRef}
      onBlur={handleBlur}
    >
      {variant === "chip" && removable ? (
        <button
          className="active-filter-chip__remove"
          type="button"
          aria-label={removeLabel}
          onClick={() => {
            onClear();
            setOpen(false);
          }}
        >
          <X aria-hidden="true" />
        </button>
      ) : null}

      <button
        id={controlId}
        className={
          variant === "chip"
            ? "active-filter-chip__body date-range-picker__trigger--chip"
            : variant === "add"
              ? "active-filter-add date-range-picker__trigger--add"
              : "date-range-picker__trigger"
        }
        type="button"
        disabled={disabled}
        title={summaryTitle}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => {
          if (open) {
            setOpen(false);
          } else {
            openPicker();
          }
        }}
      >
        {variant === "chip" ? (
          <>
            <small>Date</small>
            <strong>{summary}</strong>
          </>
        ) : variant === "add" ? (
          <>
            <CalendarPlus aria-hidden="true" />
            Add dates
          </>
        ) : (
          <>
            <span>Dates</span>
            <CalendarDays aria-hidden="true" />
            <strong>{summary}</strong>
            <small>{rangeCount > 1 ? `${rangeCount} ranges` : ""}</small>
            <Plus aria-hidden="true" />
          </>
        )}
      </button>

      {open ? (
        <div
          className="date-range-picker__popover"
          role="dialog"
          aria-label={editing ? "Edit date range" : "Add date range"}
        >
          <div className="date-range-picker__mode">
            <span>{editing ? "Edit range" : "Add another range"}</span>
            <small>{editing ? "Changes only this date filter" : "Matches either date range"}</small>
          </div>
          {onPreset ? (
            <div
              className="date-range-picker__rolling"
              role="group"
              aria-label="Rolling date windows"
            >
              {ROLLING_WINDOWS.map((window) => (
                <button
                  key={window.value}
                  className={activePreset === window.value ? "is-active" : ""}
                  type="button"
                  aria-pressed={activePreset === window.value}
                  onClick={() => {
                    onPreset(window.value);
                    setOpen(false);
                  }}
                >
                  {window.label}
                </button>
              ))}
            </div>
          ) : null}

          <header>
            <button
              type="button"
              aria-label="Previous month"
              onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}
            >
              <ChevronLeft aria-hidden="true" />
            </button>
            <strong>
              {new Intl.DateTimeFormat(undefined, {
                month: "long",
                year: "numeric",
              }).format(month)}
            </strong>
            <button
              type="button"
              aria-label="Next month"
              onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}
            >
              <ChevronRight aria-hidden="true" />
            </button>
          </header>

          <div className="date-range-picker__summary" aria-live="polite">
            <span>
              <small>Start</small>
              {friendlyDate(draftStart)}
            </span>
            <span>
              <small>End</small>
              {draftEnd ? friendlyDate(draftEnd) : "Choose an end date"}
            </span>
          </div>

          <div className="date-range-picker__weekdays" aria-hidden="true">
            {WEEKDAYS.map((weekday, index) => <span key={`${weekday}:${index}`}>{weekday}</span>)}
          </div>
          <div className="date-range-picker__calendar">
            {days.map((date) => {
              const key = localDateKey(date);
              const isOutside = date.getMonth() !== month.getMonth();
              const inRange = Boolean(
                draftStart
                && draftEnd
                && key >= draftStart
                && key <= draftEnd,
              );
              const isEndpoint = key === draftStart || key === draftEnd;
              return (
                <button
                  key={key}
                  className={[
                    isOutside ? "is-outside" : "",
                    inRange ? "is-in-range" : "",
                    isEndpoint ? "is-endpoint" : "",
                  ].filter(Boolean).join(" ")}
                  type="button"
                  aria-pressed={isEndpoint}
                  onClick={() => chooseDay(date)}
                >
                  {date.getDate()}
                </button>
              );
            })}
          </div>

          {/* Nothing is staged, so a picker that adds a range has nothing to cancel. */}
          {editing && removable ? (
            <footer>
              <button
                type="button"
                onClick={() => {
                  onClear();
                  setOpen(false);
                }}
              >
                Remove range
              </button>
            </footer>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
