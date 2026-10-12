import type { Metadata } from "next";
import styles from "../docs.module.css";
export const metadata: Metadata = { title: "How it works" };

export default function HowItWorks() {
  return <>
    <h1>How it works</h1>
    <h2>Event discovery</h2>
    <ol className={styles.flow}>
      <li><strong>Collection workers</strong>Refresh event details from configured sources.</li>
      <li><strong>Catalog database</strong>Stores published event details and their sources. Failed refreshes retain the last successful catalog.</li>
      <li><strong>Catalog API</strong>Reads those records for search, filters and event details.</li>
      <li><strong>Web client</strong>Shows the results as an event list, map, calendar or entity graph.</li>
    </ol>
    <p>Browsing reads the collected catalog. It does not fetch every event website live. Provider availability can change between collection and registration.</p>
    <h2>Event roles</h2>
    <p>The graph links people and organizations to events through source-reported roles such as host or organizer. Names alone do not establish identity.</p>
    <h2>Muse registrations</h2>
    <ol className={styles.flow}>
      <li><strong>Your selection</strong>Queues an account-owned registration task.</li>
      <li><strong>Muse connector</strong>Reads queued tasks and claims an attempt after you start Muse.</li>
      <li><strong>Muse browser</strong>Checks the event and completes the provider’s registration flow.</li>
      <li><strong>Reported result</strong>Updates your task status and the in-app registration panel.</li>
    </ol>
    <p>Events Concierge manages the queue. Muse controls its browser and provider logins. Opening Muse does not automatically start the queue.</p>
  </>;
}
