"use client";

import { ArrowUpRight, ChevronDown } from "lucide-react";

import { useEffect, useState } from "react";

import { EventCard } from "@/components/event-card";
import type { AgentBlock, AgentEventCard, SelectionEntry } from "@/lib/agent-stream";
import type { EventEntityReference, EventItem } from "@/lib/types";

/**
 * Render the concierge's typed blocks as interactive components.
 *
 * The client renders only block kinds it recognises and reads only named fields. Model prose is
 * never parsed into layout, so a crafted event title cannot steer what gets drawn.
 *
 * Everything that names a thing is clickable, and every click becomes a follow-up question. A
 * chat surface has no filter rail to click into, so the cards themselves have to be the controls
 * — tapping a topic, an organizer, or a day is how you narrow without typing a sentence.
 */

export type BlockAction = { type: "ask"; text: string };

interface BlockProps {
  onAction?: (action: BlockAction) => void;
  /** Ids currently pulled into the conversation's context. */
  selected?: ReadonlySet<string>;
  onSelectionChange?: (
    entries: SelectionEntry[],
    mode: "toggle" | "range" | "replace",
  ) => void;
  /** The catalog's own affordances, so a chat card behaves like an Events-view card. */
  onSourceSelect?: (sourceKey: string) => void;
  onFacetSelect?: (value: string) => void;
  onPriceSelect?: (price: "free" | "paid" | "unknown") => void;
  onEntitySelect?: (reference: EventEntityReference) => void;
  onTopicSelect?: (topic: string) => void;
}

function ask(onAction: BlockProps["onAction"], text: string) {
  return () => onAction?.({ type: "ask", text });
}

/**
 * Every rail on screen, by selection id.
 *
 * A drag has to toggle rails it did not start on, and those live in unrelated subtrees — an event
 * card, a paragraph, an organizer. Rather than thread a callback through every branch, each rail
 * registers itself here and the drag resolves ids to entries as the pointer crosses them. It is a
 * single-pointer gesture, so one module-level record is enough.
 */
const COMPACT_PEOPLE = 4;

const railRegistry = new Map<string, { entry: SelectionEntry; commit: Commit }>();
type Commit = (entries: SelectionEntry[], mode: "toggle" | "range" | "replace") => void;
let activeDrag: { touched: Set<string> } | null = null;

function idUnder(x: number, y: number): string | null {
  const node = document.elementFromPoint(x, y)?.closest("[data-selection-id]");
  return node?.getAttribute("data-selection-id") ?? null;
}

/**
 * The selection gesture, shared by every selectable element.
 *
 * One control, one look, one keyboard contract, whether the thing beside it is an event card, an
 * organizer, or a paragraph. Selection lives on a dedicated strip rather than the element body so
 * it never competes with that element's own click — expanding a card and quoting a sentence are
 * different intentions.
 */
function PickRail({
  entry,
  selected,
  onSelectionChange,
  index,
}: {
  entry: SelectionEntry;
  index?: number;
} & Pick<BlockProps, "selected" | "onSelectionChange">) {
  const picked = selected?.has(entry.id) ?? false;

  useEffect(() => {
    if (!onSelectionChange) return;
    railRegistry.set(entry.id, { entry, commit: onSelectionChange });
    return () => {
      railRegistry.delete(entry.id);
    };
  }, [entry, onSelectionChange]);

  if (!onSelectionChange) return null;
  return (
    <button
      type="button"
      className="ec-pick__rail"
      role="checkbox"
      aria-checked={picked}
      aria-label={`${picked ? "Remove" : "Add"} ${entry.label} ${picked ? "from" : "to"} the conversation`}
      title="Add to the conversation. Drag to take several."
      data-pick-index={index}
      data-selection-id={entry.id}
      onPointerDown={(interaction) => {
        // Capture so the drag keeps receiving moves once it leaves this rail.
        interaction.currentTarget.setPointerCapture(interaction.pointerId);
        activeDrag = { touched: new Set([entry.id]) };
      }}
      onPointerMove={(interaction) => {
        if (!activeDrag) return;
        const over = idUnder(interaction.clientX, interaction.clientY);
        if (!over || activeDrag.touched.has(over)) return;
        activeDrag.touched.add(over);
        const target = railRegistry.get(over);
        // A drag ADDS across its span, so passing back over a rail cannot half-clear it.
        if (target) target.commit([target.entry], "range");
      }}
      onPointerUp={() => {
        const drag = activeDrag;
        activeDrag = null;
        // A press that never travelled is an ordinary toggle.
        if (drag && drag.touched.size === 1) onSelectionChange([entry], "toggle");
        else if (drag) onSelectionChange([entry], "range");
      }}
      onPointerCancel={() => {
        activeDrag = null;
      }}
    >
      <span aria-hidden="true" />
    </button>
  );
}

