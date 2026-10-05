"use client";

import {
  ArrowUpRight,
  Building2,
  ChevronDown,
  Clock3,
  MapPin,
  Mic2,
  Tags,
  UsersRound,
} from "lucide-react";
import { useId, type ReactNode } from "react";

import { discoveryLabels } from "@/lib/event-discovery";
import { formatEventDate, formatEventTime } from "@/lib/date";
import {
  eventFormatLabel,
  eventRegistrationCtaLabel,
  eventRegistrationLabel,
  safeEntityProfile,
} from "@/lib/event-facets";
import {
  eventLocationLabel,
  eventPageUrl,
  googleCalendarUrl,
  googleMapsUrl,
} from "@/lib/event-links";
import { formatEventPrice } from "@/lib/event-price";
import { eventTopicLabel, eventTopicTone } from "@/lib/event-topics";
import type {
  EventEntityKind,
  EventEntityProfile,
  EventEntityReference,
  EventEntityRole,
  EventItem,
} from "@/lib/types";

interface EventCardProps {
  event: EventItem;
  expanded: boolean;
  onToggle?: () => void;
  onSourceSelect?: (sourceKey: string) => void;
  onFacetSelect?: (value: string) => void;
  /** Optional: makes the eyebrow price a filter control. Omitted, the price stays static text. */
  onPriceSelect?: (price: "free" | "paid" | "unknown") => void;
  onEntitySelect?: (reference: EventEntityReference) => void;
  onTopicSelect?: (topic: string) => void;
  compact?: boolean;
  /** Optional graph navigation alongside the complete shared event facts. */
  entityDetails?: ReactNode;
}

function providerName(event: EventItem): string {
  return (
    event.calendar_labels?.[0]
    ?? event.sources[0]?.label
    ?? event.providers?.[0]
    ?? event.sources[0]?.provider
    ?? event.sources[0]?.source
    ?? "Event"
  );
}

function providerFilter(event: EventItem): { label: string; sourceKey: string } | null {
  const label = providerName(event);
  const matchingSource = event.sources.find((source) => (
    source.source_key
    && source.label?.trim() === label.trim()
  ));
  const sourceKey = matchingSource?.source_key
    ?? event.source_keys?.find((value) => value.trim())
    ?? event.sources.find((source) => source.source_key)?.source_key;
  return sourceKey ? { label, sourceKey } : null;
}

