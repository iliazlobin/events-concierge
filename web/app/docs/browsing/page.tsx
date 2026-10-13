import type { Metadata } from "next";
import Link from "next/link";
import { CodeBlock, GuidePage, RelatedGuides } from "@/components/docs/docs-kit";
export const metadata: Metadata = { title: "Find events" };

export default function BrowsingGuide() {
  return <GuidePage
    title="Find events"
    intro={<p>Browse without signing in. Sign in to save filters or queue registrations.</p>}
    contents={[{ href: "#filters", label: "Search and filters" }, { href: "#views", label: "Views and calendar" }, { href: "#sources", label: "Sources and entities" }]}
  >
    <h2 id="filters" tabIndex={-1}>Search and filters</h2>
    <ul>
      <li>Search event names, people, organizations and venues. Filter by source, place, topic, dates, price or registration status.</li>
      <li>The initial selection is <strong>San Francisco · This week</strong>. This week includes events that have not started yet.</li>
      <li>If no events match, change the dates or place. <strong>Reset</strong> removes the filters.</li>
      <li><strong>Save filters</strong> stores the selection in your account. In <Link href="/settings/saved-filters">Saved filters</Link>, <strong>Open</strong> reapplies a selection, <strong>Rename</strong> changes its name and <strong>Delete</strong> removes it after confirmation.</li>
    </ul>
    <CodeBlock label="Example: free San Francisco events">{"/?view=events&when=all&city=sanfrancisco&price=free"}</CodeBlock>
    <p><Link href="/?view=events&when=all&city=sanfrancisco&price=free">Open this search</Link>. The URL keeps the filters, so you can bookmark or share it.</p>
    <h2 id="views" tabIndex={-1}>Events, map and calendar</h2>
    <ul>
      <li>Switch between <strong>Events</strong> and <strong>Map</strong> with the same filters. <strong>Calendar</strong> keeps the source, place, topic and price filters, and shows dates in its selected <strong>Week</strong>, <strong>Month</strong> or <strong>6 months</strong> range. Expand a card for event details.</li>
      <li>Map pins can show an approximate area. Events without coordinates remain available in the event list.</li>
      <li>Click the date or time on a full event card to open a Google Calendar draft. Review and save it there; this does not register you or keep the calendar entry synchronized.</li>
      <li>Follow the event link to check current availability and register on its provider’s website.</li>
    </ul>
    <p>Try San Francisco: <Link href="/?view=events&when=all&city=sanfrancisco">Events across all dates</Link> · <Link href="/?view=map&when=all&city=sanfrancisco">Map across all dates</Link> · <Link href="/?view=calendar&calendar=week&city=sanfrancisco">Calendar this week</Link>.</p>
    <h2 id="sources" tabIndex={-1}>Sources and entities</h2>
    <ul>
      <li>Click a source name on an event card to filter results to that source and its event dates. This clears the search and place selections. Use <strong>Add source</strong> to choose another listing.</li>
      <li><Link href="/?view=entities&when=all&city=">Entities</Link> connects people and organizations to events through <Link href="/docs/how-it-works#roles">recorded roles</Link>. A connection does not prove attendance or a personal relationship.</li>
      <li>A profile link comes from the source. It does not independently verify identity.</li>
    </ul>
    <RelatedGuides links={[{ href: "/docs/account#saved-filters", label: "Save and reuse filters" }, { href: "/docs/muse#start", label: "Register with Muse" }]} />
  </GuidePage>;
}