function Prose({ text, selected, onSelectionChange }: { text: string } & BlockProps) {
  // Restricted markdown by construction: paragraphs, `- ` bullets, **bold**. Nothing interprets
  // HTML or links, so listing text cannot inject markup.
  const paragraphs = text.trim().split(/\n{2,}/);
  // Each paragraph and each bullet is separately quotable: "what did you mean by this line" is a
  // far more common follow-up than one about a whole answer.
  const quotable = (raw: string, key: string) => {
    const plain = raw.replace(/\*\*/g, "").trim();
    const entry: SelectionEntry = {
      id: `text:${key}:${plain.slice(0, 48)}`,
      kind: "text",
      label: plain.slice(0, 60),
      text: plain,
    };
    return entry;
  };
  return (
    <div className="chat-prose">
      {paragraphs.map((paragraph, index) => {
        const lines = paragraph.split("\n");
        if (lines.every((line) => line.trim().startsWith("- "))) {
          return (
            <ul key={index}>
              {lines.map((line, item) => {
                const body = line.trim().slice(2);
                const entry = quotable(body, `${index}-${item}`);
                return (
                  <li
                    key={item}
                    className={`ec-quote${selected?.has(entry.id) ? " is-picked" : ""}`}
                    data-selection-anchor={entry.id}
                  >
                    <PickRail
                      entry={entry}
                      selected={selected}
                      onSelectionChange={onSelectionChange}
                    />
                    <span>{emphasise(body)}</span>
                  </li>
                );
              })}
            </ul>
          );
        }
        const entry = quotable(paragraph, String(index));
        return (
          <div
            key={index}
            className={`ec-quote${selected?.has(entry.id) ? " is-picked" : ""}`}
            data-selection-anchor={entry.id}
          >
            <PickRail
              entry={entry}
              selected={selected}
              onSelectionChange={onSelectionChange}
            />
            <p>{emphasise(paragraph)}</p>
          </div>
        );
      })}
    </div>
  );
}

function emphasise(text: string) {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, index) =>
    part.startsWith("**") && part.endsWith("**")
      ? <strong key={index}>{part.slice(2, -2)}</strong>
      : <span key={index}>{part}</span>,
  );
}

/** "Fri Aug 28, 7:00 PM PDT" -> a compact date badge plus the time, matching the catalog cards. */
function splitWhen(when: string): { day: string; date: string; time: string } {
  const match = /^(\w{3}) (\w{3}) (\d{1,2}), (.+)$/.exec(when ?? "");
  if (!match) return { day: "", date: "", time: when ?? "" };
  return { day: match[1], date: `${match[2].toUpperCase()} ${match[3]}`, time: match[4] };
}

/**
 * Event blocks render through the catalog's own EventList.
 *
 * The chat used to draw a lookalike card, which meant two components describing the same record
 * and drifting apart on every change. The agent now ships the full ``EventItem`` on the render
 * channel, so the chat can use the component the Events, Map and Calendar views already use and
 * inherit its expansion, entity links, topic chips and source affordances for free.
 */
/**
 * Event blocks, each card fronted by a selection rail.
 *
 * Cards render through the catalog's own EventCard rather than a lookalike: the agent ships the
 * full ``EventItem`` on the render channel, so chat inherits the same expansion, calendar and
 * maps links, entity chips and topic tones the Events view has, and the two cannot drift.
 *
 * The rail is a separate control on purpose. The card body is already a disclosure toggle, so
 * overloading it with selection would make every "read more" a context change. A dedicated strip
 * down the left edge keeps the two gestures apart and gives a drag something to travel along.
 */
