"use client";

import { useEffect, useId, useRef } from "react";
import { Check, Clock3, Hourglass, LoaderCircle, TriangleAlert, X } from "lucide-react";
import { museEligibility, museStatusLabel, type MuseStatus } from "@/lib/muse";
import type { EventItem } from "@/lib/types";
import { useEventActions } from "@/components/event-actions-context";
import { MuseIcon } from "@/components/muse-icon";

const statusBadges: Record<MuseStatus, typeof Clock3> = {
  queued: Clock3, in_progress: LoaderCircle, registered: Check,
  needs_input: TriangleAlert, uncertain: TriangleAlert, failed: X,
  waitlisted: Hourglass, awaiting_approval: Hourglass,
};

/** Available only when the app supplies signed-in, enabled Muse actions. */
export function EventMuseAction({ event }: { event: EventItem }) {
  const muse = useEventActions()?.muse;
  const button = useRef<HTMLButtonElement>(null);
  const descriptionId = useId();
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
  const Badge = status ? statusBadges[status] : null;
  const description = pending ? "Saving registration" : status ? museStatusLabel[status] : unavailable;
  return <button ref={button}
    type="button"
    className="event-muse-action"
    data-registration-state={pending ? "saving" : status ?? (unavailable ? "unavailable" : "available")}
    disabled={pending || Boolean(unavailable)}
    aria-busy={pending || undefined}
    aria-label={status ? `View registration for ${event.title}` : `Sign up with Muse for ${event.title}`}
    aria-describedby={description ? descriptionId : undefined}
    title={pending ? "Queueing with Muse" : status ? `${museStatusLabel[status]} · View registration` : unavailable ?? "Sign up with Muse"}
    onPointerDown={interaction => interaction.stopPropagation()}
    onClick={interaction => {
      interaction.preventDefault();
      interaction.stopPropagation();
      muse.onSignup(event);
    }}
  >{pending ? <LoaderCircle className="spin" aria-hidden="true" /> : <MuseIcon />}
    {!pending && Badge ? <span className="event-muse-action__badge" aria-hidden="true">
      <Badge className={status === "in_progress" ? "spin" : undefined} />
    </span> : null}
    {description ? <span id={descriptionId} className="sr-only">{description}</span> : null}
  </button>;
}
