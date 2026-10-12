import type { Metadata } from "next";
import Link from "next/link";
export const metadata: Metadata = { title: "Find events" };

export default function BrowsingGuide() {
  return <>
    <h1>Find events</h1>
    <p>Browse without signing in. Sign in to save filters or queue registrations.</p>
    <h2>Search and filters</h2>
    <ul>
      <li>Search event names, people, organizations and venues. Filter by source, place, topic, dates, price or registration status.</li>
      <li>The initial selection is <strong>San Francisco · This week</strong>. This week includes events that have not started yet.</li>
      <li>If no events match, change the dates or place. <strong>Reset</strong> removes the filters.</li>
      <li><strong>Save filters</strong> stores the selection in your account.</li>
    </ul>
    <h2>Events, map and calendar</h2>
    <ul>
      <li>Switch views with the same filters. Expand a card for event details.</li>
      <li>Map pins can show an approximate area. Events without coordinates remain available in the event list.</li>
      <li>Click an event date or time to open a Google Calendar draft. Review and save it there; this does not register you or keep the calendar entry synchronized.</li>
      <li>Follow the event link to check current availability and register on its provider’s website.</li>
    </ul>
    <h2>Sources and entities</h2>
    <ul>
      <li>Click a source name to open its catalog details and events. Totals cover the selected date range; place and topic summaries cover loaded events.</li>
      <li><strong>Entities</strong> connects people and organizations to events through recorded roles. A connection does not prove attendance or a personal relationship.</li>
      <li>A profile link comes from the source. It does not independently verify identity.</li>
    </ul>
    <p>For assisted registration, see <Link href="/docs/muse">Muse signups</Link>.</p>
  </>;
}
