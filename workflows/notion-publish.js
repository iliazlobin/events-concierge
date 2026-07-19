export const meta = {
  name: 'ec-notion-publish',
  description: 'Publish the Events Concierge design package into the four Notion hub placeholder pages',
  phases: [{ title: 'Publish', detail: 'one agent per placeholder page' }],
}

const WS = '/Users/iliazlobin/Claude/events-concierge'
const PAGES = {
  requirements: '391d865005a8816b830bd280c9f2479e',
  systemDesign: '391d865005a88164a182eabc18fe068f',
  decisionLog: '391d865005a881c78431ef1340ae7496',
  research: '391d865005a881939eabc0a529bf52fc',
}

const CONVENTIONS = [
  'You are publishing local markdown into a specific Notion page using the claude.ai Notion connector.',
  'STEP 0 — load the Notion tools in ONE call: ToolSearch with query "select:mcp__claude_ai_Notion__notion-fetch,mcp__claude_ai_Notion__notion-update-page,mcp__claude_ai_Notion__notion-create-pages". You have MCP access (verified).',
  'CONVERSION RULES (Notion-flavored Markdown), apply to every file body you send:',
  '- Strip the file\'s FIRST-level H1 line ("# ...") from the body; use its text (minus "# ") as the Notion page title. The page title carries the name, so the body must NOT repeat it.',
  '- Callouts: any block written as <callout icon="X" color="yellow_background"> must have its color changed to color="yellow_bg" (Notion uses yellow_bg, not yellow_background). Keep the icon and body unchanged.',
  '- Keep fenced code blocks verbatim, including ```mermaid , ```sql , ```text — do NOT escape characters inside code fences. Mermaid <br/> in node labels stays as-is (it renders inside the fence).',
  '- Keep tables verbatim whether they are <table header-row="true">...</table> HTML blocks or GitHub-style | a | b | pipe tables; the connector renders both.',
  '- Do not otherwise reword, summarize, or re-order the content. Publish it faithfully.',
  'ORDER OF OPERATIONS for a placeholder page that also gets child pages: FIRST call notion-update-page with command "replace_content" to set the placeholder body (the placeholders currently have only placeholder text and no child pages, so replace is safe). THEN call notion-create-pages to add child pages. Never create children before replacing the body, or replace_content will try to delete them.',
  'CHILD PAGES: use notion-create-pages with parent {"type":"page_id","page_id":"<TARGET>"} and a "pages" array; each element is {"properties":{"title":"<child title>"},"content":"<converted body>"}. The array order IS the creation order, and Notion sorts children by creation time, so ALWAYS pass children in ascending numeric order. Create in batches of at most 4 pages per call to keep the payload safe; issue the batches sequentially (await each before the next) so order is preserved across batches.',
  'VERIFY at the end: fetch the target page and confirm the body landed and the expected number of children exist. Report what you did.',
].join('\n')

const RESULT = { type: 'object', required: ['page_body_set', 'children_created', 'errors'], properties: {
  page_body_set: { type: 'boolean' },
  children_created: { type: 'array', items: { type: 'string', description: 'child page title (+ id/url if known)' } },
  errors: { type: 'array', items: { type: 'string' } },
  notes: { type: 'string' } } }

const reqPrompt = [
  CONVENTIONS,
  'YOUR TARGET PAGE: the "Requirements Specification" placeholder, page_id ' + PAGES.requirements + '.',
  'TASK: replace_content of that page with the body of ' + WS + '/design/requirements.md (strip its leading H1). This is the signed-off v0.2 requirements — publish it in full and faithfully. No child pages. Then verify and report via the structured tool.',
].join('\n\n')

const sdPrompt = [
  CONVENTIONS,
  'YOUR TARGET PAGE: the "System Design" placeholder, page_id ' + PAGES.systemDesign + '.',
  'TASK:',
  '1. replace_content of that page with the body of ' + WS + '/design/system-design.md (strip nothing at the top — this file has NO H1, it starts at "## 1. Problem"; publish from the first line). Remember the callout color conversion (yellow_background -> yellow_bg) — this file has ~6 callouts. Keep all ```mermaid and ```sql/```text fences verbatim.',
  '2. Then create 4 child pages under it, in this ascending order, each = the panel file body minus its leading "# Panel N: ..." H1, titled by that H1 text:',
  '   - ' + WS + '/design/panels/01-discovery-topology.md',
  '   - ' + WS + '/design/panels/02-registration-orchestration.md',
  '   - ' + WS + '/design/panels/03-credential-isolation.md',
  '   - ' + WS + '/design/panels/04-handoff-lifecycle.md',
  'Verify the body + 4 children landed, then report via the structured tool.',
].join('\n\n')

