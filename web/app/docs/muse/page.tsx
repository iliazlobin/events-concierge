import type { Metadata } from "next";
import Link from "next/link";
import { MuseInstructions } from "@/components/docs/muse-instructions";
import { MUSE_URL } from "@/lib/muse";
import { CodeBlock, FlowDiagram, PageContents, RelatedGuides } from "@/components/docs/docs-kit";
export const metadata: Metadata = { title: "Muse signups" };

export default function MuseGuide() {
  return <>
    <h1>Muse signups</h1>
    <p>Queue upcoming free Luma or Meetup events. Muse completes registration in its own browser.</p>
    <p>Muse signups appear only when enabled for this app.</p>
    <PageContents links={[{ href: "#connect", label: "Connect" }, { href: "#start", label: "Queue and start" }, { href: "#progress", label: "Progress" }, { href: "#recovery", label: "Limits and recovery" }]} />
    <h2 id="connect" tabIndex={-1}>Connect Muse</h2>
    <ol>
      <li>Sign in and open <Link href="/settings/muse">Muse settings</Link>.</li>
      <li>Expand <strong>Connector setup</strong> and copy the <strong>Connector URL</strong>. The app must use a public HTTPS address; local preview addresses are unavailable to Muse.</li>
      <li>Configure a Muse connector with that URL and <strong>Bearer token</strong> authentication.</li>
      <li>Choose <strong>Create key</strong> in Events Concierge. Paste the key into the connector’s credential field.</li>
    </ol>
    <p>The key is shown once and expires after 30 days. An active key confirms that Events Concierge issued access; it does not confirm that Muse connected.</p>
    <p>Bearer authentication sends the connection key in the request’s authorization header:</p>
    <CodeBlock label="Connector request format — placeholder key">{"GET /v1/muse/connector/batches?limit=100\nAuthorization: Bearer YOUR_CONNECTION_KEY"}</CodeBlock>
    <p>See <a href="https://muse.ai/platform/docs" target="_blank" rel="noopener noreferrer">Muse’s connector guidelines</a> for connector requirements. Connector setup and provider sign-in must be checked separately.</p>
    <h2 id="start" tabIndex={-1}>Queue and start</h2>
    <FlowDiagram id="signup-flow" label="From selection to registration" steps={[
      { title: "Queue event", detail: "Save a task in the app" },
      { title: "Start Muse", detail: "Paste the instruction yourself" },
      { title: "Provider form", detail: "Muse completes signup" },
      { title: "Reported result", detail: "Track progress in the app" },
    ]} />
    <ol>
      <li>Click Muse beside an event title. This saves a registration task and opens <strong>Registrations</strong>.</li>
      <li>Review the queued events in <Link href="/?registrations=1">Registrations</Link>. Clicking an already queued event opens its progress.</li>
      <li>Copy the instruction below, <a href={MUSE_URL} target="_blank" rel="noopener noreferrer">open Muse</a>, and paste it to start.</li>
    </ol>
    <p>Opening Muse does not start registrations. Muse may ask you to sign in to the event provider or answer required questions.</p>
    <MuseInstructions />
    <h2 id="progress" tabIndex={-1}>Registration progress</h2>
    <p>The header’s <Link href="/?registrations=1">Registrations</Link> button opens the queue. Unread updates are marked until viewed.</p>
    <table aria-label="Registration statuses"><thead><tr><th scope="col">Status</th><th scope="col">Meaning</th></tr></thead><tbody>
      <tr><td>Queued</td><td>Saved; waiting for Muse to claim the task.</td></tr>
      <tr><td>In progress</td><td>Muse claimed the task.</td></tr>
      <tr><td>Needs input</td><td>Muse needs a login, answer or approval from you.</td></tr>
      <tr><td>Awaiting organizer approval</td><td>Submitted; the organizer’s decision is pending.</td></tr>
      <tr><td>Waitlisted</td><td>The provider placed you on a waitlist.</td></tr>
      <tr><td>Registered</td><td>Muse reported a confirmation reference or evidence link.</td></tr>
      <tr><td>Failed</td><td>The attempt ended without a confirmed registration.</td></tr>
      <tr><td>Unconfirmed</td><td>The outcome is uncertain. Check the provider before another submission.</td></tr>
    </tbody></table>
    <p>Events Concierge records Muse’s report; it does not independently check your provider RSVP. Confirm your registration on the event website.</p>
    <h2 id="recovery" tabIndex={-1}>Limits and recovery</h2>
    <ul>
      <li>Only upcoming, scheduled, free Luma/Meetup events are supported. The app requires provider details observed within the last 48 hours.</li>
      <li>Tasks are not automatically retried. Repeated clicks open existing progress.</li>
      <li>In <Link href="/settings/muse">Muse settings</Link>, <strong>Replace key</strong> revokes the old key. <strong>Revoke key</strong> removes connector access.</li>
      <li>Key revocation cannot stop an already running Muse browser task or withdraw a registration. Stop the task in Muse separately; see <a href="https://www.meta.com/help/artificial-intelligence/2124746764949121/" target="_blank" rel="noopener noreferrer">Muse’s browser help</a>.</li>
    </ul>
    <RelatedGuides links={[{ href: "/docs/account#access", label: "Account and connector access" }, { href: "/docs/how-it-works#registrations", label: "How registration tasks work" }]} />
  </>;
}
