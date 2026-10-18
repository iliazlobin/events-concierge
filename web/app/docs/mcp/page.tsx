import type { Metadata } from "next";
import Link from "next/link";
import { CodeBlock, GuidePage, RelatedGuides } from "@/components/docs/docs-kit";
export const metadata: Metadata = { title: "MCP discovery" };

export default function McpGuide() {
  return <GuidePage title="MCP discovery" intro={<p>Connect an assistant to search the Events Concierge catalog and compare events.</p>}
    contents={[{ href: "#connect", label: "Connect" }, { href: "#discover", label: "Find events" }]}>
    <h2 id="connect" tabIndex={-1}>Connect MCP</h2>
    <p>Use MCP for read-only catalog discovery in a client supporting <strong>Streamable HTTP</strong> and Bearer authentication. For Muse’s existing OpenAPI connector, follow <Link href="/docs/muse#connect">Connect Muse</Link>. That connector also supports discovery.</p>
    <ol>
      <li>Open <Link href="/settings/mcp">MCP settings</Link> and copy <strong>MCP URL</strong>. Use the deployed public HTTPS address, not localhost.</li>
      <li>Add a remote MCP server in your client, select <strong>Streamable HTTP</strong> and paste the URL.</li>
      <li>Set Bearer authentication to your existing connection key, or create a key in settings. Enter it only in the secure credential field; never in chat or the URL.</li>
      <li>Connect and check that <code>search_events</code> and <code>get_event</code> are available. Try the <a href="#discover">discovery request below</a>.</li>
    </ol>
    <CodeBlock label="MCP connection — replace the example origin and key">{"URL: https://your-events-site.example/v1/muse/mcp\nTransport: Streamable HTTP\nAuthorization: Bearer YOUR_CONNECTION_KEY"}</CodeBlock>
    <p>The same key works for both connections. Replacing or revoking it removes access through both. MCP exposes no registration writes; keep the OpenAPI connector for queued signups.</p>
    <p>If connection fails, check the HTTPS URL and whether MCP is deployed and enabled. A 401 response means the key is missing, expired or revoked. If tools are missing, refresh the connection. An active key alone does not confirm connectivity.</p>
    <h2 id="discover" tabIndex={-1}>Find events with Muse</h2>
    <p>The connector can search the published catalog and read event details. Ask for a place, dates and interests; Muse returns source links and the last observed details.</p>
    <CodeBlock label="Discovery request">{"Find free AI events in San Francisco next week using Events Concierge. Show the source links and any signup limits. Do not register me yet."}</CodeBlock>
    <p>MCP exposes read-only <code>search_events</code> and <code>get_event</code> tools. Search by place, dates, topics, text and price. Results include source evidence, freshness and signup eligibility.</p>
    <p>Discovery does not start signups. Open a result in Events Concierge and queue it explicitly. Source timestamps and price or availability limits describe catalog observations; Muse rechecks the provider before registration. See <Link href="/docs/browsing">Find events</Link> for catalog coverage.</p>
    <RelatedGuides links={[{ href: "/docs/browsing", label: "Catalog coverage" }, { href: "/docs/muse", label: "Registrations with Muse" }]} />
  </GuidePage>;
}
