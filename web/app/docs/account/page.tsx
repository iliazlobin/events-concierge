import type { Metadata } from "next";
import Link from "next/link";
import { PageContents, RelatedGuides } from "@/components/docs/docs-kit";
export const metadata: Metadata = { title: "Your account" };

export default function AccountGuide() {
  return <>
    <h1>Your account</h1>
    <p>Event browsing is public. Saving filters, editing a profile and using Muse require sign-in.</p>
    <PageContents links={[{ href: "#profile", label: "Profile" }, { href: "#saved-filters", label: "Saved filters" }, { href: "#access", label: "Access and data" }]} />
    <h2 id="profile" tabIndex={-1}>Profile</h2>
    <p>Edit your name and photo in <Link href="/settings">Profile settings</Link>. Account email comes from your sign-in provider.</p>
    <h2 id="saved-filters" tabIndex={-1}>Saved filters and interests</h2>
    <ol>
      <li>Choose <Link href="/docs/browsing#filters">search filters</Link> in the catalog.</li>
      <li>Click <strong>Save filters</strong> and name the selection.</li>
      <li>Reuse or remove it in <Link href="/settings/saved-filters">Saved filters</Link>.</li>
    </ol>
    <p><Link href="/settings/taste">Interests</Link> stores preferences; it does not filter discovery results.</p>
    <h2 id="access" tabIndex={-1}>Access and data</h2>
    <table aria-label="Account permissions"><thead><tr><th scope="col">Access</th><th scope="col">Allowed</th></tr></thead><tbody>
      <tr><td>Guest</td><td>Browse the shared catalog.</td></tr>
      <tr><td>Your account</td><td>Manage your profile, saved filters and registrations.</td></tr>
      <tr><td>Muse connector</td><td>Read and update your selected registration tasks.</td></tr>
    </tbody></table>
    <p>Sign in through the provider offered by the app. Password and recovery changes belong in that provider’s settings.</p>
    <p>Registration tasks and connection keys belong to your account. Another account cannot view or update them.</p>
    <p>A <Link href="/docs/muse#connect">Muse connection key</Link> grants access to registration tasks. It does not grant account administration or access to your provider credentials.</p>
    <p><Link href="/settings/account">Account and data</Link> shows the deletion options available for your sign-in configuration.</p>
    <RelatedGuides links={[{ href: "/docs/browsing#filters", label: "Find and filter events" }, { href: "/docs/muse#connect", label: "Connect Muse" }]} />
  </>;
}
