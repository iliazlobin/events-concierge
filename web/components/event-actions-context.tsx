"use client";

import { createContext, useContext, type ReactNode } from "react";
import type { MuseStatus } from "@/lib/muse";
import type { EventItem } from "@/lib/types";

export interface EventActions {
  onSourceSelect?: (sourceKey: string) => void;
  muse?: {
    statuses: ReadonlyMap<string, MuseStatus>;
    pendingIds: ReadonlySet<string>;
    onSignup: (event: EventItem) => void;
  };
}

const EventActionsContext = createContext<EventActions | null>(null);

/** App-owned callbacks keep event controls consistent across every catalog view. */
export function EventActionsProvider({ value, children }: { value: EventActions; children: ReactNode }) {
  return <EventActionsContext.Provider value={value}>{children}</EventActionsContext.Provider>;
}

export function useEventActions(): EventActions | null {
  return useContext(EventActionsContext);
}

/** Match the displayed calendar label before falling back to the event's source IDs. */
export function eventSourceFilter(event: EventItem): { label: string; sourceKey: string | null } {
  const label = event.calendar_labels?.find(value => value.trim())
    ?? event.sources.find(source => source.label?.trim())?.label
    ?? event.providers?.find(value => value.trim())
    ?? event.sources.find(source => source.provider?.trim())?.provider
    ?? event.sources.find(source => source.source?.trim())?.source
    ?? "Event";
  const matching = event.sources.find(source => source.source_key?.trim() && source.label?.trim() === label.trim());
  const sourceKey = matching?.source_key
    ?? event.source_keys?.find(value => value.trim())
    ?? event.sources.find(source => source.source_key?.trim())?.source_key
    ?? null;
  return { label, sourceKey };
}
