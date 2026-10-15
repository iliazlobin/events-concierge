"use client";

import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
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
  const hint = useRef<HTMLDivElement>(null);
  const dismissTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [hintOpen, setHintOpen] = useState(false);
  const [hintPosition, setHintPosition] = useState<{ left: number; top: number } | null>(null);
  const status = muse?.statuses.get(event.canonical_event_id);
  const pending = muse?.pendingIds.has(event.canonical_event_id) ?? false;
  const unavailable = muse && !status ? museEligibility(event) : null;
  const showHint = hintOpen && Boolean(unavailable) && !pending;
  const cancelDismiss = () => {
    if (dismissTimer.current) clearTimeout(dismissTimer.current);
    dismissTimer.current = null;
  };
  const openHint = () => {
    if (!unavailable || pending) return;
    cancelDismiss();
    setHintOpen(true);
  };
  const leaveHint = () => {
    cancelDismiss();
    // Let the pointer cross the small gap to the portaled help text.
    dismissTimer.current = setTimeout(() => {
      if (document.activeElement !== button.current && !button.current?.matches(":hover")
        && !hint.current?.matches(":hover")) setHintOpen(false);
    }, 120);
  };
  useEffect(() => () => {
    if (dismissTimer.current) clearTimeout(dismissTimer.current);
  }, []);
  useLayoutEffect(() => {
    if (!showHint) return;
    const positionHint = () => {
      const anchor = button.current?.getBoundingClientRect();
      const text = hint.current?.getBoundingClientRect();
      if (!anchor || !text) return;
      const gutter = 8;
      const viewportWidth = document.documentElement.clientWidth;
      const viewportHeight = window.innerHeight;
      const below = anchor.bottom + gutter;
      const above = anchor.top - text.height - gutter;
      const top = below + text.height <= viewportHeight - gutter || above < gutter ? below : above;
      setHintPosition({
        left: Math.max(gutter, Math.min(anchor.right - text.width, viewportWidth - text.width - gutter)),
        top: Math.max(gutter, Math.min(top, viewportHeight - text.height - gutter)),
      });
    };
    positionHint();
    window.addEventListener("resize", positionHint);
    window.addEventListener("scroll", positionHint, true);
    return () => {
      window.removeEventListener("resize", positionHint);
      window.removeEventListener("scroll", positionHint, true);
    };
  }, [showHint, unavailable]);
  useEffect(() => {
    if (!showHint) return;
    const dismissOutside = (interaction: PointerEvent) => {
      if (interaction.target instanceof Node && !button.current?.contains(interaction.target)
        && !hint.current?.contains(interaction.target)) setHintOpen(false);
    };
    const dismissEscape = (interaction: KeyboardEvent) => {
      if (interaction.key === "Escape") {
        interaction.preventDefault();
        interaction.stopPropagation();
        setHintOpen(false);
      }
    };
    document.addEventListener("pointerdown", dismissOutside);
    document.addEventListener("keydown", dismissEscape, true);
    return () => {
      document.removeEventListener("pointerdown", dismissOutside);
      document.removeEventListener("keydown", dismissEscape, true);
    };
  }, [showHint]);
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
  const Badge = status ? statusBadges[status] : null;
  const description = pending ? "Saving registration" : status ? museStatusLabel[status] : unavailable;
  return <><button ref={button}
    type="button"
    className="event-muse-action"
    data-registration-state={pending ? "saving" : status ?? (unavailable ? "unavailable" : "available")}
    disabled={pending}
    aria-disabled={unavailable ? true : undefined}
    aria-busy={pending || undefined}
    aria-label={status ? `View registration for ${event.title}` : unavailable ? `Muse unavailable for ${event.title}` : `Sign up with Muse for ${event.title}`}
    aria-describedby={description ? descriptionId : undefined}
    title={pending ? "Queueing with Muse" : status ? `${museStatusLabel[status]} · View registration` : unavailable ? undefined : "Sign up with Muse"}
    onMouseEnter={openHint}
    onMouseLeave={leaveHint}
    onFocus={openHint}
    onBlur={leaveHint}
    onKeyDown={interaction => {
      if (interaction.key !== "Enter" && interaction.key !== " ") return;
      interaction.stopPropagation();
      if (unavailable) {
        interaction.preventDefault();
        openHint();
      }
    }}
    onKeyUp={interaction => {
      if (interaction.key !== "Enter" && interaction.key !== " ") return;
      interaction.stopPropagation();
      if (unavailable) interaction.preventDefault();
    }}
    onPointerDown={interaction => interaction.stopPropagation()}
    onClick={interaction => {
      interaction.preventDefault();
      interaction.stopPropagation();
      if (unavailable) { openHint(); return; }
      if (!pending) muse.onSignup(event);
    }}
  >{pending ? <LoaderCircle className="spin" aria-hidden="true" /> : <MuseIcon />}
    {!pending && Badge ? <span className="event-muse-action__badge" aria-hidden="true">
      <Badge className={status === "in_progress" ? "spin" : undefined} />
    </span> : null}
    {description && !showHint ? <span id={descriptionId} className="sr-only">{description}</span> : null}
  </button>
    {showHint ? createPortal(<div ref={hint} id={descriptionId} role="tooltip"
      className="event-muse-hint"
      style={{ left: hintPosition?.left ?? 0, top: hintPosition?.top ?? 0, visibility: hintPosition ? "visible" : "hidden" }}
      onMouseEnter={cancelDismiss} onMouseLeave={leaveHint}
      onPointerDown={interaction => interaction.stopPropagation()}
      onClick={interaction => interaction.stopPropagation()}>
      <strong>Muse unavailable</strong>
      <span>{unavailable}</span>
    </div>, document.body) : null}
  </>;
}
