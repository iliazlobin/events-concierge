"use client";

import { LocateFixed, Maximize2 } from "lucide-react";
import type { CSSProperties, KeyboardEvent as ReactKeyboardEvent, RefObject } from "react";
import { Fragment, useCallback, useEffect, useRef } from "react";

import {
  dayCompactLabel,
  dayFullLabel,
  stepDay,
} from "@/lib/map-days";
import type { MapDayModel } from "@/lib/map-days";

interface MapDayTrackProps {
  model: MapDayModel;
  activeDay: string | null;
  peekDay: string | null;
  peekEnabled: boolean;
  trackRef: RefObject<HTMLDivElement | null>;
  onActivate: (dayKey: string | null) => void;
  onPeek: (dayKey: string | null) => void;
  onFit: (dayKey: string | null) => void;
}

/** Long enough that a fast sweep does not strobe, short enough to feel like a preview. */
const PEEK_ENTER_MS = 90;
const PEEK_LEAVE_MS = 80;

export function MapDayTrack({
  model,
  activeDay,
  peekDay,
  peekEnabled,
  trackRef,
  onActivate,
  onPeek,
  onFit,
}: MapDayTrackProps) {
  const emphasisDay = peekDay ?? activeDay;
  const cellRefs = useRef(new Map<string, HTMLButtonElement>());
  const peekTimer = useRef<number | null>(null);
  const canPeek = useRef(false);

  useEffect(() => {
    canPeek.current = window.matchMedia("(hover: hover)").matches;
    return () => {
      if (peekTimer.current !== null) window.clearTimeout(peekTimer.current);
    };
  }, []);

  const schedulePeek = useCallback((dayKey: string | null) => {
    if (!peekEnabled || !canPeek.current) return;
    if (peekTimer.current !== null) window.clearTimeout(peekTimer.current);
    peekTimer.current = window.setTimeout(
      () => onPeek(dayKey),
      dayKey ? PEEK_ENTER_MS : PEEK_LEAVE_MS,
    );
  }, [onPeek, peekEnabled]);

  /**
   * Selection follows focus across the track: activation is one state write and is
   * fully reversible, so making the arrow keys commit costs nothing and saves a press.
   */
  const focusDay = useCallback((dayKey: string | null) => {
    onActivate(dayKey);
    const target = cellRefs.current.get(dayKey ?? "all");
    target?.focus();
  }, [onActivate]);

  const handleKeyDown = useCallback((event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    if (event.key === "ArrowRight") {
      event.preventDefault();
      focusDay(stepDay(model.keys, activeDay, 1));
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      focusDay(activeDay ? stepDay(model.keys, activeDay, -1) : null);
    } else if (event.key === "Home") {
      event.preventDefault();
      focusDay(null);
    } else if (event.key === "End") {
      event.preventDefault();
      focusDay(model.keys[model.keys.length - 1] ?? null);
    }
  }, [activeDay, focusDay, model.keys]);

  const offCount = Math.max(0, model.activeTotal - model.activeInView);

  return (
    <div
      className="map-day-track"
      ref={trackRef}
      role="toolbar"
      aria-orientation="horizontal"
      aria-label="Emphasize a day on the map"
      onKeyDown={handleKeyDown}
      onPointerLeave={() => schedulePeek(null)}
    >
      <button
        className="map-day-track__all"
        type="button"
        ref={(node) => {
          if (node) cellRefs.current.set("all", node);
          else cellRefs.current.delete("all");
        }}
        tabIndex={activeDay === null ? 0 : -1}
        aria-pressed={activeDay === null}
        aria-label={`All days, ${model.totalInView} events in this area`}
        title="Show all days (0)"
        onClick={() => onActivate(null)}
        onPointerEnter={() => schedulePeek(null)}
      >
        <LocateFixed aria-hidden="true" />
        <span>
          <small>ALL</small>
          <strong>{model.cells.length} days</strong>
        </span>
      </button>

      <div className="map-day-track__scroll">
        {model.cells.map((cell) => {
          const active = activeDay === cell.key;
          const peeked = peekDay === cell.key && !active;
          const muted = emphasisDay !== null && emphasisDay !== cell.key;
          const empty = cell.inView === 0;
          const label = empty
            ? `${dayFullLabel(cell.key)}: no events in this area, ${cell.total} elsewhere`
            : `${dayFullLabel(cell.key)}: ${cell.inView} of ${cell.total} events in this area`;
          return (
            <Fragment key={cell.key}>
              {cell.gapBefore ? (
                <span
                  className="map-day-track__gap"
                  aria-hidden="true"
                  title={`${cell.gapBefore} ${cell.gapBefore === 1 ? "day" : "days"} with no mapped events`}
                />
              ) : null}
              <button
                className={[
                  "map-day-cell",
                  active ? "is-active" : "",
                  peeked ? "is-peeked" : "",
                  muted ? "is-muted" : "",
                  empty ? "is-empty" : "",
                ].filter(Boolean).join(" ")}
                type="button"
                ref={(node) => {
                  if (node) cellRefs.current.set(cell.key, node);
                  else cellRefs.current.delete(cell.key);
                }}
                style={{
                  "--day-weight": model.maxTotal ? cell.total / model.maxTotal : 0,
                  "--coverage": cell.total ? cell.inView / cell.total : 0,
                } as CSSProperties}
                tabIndex={active ? 0 : -1}
                aria-pressed={active}
                aria-label={label}
                title={label}
                onClick={() => onActivate(active ? null : cell.key)}
                onPointerEnter={() => schedulePeek(cell.key)}
              >
                <span className="map-day-cell__weekday">
                  {cell.monthLabel ?? cell.weekday}
                </span>
                <span className="map-day-cell__date">{cell.dayOfMonth}</span>
                <span className="map-day-cell__count">{cell.inView}</span>
                <span className="map-day-cell__bar" aria-hidden="true" />
              </button>
            </Fragment>
          );
        })}
      </div>

      <div className="map-day-track__status">
        {activeDay ? (
          <>
            <span aria-hidden="true">
              {model.activeInView} here{offCount ? ` · ${offCount} off` : ""}
            </span>
            <button
              className="map-day-track__fit"
              type="button"
              title={`Fit map to ${dayCompactLabel(activeDay)} (F)`}
              aria-label={`Fit the map to ${dayFullLabel(activeDay)}`}
              onClick={() => onFit(activeDay)}
            >
              <Maximize2 aria-hidden="true" />
            </button>
          </>
        ) : (
          <span aria-hidden="true">{model.totalInView} in view</span>
        )}
      </div>
    </div>
  );
}
