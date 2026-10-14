import type { Metadata } from "next";
import Link from "next/link";
import { GuidePage, RelatedGuides } from "@/components/docs/docs-kit";
export const metadata: Metadata = { title: "Account" };

export default function AccountGuide() {
  return <GuidePage
    title="Account"
    intro={<p>Event browsing is public. Saving filters, editing a profile and using Muse require sign-in.</p>}
    contents={[{ href: "#profile", label: "Profile" }, { href: "#saved-filters", label: "Saved filters" }, { href: "#interests", label: "Interests" }, { href: "#access", label: "Sign-in and access" }, { href: "#deletion", label: "Delete your account" }]}
  >
    <h2 id="profile" tabIndex={-1}>Profile</h2>
    <p>Edit your name and photo in <Link href="/settings">Profile settings</Link>. Account email comes from your sign-in provider.</p>
    <ul>
      <li>Your display name can contain up to 64 characters.</li>
      <li>Upload a PNG, JPEG or WebP photo. The app crops it to a 256-pixel square and removes camera metadata.</li>
      <li>Time zone is saved with your profile. Event times in the discovery views use your device’s time zone.</li>
    </ul>
    <h2 id="saved-filters" tabIndex={-1}>Saved filters</h2>
    <ol>
      <li>Choose <Link href="/docs/browsing#filters">search filters</Link> in the catalog.</li>
      <li>Click <strong>Save filters</strong> and name the selection.</li>
      <li>Reuse or remove it in <Link href="/settings/saved-filters">Saved filters</Link>.</li>
    </ol>
    <h2 id="interests" tabIndex={-1}>Interests</h2>
    <p>Choose or add interests in <Link href="/settings/taste">Interests</Link>, then select <strong>Save</strong>. Saved interests do not filter discovery results; use <Link href="/docs/browsing#filters">topic filters</Link> to choose which events you see.</p>
    <h2 id="access" tabIndex={-1}>Sign-in and access</h2>
    <table aria-label="Account permissions"><thead><tr><th scope="col">Access</th><th scope="col">Allowed</th></tr></thead><tbody>
      <tr><td>Guest</td><td>Browse the shared catalog.</td></tr>
      <tr><td>Account</td><td>Manage your profile, saved filters and registrations.</td></tr>
      <tr><td>Muse connector</td><td>Read and update your selected registration tasks.</td></tr>
    </tbody></table>
    <p>Sign in through the provider offered by the app. Password and recovery changes belong in that provider’s settings.</p>
    <p>Deployed sessions last up to eight hours and do not extend as you use the app. Actions that delete account data require a recent identity check when the sign-in configuration supports it.</p>
    <p>Registration tasks and connection keys belong to your account. Another account cannot view or update them.</p>
    <p>A <Link href="/docs/muse#connect">Muse connection key</Link> grants access to registration tasks. It does not grant account administration or access to your provider credentials.</p>
    <h2 id="deletion" tabIndex={-1}>Delete your account</h2>
    <p>Open <Link href="/settings/account">Account</Link>, choose the deletion action and type <code>DELETE MY ACCOUNT</code> to confirm. Deletion cannot be undone.</p>
    <p>An accepted request stops account access, cancels concierge work, removes any concierge-owned calendar entries, revokes sessions and stored credentials, and starts deletion of account records. Acceptance does not mean cleanup has finished; the receipt shows progress and any audit records retained.</p>
    <p>If Account shows deletion as unavailable with the legacy Google sign-in configuration, the required additional identity check is unavailable. Signing in again does not enable deletion.</p>
    <RelatedGuides links={[{ href: "/docs/browsing#filters", label: "Find and filter events" }, { href: "/docs/muse#connect", label: "Connect Muse" }]} />
  </GuidePage>;
}
