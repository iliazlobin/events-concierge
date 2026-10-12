import type { Metadata } from "next";
import Link from "next/link";
import { CodeBlock, FlowDiagram, PageContents, RelatedGuides } from "@/components/docs/docs-kit";
export const metadata: Metadata = { title: "How it works" };

export default function HowItWorks() {
  return <>
    <h1>How it works</h1>
    <p>Search reads a collected catalog. Registration happens on the event provider’s website.</p>
    <PageContents links={[{ href: "#discovery", label: "Discovery" }, { href: "#roles", label: "Event roles" }, { href: "#registrations", label: "Registration tasks" }]} />
    <h2 id="discovery" tabIndex={-1}>Event discovery</h2>
    <FlowDiagram id="catalog-flow" label="How events reach the app" steps={[
      { title: "Collection workers", detail: "Refresh source listings" },
      { title: "Catalog database", detail: "Store published details" },
      { title: "Catalog API", detail: "Serve filtered records" },
      { title: "Web client", detail: "Show event results" },
    ]} />
    <p>The catalog API is the interface the web client uses to read events. Failed refreshes retain the last successful catalog.</p>
    <p>Browsing reads the collected catalog. It does not fetch every event website live. Provider availability can change between collection and registration.</p>
    <h2 id="roles" tabIndex={-1}>Event roles</h2>
    <p>The <Link href="/docs/browsing#sources">entity graph</Link> links people and organizations to events through source-reported roles such as host or organizer. Names alone do not establish identity.</p>
    <h2 id="registrations" tabIndex={-1}>Muse registration tasks</h2>
    <FlowDiagram id="task-flow" label="App queue and Muse execution" steps={[
      { title: "Your selection", detail: "Save an account-owned task" },
      { title: "Muse connector", detail: "Claim a task after manual start" },
      { title: "Muse browser", detail: "Check and register with provider" },
      { title: "Reported result", detail: "Update in-app progress" },
    ]} />
    <p>Events Concierge manages the queue. Muse controls its browser and provider logins. Opening Muse does not automatically start the queue.</p>
    <CodeBlock label="Saved task state — example excerpt">{'{\n  "status": "queued",\n  "version": 1,\n  "unread": false\n}'}</CodeBlock>
    <p>Changes to a task’s status or result advance its version. Unread marks an update you have not viewed. See <Link href="/docs/muse#progress">registration statuses</Link> for the meaning of each outcome.</p>
    <RelatedGuides links={[{ href: "/docs/browsing#views", label: "Explore the event views" }, { href: "/docs/muse#start", label: "Start a registration" }]} />
  </>;
}
