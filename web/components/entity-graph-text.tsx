"use client";

import { ArrowUpRight } from "lucide-react";

import { formatEventDate, formatEventTime } from "@/lib/date";
import type {
  EntityGraphTextAppearance,
  EntityGraphTextModel,
} from "@/lib/entity-graph";
import { eventTopicLabel } from "@/lib/event-topics";
import { formatCity } from "@/lib/presentation";

/**
 * The complete text equivalent of the scene.
 *
 * Not a stub and not a summary: every node the canvas draws appears here, with its relationship to
 * the ego spelled out in words.  It renders `sceneToTextModel()`'s output — the same derived model
 * the canvas is laid out from — so the two renderings cannot disagree about what the frame
 * contains.  This is simultaneously the screen-reader truth, the no-canvas fallback, the print
 * form, and the default below 700 px.
 */

function roleLabel(role: string): string {
  return role.replace(/^./, (value) => value.toUpperCase());
}

function whereLine(item: EntityGraphTextAppearance): string {
  const when = item.start_at ? formatEventTime(item.start_at, null) : "";
  const where = [item.venue_name, formatCity(item.city)].filter(Boolean).join(" · ");
  return [when, where].filter(Boolean).join(" — ");
}

function AppearanceList({
  items,
  onSelectNode,
}: {
  items: EntityGraphTextAppearance[];
  onSelectNode: (nodeId: string) => void;
}) {
  return (
    <ol className="entity-graph-text__events">
      {items.map((item) => {
        const date = item.start_at ? formatEventDate(item.start_at) : null;
        return (
          <li key={item.node_id}>
            <button
              type="button"
              className="entity-graph-text__event"
              onClick={() => onSelectNode(item.node_id)}
            >
              {date ? (
                <time dateTime={item.start_at ?? undefined}>
                  <span>{date.month}</span><strong>{date.day}</strong><small>{date.weekday}</small>
                </time>
              ) : null}
              <span>
                <strong>{item.title}</strong>
                <span>{whereLine(item)}</span>
                {/*
                  Provenance in words: the role a source asserted, which source asserted it, and
                  when it was last seen.  This is the path the canvas deliberately does not offer on
                  an edge, because an edge tooltip is unreachable by keyboard.
                */}
                <small>
                  {item.roles.length ? item.roles.map(roleLabel).join(" · ") : "Named"}
                  {item.source_labels.length
                    ? ` · asserted by ${item.source_labels.join(", ")}`
                    : ""}
                  {item.observed_at
                    ? ` · seen ${new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", year: "numeric" }).format(new Date(item.observed_at))}`
                    : ""}
                  {` · ${item.entity_count} ${item.entity_count === 1 ? "entity" : "entities"} named`}
                </small>
              </span>
            </button>
            {item.registration_url ? (
              <a href={item.registration_url} target="_blank" rel="noopener noreferrer">
                View event <ArrowUpRight aria-hidden="true" />
              </a>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

export interface EntityGraphTextProps {
  model: EntityGraphTextModel;
  /** Inspect a node without changing the ego. */
  onSelectNode: (nodeId: string) => void;
  /** Make an entity the new ego. */
  onFocusEntity: (entityId: string) => void;
}

export function EntityGraphText({ model, onSelectNode, onFocusEntity }: EntityGraphTextProps) {
  const eventTotal = model.upcoming.length + model.past.length;

  return (
    <section className="entity-graph-text">
      <header>
        <p>TEXT EQUIVALENT</p>
        <h2>{model.ego?.display_name ?? "This entity"} in words</h2>
        <small>
          {eventTotal} {eventTotal === 1 ? "event" : "events"}, {model.peers.length} connected
          {model.topics.length ? `, ${model.topics.length} topics` : ""} — the same scene the graph
          draws, read out in full.
        </small>
      </header>

      {model.ego ? (
        <dl className="entity-graph-text__identity">
          <div>
            <dt>Kind</dt>
            <dd>{model.ego.kind ?? "unknown"}</dd>
          </div>
          <div>
            <dt>Identity</dt>
            <dd>
              {model.ego.identity_status === "profile_verified"
                ? "A direct profile URL is on file — that is a link a source gave us, not a verification of the person."
                : "Scoped to the source that named it. No direct profile URL is on file."}
            </dd>
          </div>
          <div>
            <dt>Connections</dt>
            <dd>
              {model.ego.degree} {model.ego.degree === 1 ? "event" : "events"} in the catalog
              {" · "}
              {model.peers.length} {model.peers.length === 1 ? "person or org" : "people and orgs"}
              {" reached through them"}
            </dd>
          </div>
          {model.ego.roles.length ? (
            <div>
              <dt>Named as</dt>
              <dd>{model.ego.roles.map(roleLabel).join(" · ")}</dd>
            </div>
          ) : null}
          {model.ego.profile_url ? (
            <div>
              <dt>Profile URL</dt>
              <dd>
                <a href={model.ego.profile_url} target="_blank" rel="noopener noreferrer">
                  {model.ego.profile_url} <ArrowUpRight aria-hidden="true" />
                </a>
              </dd>
            </div>
          ) : null}
        </dl>
      ) : null}

      <section>
        <h3>Upcoming appearances ({model.upcoming.length})</h3>
        {model.upcoming.length ? (
          <AppearanceList items={model.upcoming} onSelectNode={onSelectNode} />
        ) : (
          <p className="entity-empty-copy">No upcoming event mentions in this frame.</p>
        )}
      </section>

      <section>
        <h3>Past appearances ({model.past.length})</h3>
        {model.past.length ? (
          <AppearanceList items={model.past} onSelectNode={onSelectNode} />
        ) : (
          <p className="entity-empty-copy">No past event mentions in this frame.</p>
        )}
      </section>

      <section>
        <h3>Reached through those events ({model.peers.length})</h3>
        {model.peers.length ? (
          <ul className="entity-graph-text__peers">
            {model.peers.map((peer) => (
              <li key={peer.node_id}>
                <button
                  type="button"
                  onClick={() => (peer.entity_id
                    ? onFocusEntity(peer.entity_id)
                    : onSelectNode(peer.node_id))}
                >
                  <strong>{peer.display_name}</strong>
                  <span>
                    {peer.kind ?? "unknown"}
                    {" · "}
                    {peer.shared_event_count} shared
                    {" · "}
                    {peer.degree} {peer.degree === 1 ? "event" : "events"} overall
                    {" · "}
                    {peer.identity_status === "profile_verified"
                      ? "direct profile on file"
                      : "source-scoped"}
                  </span>
                </button>
                {/* The evidence for the tie, named: peers are reached THROUGH these events. */}
                {peer.shared_event_titles.length ? (
                  <small>Through: {peer.shared_event_titles.join("; ")}</small>
                ) : null}
              </li>
            ))}
          </ul>
        ) : (
          <p className="entity-empty-copy">
            No one else is named on these events. The appearances above are still the full record.
          </p>
        )}
      </section>

      {model.topics.length ? (
        <section>
          <h3>Usual topics ({model.topics.length})</h3>
          <ul className="entity-graph-text__topics">
            {model.topics.map((topic) => (
              <li key={topic.node_id}>
                <button type="button" onClick={() => onSelectNode(topic.node_id)}>
                  {eventTopicLabel(topic.label)}
                  <small>{topic.event_count} of the {eventTotal} shown events</small>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {model.same_name_candidates.length ? (
        <section>
          <h3>Other rows with this exact name ({model.same_name_candidates.length})</h3>
          <p className="entity-empty-copy">
            Same name — not merged. A matching display name is not evidence that these are the same
            person or organization.
          </p>
          <ul className="entity-graph-text__same-name">
            {model.same_name_candidates.map((candidate) => (
              <li key={candidate.entity_id}>
                <button type="button" onClick={() => onFocusEntity(candidate.entity_id)}>
                  <strong>{candidate.display_name}</strong>
                  <span>
                    {candidate.kind}
                    {" · "}
                    {candidate.identity_status === "profile_verified"
                      ? "direct profile on file"
                      : "source-scoped"}
                    {" · "}
                    {candidate.event_count} {candidate.event_count === 1 ? "event" : "events"}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {model.truncated.events || model.truncated.peers || model.truncated.edges ? (
        <p className="entity-graph-text__truncation">
          This frame is capped: showing {model.counts.events} of {model.counts.events_total} events
          {" and "}{model.counts.peers} of {model.counts.peers_total} connected entities.
        </p>
      ) : null}
    </section>
  );
}