function descriptionPreview(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

interface EntityLinksProps {
  names: string[];
  defaultKind: EventEntityKind;
  profiles: EventEntityProfile[];
  role: EventEntityRole;
  roleLabel: string;
  canonicalEventId: string;
  onEntitySelect?: (reference: EventEntityReference) => void;
}

function normalizedEntityName(value: string): string {
  return value.replace(/\s+/g, " ").trim().toLocaleLowerCase();
}

function EntityLinks({
  names,
  defaultKind,
  profiles,
  role,
  roleLabel,
  canonicalEventId,
  onEntitySelect,
}: EntityLinksProps) {
  const visible = names.slice(0, 6);
  const remaining = names.length - visible.length;
  return (
    <ul className="event-card__entities">
      {visible.map((name) => {
        const identity = normalizedEntityName(name);
        const profile = profiles.find((candidate) => (
          candidate.role === role
          && normalizedEntityName(candidate.name) === identity
        ));
        const safeProfile = profile
          ? safeEntityProfile(profile.profile_url, defaultKind)
          : null;
        const profileLabel = safeProfile?.network === "linkedin"
          ? `Open ${name} on LinkedIn`
          : defaultKind === "organization"
            ? `Open ${name} website`
            : `Open profile for ${name}`;
        return (
          <li key={name}>
            {onEntitySelect ? (
              <button
                type="button"
                onClick={(interaction) => {
                  interaction.preventDefault();
                  interaction.stopPropagation();
                  onEntitySelect({ canonicalEventId, role, name });
                }}
                aria-label={`Explore ${roleLabel.toLowerCase()} ${name}`}
                title={`Explore ${name}`}
              >
                {name}
              </button>
            ) : (
              <span>{name}</span>
            )}
            {safeProfile ? (
              <a
                href={safeProfile.url}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={profileLabel}
                title={profileLabel}
                onPointerDown={(interaction) => interaction.stopPropagation()}
                onClick={(interaction) => interaction.stopPropagation()}
              >
                {safeProfile.network === "linkedin" ? (
                  <span aria-hidden="true">in</span>
                ) : (
                  <ArrowUpRight aria-hidden="true" />
                )}
              </a>
            ) : null}
          </li>
        );
      })}
      {remaining > 0 ? <li className="event-card__entity-more">+{remaining} more</li> : null}
    </ul>
  );
}

export function EventCard({
  event,
  expanded,
  onToggle,
  onSourceSelect,
  onFacetSelect,
  onPriceSelect,
  onEntitySelect,
  onTopicSelect,
  compact = false,
  entityDetails,
}: EventCardProps) {
  const date = formatEventDate(event.start_at);
  const url = eventPageUrl(event);
  const calendarUrl = googleCalendarUrl(event);
  const mapsUrl = googleMapsUrl(event);
  const price = formatEventPrice(event);
  const registration = eventRegistrationLabel(event);
  const format = eventFormatLabel(event);
  const actionLabel = eventRegistrationCtaLabel(event);
  const topics = [...new Set(event.topics ?? [])];
  const description = descriptionPreview(event.description);
  const hosts = event.host_names ?? [];
  const speakers = event.speaker_names ?? [];
  const partners = event.partner_names ?? [];
  const profiles = event.entity_profiles ?? [];
  const organizerKey = event.organizer_name?.toLocaleLowerCase();
  const organizer = event.organizer_name
    && !hosts.some((name) => name.toLocaleLowerCase() === organizerKey)
    ? event.organizer_name
    : null;
  const attendance = event.attendance_count !== null
    && event.attendance_count !== undefined
    && event.attendance_count > 0
    ? `${event.attendance_count.toLocaleString()} going`
    : null;
  const decisionFacts = [
    registration,
    price,
    attendance,
    format,
  ].filter((value): value is string => Boolean(value));
  const summaryFacts = [...discoveryLabels(event), registration, attendance, format]
    .filter((value): value is string => Boolean(value));
  const instanceId = useId().replaceAll(":", "");
  const regionId = `event-details-${instanceId}`;
  const titleId = `event-title-${instanceId}`;
  const time = formatEventTime(event.start_at, event.end_at);
  const location = eventLocationLabel(event);
  const provider = providerFilter(event);

  return (
    <article
      className={`event-card${expanded ? " is-open" : ""}${compact ? " is-compact" : ""}${!onToggle ? " is-static" : ""}`}
    >
      <div className="event-card__summary">
        {onToggle ? <button
          className="event-card__summary-trigger"
          type="button"
          aria-expanded={expanded}
          aria-controls={regionId}
          aria-label={`${expanded ? "Hide" : "Show"} details for ${event.title}`}
          onClick={onToggle}
        >
          <span className="event-card__disclosure-cue" aria-hidden="true">
            <ChevronDown />
          </span>
        </button> : null}

        <time
          className="event-card__date"
          dateTime={event.start_at}
          aria-hidden={onToggle ? true : undefined}
        >
          <span>{date.month}</span>
          <strong>{date.day}</strong>
          <small>{date.weekday}</small>
        </time>

        <div className="event-card__body">
          <span className="event-card__eyebrow">
            {provider && onSourceSelect ? (
              <button
                className="event-card__source-filter"
                type="button"
                aria-label={`Filter events by source ${provider.label}`}
                title={`Show events from ${provider.label}`}
                onPointerDown={(interaction) => {
                  interaction.preventDefault();
                  interaction.stopPropagation();
                }}
                onClick={(interaction) => {
                  interaction.preventDefault();
                  interaction.stopPropagation();
                  onSourceSelect(provider.sourceKey);
                }}
              >
                {provider.label}
              </button>
            ) : providerName(event)}
            {price ? (
              <>
                <span aria-hidden="true">/</span>
                {onPriceSelect ? (
                  <button
                    className="event-card__source-filter"
                    type="button"
                    aria-label={`Filter events by price ${price}`}
                    title={`Show ${price.toLowerCase()} events`}
                    onPointerDown={(interaction) => {
                      interaction.preventDefault();
                      interaction.stopPropagation();
                    }}
                    onClick={(interaction) => {
                      interaction.preventDefault();
                      interaction.stopPropagation();
                      onPriceSelect(event.price_status);
                    }}
                  >
                    {price}
                  </button>
                ) : price}
              </>
            ) : null}
            {/* Only shown when the source actually published a posture: the helper returns null
                for "unknown", and a card that always claimed "open" would be worse than silent. */}
            {registration ? (
              <>
                <span aria-hidden="true">/</span>
                <span
                  className={`event-card__registration event-card__registration--${event.registration_status}`}
                >
                  {registration}
                </span>
              </>
            ) : null}
          </span>
          <h2 className="event-card__title" id={titleId}>{event.title}</h2>
          <div className="event-card__meta">
            {calendarUrl ? (
              <a
                className="event-card__meta-link"
                href={calendarUrl}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={`Add ${event.title} on ${date.weekday}, ${date.month} ${date.day} at ${time} to Google Calendar`}
                title="Add to Google Calendar"
              >
                <Clock3 aria-hidden="true" />
                {time}
              </a>
            ) : (
              <span>
                <Clock3 aria-hidden="true" />
                {time}
              </span>
            )}
            {mapsUrl ? (
              <a
                className="event-card__meta-link"
                href={mapsUrl}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={location === "Mapped location"
                  ? `Open map for ${event.title} in Google Maps`
                  : `Open ${location} in Google Maps`}
              >
                <MapPin aria-hidden="true" />
                {location}
              </a>
            ) : (
              <span>
                <MapPin aria-hidden="true" />
                {location}
              </span>
            )}
          </div>
          {summaryFacts.length ? (
            <ul className="event-card__summary-facts" aria-label="Event highlights">
              {summaryFacts.map((fact) => <li key={fact}>{fact}</li>)}
            </ul>
          ) : null}
        </div>

      </div>

      <div
        className="event-card__expansion"
        id={regionId}
        role="region"
        aria-labelledby={titleId}
        aria-hidden={!expanded}
        inert={!expanded}
      >
        <div className="event-card__expansion-inner">
          {decisionFacts.length ? (
            <ul className="event-card__decision-strip" aria-label="Event availability">
              {decisionFacts.map((fact) => (
                <li
                  key={fact}
                  className={fact === registration ? "is-registration" : undefined}
                >
                  {fact}
                </li>
              ))}
            </ul>
          ) : null}

          {event.additional_dates?.length ? (
            <details>
              <summary>{event.additional_dates.length} more dates in these results</summary>
              <ul>
                {event.additional_dates.map((occurrence) => {
                  const href = eventPageUrl({ ...event, registration_urls: occurrence.registration_urls, sources: [] });
                  const label = `${new Date(occurrence.start_at).toLocaleDateString()} · ${formatEventTime(occurrence.start_at, occurrence.end_at)}`;
                  return <li key={occurrence.canonical_event_id}>
                    {href ? <a href={href} target="_blank" rel="noopener noreferrer">{label}</a> : label}
                  </li>;
                })}
              </ul>
            </details>
          ) : null}

          {organizer || hosts.length || speakers.length || partners.length || topics.length ? (
            <dl className="event-card__facts">
              {hosts.length ? (
                <div>
                  <dt><UsersRound aria-hidden="true" />Hosts</dt>
                  <dd>
                    <EntityLinks
                      names={hosts}
                      defaultKind="person"
                      profiles={profiles}
                      role="host"
                      roleLabel="Host"
                      canonicalEventId={event.canonical_event_id}
                      onEntitySelect={onEntitySelect}
                    />
                  </dd>
                </div>
              ) : null}
              {organizer ? (
                <div>
                  <dt><Building2 aria-hidden="true" />Organizer</dt>
                  <dd>
                    <EntityLinks
                      names={[organizer]}
                      defaultKind="organization"
                      profiles={profiles}
                      role="organizer"
                      roleLabel="Organizer"
                      canonicalEventId={event.canonical_event_id}
                      onEntitySelect={onEntitySelect}
                    />
                  </dd>
                </div>
              ) : null}
              {speakers.length ? (
                <div>
                  <dt><Mic2 aria-hidden="true" />Speakers</dt>
                  <dd>
                    <EntityLinks
                      names={speakers}
                      defaultKind="person"
                      profiles={profiles}
                      role="speaker"
                      roleLabel="Speaker"
                      canonicalEventId={event.canonical_event_id}
                      onEntitySelect={onEntitySelect}
                    />
                  </dd>
                </div>
              ) : null}
              {partners.length ? (
                <div>
                  <dt><Building2 aria-hidden="true" />Organizations</dt>
                  <dd>
                    <EntityLinks
                      names={partners}
                      defaultKind="organization"
                      profiles={profiles}
                      role="partner"
                      roleLabel="Organization"
                      canonicalEventId={event.canonical_event_id}
                      onEntitySelect={onEntitySelect}
                    />
                  </dd>
                </div>
              ) : null}
              {topics.length ? (
                <div>
                  <dt><Tags aria-hidden="true" />Topics</dt>
                  <dd>
                    <ul className="event-card__signals">
                      {topics.map((topic) => (
                        <li data-topic-tone={eventTopicTone(topic)} key={topic}>
                          {onTopicSelect ? (
                            <button
                              type="button"
                              onPointerDown={(interaction) => {
                                interaction.preventDefault();
                                interaction.stopPropagation();
                              }}
                              onClick={(interaction) => {
                                interaction.preventDefault();
                                interaction.stopPropagation();
                                onTopicSelect(topic);
                              }}
                              aria-label={`Add ${eventTopicLabel(topic).toLowerCase()} topic filter`}
                            >
                              {eventTopicLabel(topic)}
                            </button>
                          ) : (
                            <span>{eventTopicLabel(topic)}</span>
                          )}
                        </li>
                      ))}
                    </ul>
                  </dd>
                </div>
              ) : null}
            </dl>
          ) : null}
          <p className="event-card__description">
            {description || "The organizer has not added a description yet."}
          </p>
          {entityDetails}

          <div className="event-card__actions">
            {url ? (
              <a
                className="event-action"
                href={url}
                target="_blank"
                rel="noopener noreferrer"
              >
                {actionLabel}
                <ArrowUpRight aria-hidden="true" />
              </a>
            ) : null}
          </div>
        </div>
      </div>
    </article>
  );
}