const dlPrompt = [
  CONVENTIONS,
  'YOUR TARGET PAGE: the "Decision Log" placeholder, page_id ' + PAGES.decisionLog + '.',
  'TASK:',
  '1. replace_content of that page with the body of ' + WS + '/decisions/README.md (strip its leading H1). This is the ADR index table + open-gates section. Its internal links like [ADR-001](adr-001-...md) are relative file links that will not resolve in Notion — that is acceptable, leave them as plain link text; do NOT try to rewrite them to Notion URLs.',
  '2. Then create child pages under it, in this ascending order (the first is the owner decision brief, then the 11 ADRs), each = the file body minus its leading "# ..." H1, titled by that H1 text:',
  '   - ' + WS + '/design/owner-decisions.md  (title it "Owner Decision Brief")',
  '   - ' + WS + '/decisions/adr-001-single-postgres-catalog.md',
  '   - ' + WS + '/decisions/adr-002-tm-crawl-plan-budget-ledger.md',
  '   - ' + WS + '/decisions/adr-003-two-tier-orchestration.md',
  '   - ' + WS + '/decisions/adr-004-data-plane-policy-killswitch.md',
  '   - ' + WS + '/decisions/adr-005-redis-fair-share-pacer.md',
  '   - ' + WS + '/decisions/adr-006-managed-fleet-sovereign-trust-plane.md',
  '   - ' + WS + '/decisions/adr-007-db-anchored-lifecycle.md',
  '   - ' + WS + '/decisions/adr-008-central-change-detection.md',
  '   - ' + WS + '/decisions/adr-009-email-launch-notification-channel.md',
  '   - ' + WS + '/decisions/adr-010-temporal-cloud-engine.md',
  '   - ' + WS + '/decisions/adr-011-relay-inbox-no-gmail.md',
  'The owner-decisions.md file uses "- [ ]" task-list checkboxes — keep them as Notion to-do items (that is valid Notion-flavored markdown). Verify the body + 12 children landed, then report via the structured tool.',
].join('\n\n')

const dossiers = ['00-research-brief', '00b-gap-remediation-addendum', '00c-product-landscape-brief',
  '01-agent-orchestration-2026', '02-autonomous-browser-registration', '03-event-source-landscape',
  '04-autonomous-action-safety', '05-multitenancy-auth-secrets', '06-calendar-integration',
  '07-ranking-personalization', '08-clean-architecture-agentic', '09-cost-efficiency-scale',
  '10-per-source-legal-tos', '11-source-tier-reverify-2026', '12-google-oauth-casa-gate',
  '13-durable-execution-engine', '14-credential-injection-worker-isolation', '15-capacity-quota-model',
  '16-email-ingestion-architecture', '17-meetup-rsvp-prerequisite-chain', '18-commercial-event-discovery-products',
  '19-agentic-assistants-that-book', '20-open-source-event-aggregation', '21-indie-builds-and-writeups',
  '22-academic-event-recsys-web-agents', '23-market-failures-postmortems', '24-adjacent-concierge-scheduling',
  '25-demand-signals-user-jobs']

const researchPrompt = [
  CONVENTIONS,
  'YOUR TARGET PAGE: the "Research" placeholder, page_id ' + PAGES.research + '.',
  'TASK:',
  '1. replace_content of that page with a short index you write: a one-paragraph note that this holds the Events Concierge research corpus (28 fact-checked artifacts) followed by three bold group labels as plain text — "Synthesis briefs" (00, 00b, 00c), "Technical dossiers" (01-17), "Product-landscape dossiers" (18-25) — each with a one-line description. Keep it brief; the dossiers themselves are the child pages.',
  '2. Then create 28 child pages under it, in EXACTLY this ascending order, each = the dossier file body minus its leading "# ..." H1, titled by that H1 text. Files live in ' + WS + '/research/<name>.md :',
  dossiers.map((d, i) => '   ' + (i + 1) + '. ' + d + '.md').join('\n'),
  'Create them in batches of at most 4 per notion-create-pages call, sequentially, in ascending order, so the children sort correctly. These are large files (15-33 KB each) — publish each faithfully; do not summarize. Verify all 28 children landed (fetch the page and count), then report via the structured tool, listing any file that failed so it can be retried.',
].join('\n\n')

phase('Publish')
const [req, sd, dl, research] = await parallel([
  () => agent(reqPrompt, { label: 'publish:requirements', phase: 'Publish', schema: RESULT, effort: 'medium' }),
  () => agent(sdPrompt, { label: 'publish:system-design', phase: 'Publish', schema: RESULT, effort: 'medium' }),
  () => agent(dlPrompt, { label: 'publish:decision-log', phase: 'Publish', schema: RESULT, effort: 'medium' }),
  () => agent(researchPrompt, { label: 'publish:research', phase: 'Publish', schema: RESULT, effort: 'medium' }),
])
log('publish done: req=' + (req && req.page_body_set) + ' sd=' + (sd ? sd.children_created.length + ' panels' : 'null') + ' dl=' + (dl ? dl.children_created.length + ' children' : 'null') + ' research=' + (research ? research.children_created.length + ' dossiers' : 'null'))
return { req, sd, dl, research }