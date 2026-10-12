import type { Metadata } from "next";
import Link from "next/link";
export const metadata: Metadata = { title: "Your account" };

export default function AccountGuide() {
  return <>
    <h1>Your account</h1>
    <p>Event browsing is public. Saving filters, editing a profile and using Muse require sign-in.</p>
    <h2>Settings</h2>
    <ul>
      <li><strong>Profile:</strong> your name and photo. Account email comes from your sign-in provider.</li>
      <li><strong>Saved filters:</strong> reuse or remove saved search selections.</li>
      <li><strong>Interests:</strong> stored preferences. They do not filter discovery results.</li>
      <li><strong>Muse signups:</strong> registration access keys. See the <Link href="/docs/muse">Muse guide</Link>.</li>
      <li><strong>Account and data:</strong> account controls and the deletion options available for your sign-in configuration.</li>
    </ul>
    <h2>Account access</h2>
    <p>Sign in through the provider offered by the app. Password and recovery changes belong in that provider’s settings.</p>
    <p>Registration tasks and connection keys belong to your account. Another account cannot view or update them.</p>
    <p>A Muse connection key grants access to registration tasks. It does not grant account administration or access to your provider credentials.</p>
    <p><Link href="/settings">Open settings</Link></p>
  </>;
}