function AgentEventList({
  items,
  onAction,
  selected,
  onSelectionChange,
  ...handlers
}: { items: AgentEventCard[] } & BlockProps) {
  const [expandedId, setExpandedId] = useState<string | null>(null);

  const events = items.filter(
    (item): item is AgentEventCard & EventItem =>
      typeof (item as { canonical_event_id?: unknown }).canonical_event_id === "string",
  );
  if (!events.length) return null;

  const entryFor = (event: AgentEventCard): SelectionEntry => ({
    id: `event:${event.ref}`,
    kind: "event",
    label: String(event.title ?? ""),
    ref: String(event.ref ?? ""),
  });

  return (
    <div className="ec-chat-events">
      {events.map((event, index) => {
        const entry = entryFor(event);
        const picked = Boolean(event.ref && selected?.has(entry.id));
        return (
          <div
            className={`ec-pick${picked ? " is-picked" : ""}`}
            key={event.canonical_event_id}
            data-selection-anchor={entry.id}
          >
            <PickRail
              entry={entry}
              index={index}
              selected={selected}
              onSelectionChange={onSelectionChange}
            />
            <div className="ec-pick__card" data-pick-index={index}>
              <EventCard
                event={event as unknown as EventItem}
                compact
                expanded={expandedId === event.canonical_event_id}
                onToggle={() =>
                  setExpandedId((current) =>
                    current === event.canonical_event_id ? null : event.canonical_event_id,
                  )
                }
                onSourceSelect={handlers.onSourceSelect}
                onFacetSelect={handlers.onFacetSelect}
                onPriceSelect={
                  handlers.onPriceSelect
                  ?? ((price) =>
                    onAction?.({
                      type: "ask",
                      text: price === "free" ? "Show me only free ones" : "Show me only paid ones",
                    }))
                }
                onEntitySelect={
                  handlers.onEntitySelect
                  ?? ((reference) =>
                    onAction?.({ type: "ask", text: `What else does ${reference.name} run?` }))
                }
                onTopicSelect={
                  handlers.onTopicSelect
                  ?? ((topic) => onAction?.({ type: "ask", text: `Show me more ${topic} events` }))
                }
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function EntityCard({
  item,
  onAction,
  selected,
  onSelectionChange,
}: { item: AgentEventCard } & BlockProps) {
  const entry: SelectionEntry = {
    id: `entity:${item.ref}`,
    kind: "entity",
    label: String(item.name ?? ""),
    ref: String(item.ref ?? ""),
  };
  const picked = Boolean(item.ref && selected?.has(entry.id));
  return (
    <article
      className={`ec-card ec-card--entity${picked ? " is-picked" : ""}`}
      data-selection-anchor={entry.id}
    >
      {item.ref && onSelectionChange ? (
        <PickRail entry={entry} selected={selected} onSelectionChange={onSelectionChange} />
      ) : null}
      <div className="ec-card__body">
        <div className="ec-card__main">
          <p className="ec-card__eyebrow">
            {item.kind ? <span>{item.kind}</span> : null}
            {typeof item.events_in_catalog === "number" ? (
              <span className="ec-card__price">{item.events_in_catalog} in catalog</span>
            ) : null}
          </p>
          <h4 className="ec-card__title">{item.name}</h4>
          {item.summary ? <p className="ec-card__meta"><span>{item.summary}</span></p> : null}
          <p className="ec-card__chips">
            {(item.roles ?? []).map((role) => <span className="ec-chip is-static" key={role}>{role}</span>)}
          </p>
          {item.upcoming_events?.length ? (
            <ul className="ec-card__upcoming">
              {item.upcoming_events.slice(0, 4).map((event, index) => (
                <li key={index}>
                  <button type="button" onClick={ask(onAction, `Tell me about ${event.title}`)}>
                    {event.title}
                  </button>
                  <span>{event.when}</span>
                </li>
              ))}
            </ul>
          ) : null}
          <div className="ec-card__actions">
            <button type="button" onClick={ask(onAction, `What else does ${item.name} run?`)}>
              More from {item.name}
            </button>
          </div>
        </div>
      </div>
    </article>
  );
}

/**
 * Who is behind a set of events, as links rather than sentences.
 *
 * A name in prose is a dead end; the same name as a chip reaches everything else that person or
 * organization runs. Grouped under its event so the connection stays visible -- a flat list of
 * twelve names loses which event each belongs to, which is most of the answer.
 */
/**
 * Who is behind a set of events — a disclosure, not a wall.
 *
 * A roster is reference material: worth having, rarely the answer. Six people across two events
 * pushed the actual reply below the fold, so it opens collapsed once it is big enough to cost
 * more than it gives, and stays open when it is small enough to just read.
 */
function People({
  groups,
  label,
  onAction,
  onEntitySelect,
}: {
  groups: Extract<AgentBlock, { kind: "people" }>["groups"];
  label?: string;
} & BlockProps) {
  const total = groups.reduce((count, group) => count + group.members.length, 0);
  const [open, setOpen] = useState(total <= COMPACT_PEOPLE);
  if (!groups.length) return null;

  return (
    <section className={`ec-people${open ? " is-open" : ""}`}>
      <button
        type="button"
        className="ec-people__toggle"
        aria-expanded={open}
        onClick={() => setOpen((current) => !current)}
      >
        <ChevronDown aria-hidden="true" />
        <span className="ec-people__label">{label || "Who is behind these"}</span>
        <span className="ec-people__count">
          {total} {total === 1 ? "name" : "names"}
          {groups.length > 1 ? ` · ${groups.length} events` : ""}
        </span>
      </button>

      {open ? (
        <div className="ec-people__body">
          {groups.map((group) => (
            <div className="ec-people__group" key={group.canonical_event_id}>
              {groups.length > 1 ? (
                <p className="ec-people__event">{group.event_title}</p>
              ) : null}
              <div className="ec-people__members">
                {group.members.map((member) => (
                  <span className="ec-person" key={`${member.role}:${member.name}`}>
                    <button
                      type="button"
                      className="ec-person__name"
                      title={`Explore ${member.name}`}
                      onClick={() =>
                        onEntitySelect
                          ? onEntitySelect({
                              canonicalEventId: group.canonical_event_id,
                              role: member.role as EventEntityReference["role"],
                              name: member.name,
                            })
                          : onAction?.({
                              type: "ask",
                              text: `What else does ${member.name} run?`,
                            })
                      }
                    >
                      <span className="ec-person__role">{member.role.slice(0, 4)}</span>
                      <span className="ec-person__label">{member.name}</span>
                    </button>
                    {member.profile_url ? (
                      <a
                        className="ec-person__link"
                        href={member.profile_url}
                        target="_blank"
                        rel="noreferrer noopener"
                        aria-label={`Open the public profile for ${member.name}`}
                      >
                        <ArrowUpRight aria-hidden="true" />
                      </a>
                    ) : null}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function Tally({
  rows,
  label,
  total,
  onAction,
}: { rows: { label: string; value: number }[]; label?: string; total?: number } & BlockProps) {
  const peak = Math.max(...rows.map((row) => row.value), 1);
  return (
    <div className="ec-tally">
      {label ? (
        <p className="ec-tally__label">
          {label}
          {total ? <span>{total} total</span> : null}
        </p>
      ) : null}
      {rows.map((row) => {
        const when = new Date(`${row.label}T12:00:00`);
        const readable = Number.isNaN(when.valueOf())
          ? row.label
          : when.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
        return (
          <button
            className="ec-tally__row"
            key={row.label}
            type="button"
            onClick={ask(onAction, `What's on ${readable}?`)}
          >
            <span className="ec-tally__key">{readable}</span>
            <span className="ec-tally__track">
              <span className="ec-tally__bar" style={{ width: `${(row.value / peak) * 100}%` }} />
            </span>
            <span className="ec-tally__value">{row.value}</span>
          </button>
        );
      })}
    </div>
  );
}

export function ChatBlocks({
  blocks,
  onAction,
  selected,
  onSelectionChange,
  ...handlers
}: { blocks: AgentBlock[] } & BlockProps) {
  return (
    <>
      {blocks.map((block, index) => {
        switch (block.kind) {
          case "prose":
            return (
              <Prose
                key={index}
                text={block.text}
                selected={selected}
                onSelectionChange={onSelectionChange}
              />
            );
          case "events":
            return (
              <AgentEventList
                key={index}
                items={block.items}
                onAction={onAction}
                selected={selected}
                onSelectionChange={onSelectionChange}
                {...handlers}
              />
            );
          case "entities":
            return (
              <div className="ec-cards" key={index}>
                {block.items.map((item, position) => (
                  <EntityCard key={item.ref ?? position} item={item} onAction={onAction} />
                ))}
              </div>
            );
          case "people":
            return (
              <People
                key={index}
                groups={block.groups}
                label={block.label}
                onAction={onAction}
                onEntitySelect={handlers.onEntitySelect}
              />
            );
          case "tally":
            return (
              <Tally
                key={index}
                rows={block.rows}
                label={block.label}
                total={block.total}
                onAction={onAction}
              />
            );
          case "notice":
            return (
              <p className={`ec-notice ec-notice--${block.tone ?? "coverage"}`} key={index}>
                {block.text}
              </p>
            );
          default:
            // An unrecognised kind renders nothing rather than guessing at a shape.
            return null;
        }
      })}
    </>
  );
}
