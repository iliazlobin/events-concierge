import type { Metadata } from "next";
import Link from "next/link";
import { MuseInstructions } from "@/components/docs/muse-instructions";
import { MUSE_URL } from "@/lib/muse";
import { CodeBlock, FlowDiagram, GuidePage, RelatedGuides } from "@/components/docs/docs-kit";
export const metadata: Metadata = { title: "Agentic registrations (Muse)" };

export default function MuseGuide() {
  return <GuidePage
    title="Agentic registrations (Muse)"
    intro={<p>Queue free Luma, Meetup or Partiful events before they end. Muse completes registration in its own browser.</p>}
    contents={[{ href: "#connect", label: "Connect" }, { href: "#account", label: "Registration account" }, { href: "#start", label: "Queue and start" }, { href: "#progress", label: "Progress" }, { href: "#recovery", label: "Limits and recovery" }]}
  >
    <h2 id="connect" tabIndex={-1}>Connect Muse</h2>
    <ol>
      <li>Sign in and open <Link href="/settings/registrations">Registration settings</Link>.</li>
      <li>Copy the <strong>Connector URL</strong> directly from settings. The app must use a public HTTPS address; local preview addresses are unavailable to Muse.</li>
      <li>Configure a Muse connector with that URL and <strong>Bearer token</strong> authentication.</li>
      <li>Choose <strong>Create key</strong> in Events Concierge. Paste the key into the connector’s credential field.</li>
    </ol>
    <p>The key is shown once and expires after 30 days. An active key confirms that Events Concierge issued access; it does not confirm that Muse connected.</p>
    <p>Bearer authentication sends the connection key in the request’s authorization header:</p>
    <CodeBlock label="Connector request format — placeholder key">{"GET /v1/muse/connector/batches?limit=100\nAuthorization: Bearer YOUR_CONNECTION_KEY"}</CodeBlock>
    <p>The <a href="/v1/muse/openapi.json">connector schema</a> describes the operations Muse can use when signups are enabled. Your event-provider logins stay in Muse.</p>
    <p>Looking for catalog search through MCP? See <Link href="/docs/mcp">MCP discovery</Link>.</p>
    <h2 id="account" tabIndex={-1}>Registration account</h2>
    <p>Your Events Concierge sign-in owns the queue. The account Muse uses on the event website can be different.</p>
    <ol>
      <li>Open <Link href="/settings/registrations">Registration settings</Link> and select Muse.</li>
      <li>Set the default registration email. Use <strong>Per site</strong> to override it for Luma, Meetup or Partiful.</li>
      <li>Save. Muse uses the selected email in signup and contact email fields. On Luma and Meetup, it also checks the signed-in account.</li>
    </ol>
    <p>Leave the default empty to use the current browser account. An empty site override inherits the default. This preference stores no credentials, changes no login and grants no Google or provider access.</p>
    <p>The selected email also applies to company/work email fields when accepted. If a form rejects it or requires a business-domain address, Muse asks you instead of substituting another address. With no selected email, Muse asks for unanswered email fields. Your app sign-in is never a fallback.</p>
    <p><a href="https://help.partiful.com/en-us/articles/15525505-how-do-i-rsvp-to-an-event-on-partiful" target="_blank" rel="noopener noreferrer">Partiful verifies RSVPs by phone</a>. You complete phone/SMS login in Muse’s browser; Muse does not enter phone numbers, request codes or enter verification codes. The email preference applies to email form fields. Muse checks the account email when visible or asks you to confirm the active phone account before continuing.</p>
    <CodeBlock label="Different app and provider accounts">{"Events Concierge: owner@example.test\nMuse default:      registrations@example.test\nLuma override:    events@example.test"}</CodeBlock>
    <p>Changes apply to queued tasks. Claimed attempts keep their account selection, including when resumed. If the browser account differs from the selected email, Muse stops for your instruction; it does not switch accounts automatically.</p>
    <h2 id="start" tabIndex={-1}>Queue and start</h2>
    <FlowDiagram id="signup-flow" label="From selection to registration" steps={[
      { title: "Queue event", detail: "Save a task in the app" },
      { title: "Start Muse", detail: "Paste the instruction yourself" },
      { title: "Provider form", detail: "Muse completes signup" },
      { title: "Reported result", detail: "Track progress in the app" },
    ]} />
    <ol>
      <li>While signed in, choose <strong>Sign up with Muse</strong> beside an event title. The same action is available in Events, Map, Calendar, source pages and graph details.</li>
      <li>Review the queued events in <Link href="/?registrations=1">Registrations</Link>. Clicking an already queued event opens its progress.</li>
      <li>Copy the instruction below, <a href={MUSE_URL} target="_blank" rel="noopener noreferrer">open Muse</a>, and paste it to start.</li>
    </ol>
    <p>Unavailable actions show the reason on hover, keyboard focus or tap. Opening Muse does not start registrations. Muse may ask you to sign in to the event provider or answer required questions.</p>
    <MuseInstructions />
    <h2 id="progress" tabIndex={-1}>Registration progress</h2>
    <p>The header shows queued, in-progress, registered and needs-attention totals. Open <Link href="/?registrations=1">Registrations</Link> to filter tasks by status or provider. Only the displayed unread versions are marked as read.</p>
    <p>Each task shows its status, update time and result note or next step. Expand <strong>Activity</strong> for its queue time and latest saved status. Times use your local time zone. Visible panels refresh every 15 seconds; intermediate Muse browser steps are not shared.</p>
    <table aria-label="Registration statuses"><thead><tr><th scope="col">Status</th><th scope="col">Meaning</th></tr></thead><tbody>
      <tr><td>Queued</td><td>Saved; waiting for Muse to claim the task. A lavender clock marks queued cards.</td></tr>
      <tr><td>In progress</td><td>Muse claimed the task.</td></tr>
      <tr><td>Needs your input</td><td>Muse needs a login, answer or approval from you.</td></tr>
      <tr><td>Awaiting organizer approval</td><td>Submitted; the organizer’s decision is pending.</td></tr>
      <tr><td>Waitlisted</td><td>The provider placed you on a waitlist.</td></tr>
      <tr><td>Registered · reported by Muse</td><td>Muse reported a confirmation reference or evidence link.</td></tr>
      <tr><td>Failed</td><td>The attempt ended without a confirmed registration.</td></tr>
      <tr><td>Needs verification</td><td>The outcome is uncertain. Check the provider before another submission.</td></tr>
    </tbody></table>
    <p>When Muse supplies a private thread link, choose <strong>Muse conversation</strong> to read the reason for a result. Older reports may have no saved link. Conversation links require access to your Muse account; they do not publish the chat. <strong>Provider evidence</strong> opens the separate provider confirmation. Events Concierge records Muse’s report; it does not independently check your provider RSVP. Confirm your registration on the event website.</p>
    <p>Remove one task with its trash icon, select up to 100 loaded tasks, or choose <strong>Remove all</strong> across every page and status. Restore items from <strong>Removed</strong>. Removal stops unclaimed work; already started work may finish. It does not cancel an RSVP on the provider’s website.</p>
    <h2 id="recovery" tabIndex={-1}>Limits and recovery</h2>
    <ul>
      <li>Scheduled, free Luma, Meetup or Partiful events that are not sold out are supported. Upcoming events may have no end time; ongoing events need a confirmed end time still in the future. Ended events, invalid dates and unconfirmed admission remain unavailable.</li>
      <li>The app requires provider details observed within the last 48 hours. Muse rechecks the event’s dates, price and availability before submitting.</li>
      <li>If event details are too old, wait for a source refresh or register on the provider’s website.</li>
      <li>Tasks are not automatically retried. Repeated clicks open existing progress. Existing attempts can still receive status updates after the event ends.</li>
      <li>In <Link href="/settings/registrations">Registration settings</Link>, <strong>Replace key</strong> revokes the old key. <strong>Revoke key</strong> removes connector access.</li>
      <li>Key revocation cannot stop an already running Muse browser task or withdraw a registration. Stop the task in Muse separately.</li>
    </ul>
    <RelatedGuides links={[{ href: "/docs/account#access", label: "Account and connector access" }, { href: "/docs/how-it-works#registrations", label: "How registration tasks work" }]} />
  </GuidePage>;
}
