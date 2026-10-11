"use client";

import { useEffect, useRef } from "react";
import { LoaderCircle } from "lucide-react";
import { museEligibility, museStatusLabel } from "@/lib/muse";
import type { EventItem } from "@/lib/types";
import { useEventActions } from "@/components/event-actions-context";
import { MuseIcon } from "@/components/muse-icon";

/** Available only when the app supplies signed-in, enabled Muse actions. */
export function EventMuseAction({ event }: { event: EventItem }) {
  const muse = useEventActions()?.muse;
  const button = useRef<HTMLButtonElement>(null);
  const watchEvent = muse?.watchEvent;
  useEffect(() => {
    const element = button.current;
    if (!watchEvent || !element) return;
    let stopWatching: (() => void) | null = null;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) {
        stopWatching ??= watchEvent(event.canonical_event_id);
      } else { stopWatching?.(); stopWatching = null; }
    });
    observer.observe(element);
    return () => { observer.disconnect(); stopWatching?.(); };
  }, [event.canonical_event_id, watchEvent]);
  if (!muse) return null;
  const status = muse.statuses.get(event.canonical_event_id);
  const pending = muse.pendingIds.has(event.canonical_event_id);
  const unavailable = status ? null : museEligibility(event);
  return <button ref={button}
    type="button"
    className={`event-muse-action${status ? " is-queued" : ""}`}
    disabled={pending || Boolean(unavailable)}
    aria-busy={pending || undefined}
    aria-label={status ? `View registration for ${event.title}` : `Sign up with Muse for ${event.title}`}
    title={pending ? "Queueing with Muse" : status ? `${museStatusLabel[status]} · View registration` : unavailable ?? "Sign up with Muse"}
    onPointerDown={interaction => interaction.stopPropagation()}
    onClick={interaction => {
      interaction.preventDefault();
      interaction.stopPropagation();
      muse.onSignup(event);
    }}
  >{pending ? <LoaderCircle className="spin" aria-hidden="true" /> : <MuseIcon />}
    {status ? <span className="event-muse-action__dot" aria-hidden="true" /> : null}
  </button>;
}
