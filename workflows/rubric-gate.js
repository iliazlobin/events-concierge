export const meta = {
  name: 'ec-rubric-gate',
  description: 'Rubric gate for events-concierge system-design.md: linter + 2 independent scorers + consistency adversary',
  phases: [{ title: 'Gate', detail: 'lint, score x2, consistency' }],
}

const WS = '/Users/iliazlobin/Claude/events-concierge'
const DOC = WS + '/design/system-design.md'
const REQ = WS + '/design/requirements.md'
const RUBRIC = '/Users/iliazlobin/.hermes/skills/research/system-design-kanban/references/design-doc-rubric.md'
const PANELS = [1, 2, 3, 4].map(n => WS + '/design/panels/0' + n + '-' + ['discovery-topology', 'registration-orchestration', 'credential-isolation', 'handoff-lifecycle'][n - 1] + '.md')

const SCOPE = [
  'SCOPING RULES (apply these; they override conflicting clauses in the rubric file):',
  '1. This is a PROJECT BUILDOUT design doc on the 8-section pyramid: sections 1-6 as in the rubric, then "## 7. Trade-offs" (a decision/rejected/why table) and "## 8. References" (the numbered primary-source list). The rubric file describes a 7-section variant for a different pipeline - do NOT flag the presence of section 7 Trade-offs or References living at section 8. References rules (numbered list, clickable links, primary sources only, nothing trailing) apply to section 8.',
  '2. Ignore every Notion-row concern: the Sources row-property, page icons, Notion block types (bulleted_list_item vs paragraph rendering). This artifact is a markdown FILE; judge the markdown source.',
  '3. Callouts are represented in markdown as <callout icon="..." color="yellow_background"> HTML blocks - treat these as valid callout blocks (this is the house convention for the format; the icon lives on the attribute, the body must not repeat the glyph).',
  '4. The trailing "Human Writing Standard" typography section of the rubric (em-dash/en-dash ban, ASCII-only) does NOT apply to this buildout format - do not flag em-dashes. ALL other voice rules (dim 7: no provenance/agentic language, no study-resource or company-attribution-as-justification, no AI-tell phrasing) apply in full.',
  '5. Everything else in the rubric applies verbatim: the two-bars philosophy (scaffolding = concision, substance = explanation), all dimension bars, mechanism-leakage rules for section 2, no decisions in section 3, brace-block entities, bullet API, per-FR H4 walkthroughs with Components/Flow/Design consideration, Problem-framed DDs with numbered Approach headings, Mermaid-only diagrams with fill+color styling, the 6-box cap on the section 1 overview, no emoji outside callout icons.',
].join('\n')

const FIND = { type: 'object', required: ['findings'], properties: { findings: { type: 'array', items: { type: 'object', required: ['severity', 'location', 'issue', 'proposed_fix'], properties: { severity: { type: 'string', enum: ['blocker', 'major', 'minor'] }, location: { type: 'string' }, issue: { type: 'string' }, proposed_fix: { type: 'string' } } } } } }

const DIM = { type: 'object', required: ['score', 'justification'], properties: { score: { type: 'integer' }, justification: { type: 'string' } } }
const SCORECARD = { type: 'object', required: ['scores', 'total', 'gate', 'blockers', 'improvements'], properties: {
  scores: { type: 'object', required: ['requirements', 'back_of_envelope', 'data_model', 'api', 'hld', 'deep_dives', 'voice', 'references'], properties: { requirements: DIM, back_of_envelope: DIM, data_model: DIM, api: DIM, hld: DIM, deep_dives: DIM, voice: DIM, references: DIM } },
  total: { type: 'integer' },
  gate: { type: 'string', enum: ['PASS', 'BLOCK'] },
  blockers: { type: 'array', items: { type: 'string' } },
  improvements: { type: 'array', items: { type: 'string' }, description: 'highest-leverage concrete edits, most valuable first' } } }

function scorerPrompt(id) {
  return [
    'You are independent rubric scorer ' + id + ' for a system-design document. Read, in order: (1) the rubric file ' + RUBRIC + ' IN FULL, (2) the document ' + DOC + ' IN FULL. Then score every one of the 8 dimensions 0-5 (integers) applying the rubric bars verbatim under the scoping rules below. Be harsh where the rubric says to be harsh: scaffolding dims score DOWN when borderline-verbose; substance dims score DOWN when thin or unexplained. Quote the offending passage in any justification below 4. Gate: PASS requires total >= 33, no dimension < 3, and dims requirements/back_of_envelope/data_model/hld each >= 4; otherwise BLOCK with concrete blockers.',
    SCOPE,
    'Also list the highest-leverage improvements (concrete edits with location + exact replacement direction) even on PASS. Return via structured output.',
  ].join('\n\n')
}

const lintPrompt = [
  'You are a structural linter for a system-design markdown document. Read ' + DOC + ' and check ONLY these hard structural gates (not prose quality):',
  '- Body is EXACTLY these 8 H2 sections in order, nothing before section 1, nothing after section 8\'s numbered list: "## 1. Problem", "## 2. Requirements", "## 3. Back of the envelope", "## 4. Entities", "## 5. High-Level Design", "## 6. Deep dives", "## 7. Trade-offs", "## 8. References". Case-sensitive, lowercase past the number where shown (e.g. "Back of the envelope", "Deep dives"). No H1 title, no TL;DR, no pre-section-1 content.',
  '- Section 1 Mermaid overview has <= 6 nodes and role/tech-neutral labels (no product names).',
  '- Section 2: labels are exactly **Functional** and **Non-functional** as bare bold paragraphs; FR/NFR lines are markdown bullets "- FR1: ..."; one bold **Out of scope:** line.',
  '- Section 4: ONE sql-fenced brace-block for all entities; markers limited to PK/FK/CK; then an "### API" H3 with a bullet list of `METHOD /path` chips (no table, no escaped backticks/braces).',
  '- Section 5: per-FR subsections are H4 "#### FR<N>:" matching section 2 FR numbering; each has Components / Flow (numbered steps) / Design consideration bullets.',
  '- Section 6: dives headed "### DD<N>: <name>"; approaches headed on their own line as "**Approach N: <name>**" (no trailing period, no run-in prose, no (chosen) marker); sub-points bold-labelled; each dive opens with "**Problem.**".',
  '- Diagrams: every diagram is a ```mermaid fence (no ASCII-art boxes in plain fences); no literal \\n in node labels; flowchart classDefs set BOTH fill: and color:; NO classDef/class lines inside any sequenceDiagram; no ";" inside sequenceDiagram message labels; no "@" in node labels; no empty ""-labelled node/subgraph; edge labels containing { } [ ] ( ) are quoted; every edge references a declared node.',
  '- No emoji/icon glyphs anywhere in body text; the ONLY sanctioned glyph carrier is the icon="..." attribute of <callout> blocks, all of which must have color="yellow_background", and the callout BODY must not start with the icon glyph.',
  '- Section 7 is a single table (header row: Decision | Rejected alternative | Why). Section 8 is ONLY a numbered list of [title](url) links - no subheadings, no grouping labels, no trailing content, no bare URLs, no interview-prep/tutorial sites.',
  'Report every violation as a finding with exact location; severity blocker for hard-gate breaks, major for borderline. Empty findings array if fully conformant. Return via structured output.',
].join('\n')

const consistencyPrompt = [
  'You are a consistency adversary. Cross-check the design document ' + DOC + ' against (a) the BINDING signed requirements ' + REQ + ' and (b) the four judge-panel verdicts: ' + PANELS.join(' , ') + '. Hunt for silent drift - report ONLY real contradictions or misrepresentations, most severe first:',
  '- A number in the doc that contradicts the requirements or a verdict (SLA targets, quotas, budgets, attempt counts, TTLs, concurrency, costs, cadences).',
  '- A mechanism the doc claims that a panel verdict decided AGAINST, or a verdict-decided mechanism the doc materially misstates (silently weakened launch default, inverted posture, dropped invariant that the verdict called load-bearing).',
  '- A doc statement that violates a HARD scope decision or a requirements-level obligation (e.g. Gmail scopes, IP rotation, paid checkout, per-action confirmation prompts, RelayInbox used outbound, blind re-POST).',
  '- An internal contradiction between two sections of the doc itself.',
  'Do NOT report: abstraction (the doc legitimately omits panel detail - it is a summary artifact over the verdicts); stylistic differences; owner-ratification items being presented as decisions WITH their parameters (that is expected - the ADR log carries the riders). Verify quotes before reporting; include the doc passage AND the contradicting source passage in each finding. Return via structured output.',
].join('\n')

phase('Gate')
const [lint, scoreA, scoreB, consistency] = await parallel([
  () => agent(lintPrompt, { label: 'lint', phase: 'Gate', schema: FIND, effort: 'medium' }),
  () => agent(scorerPrompt('A'), { label: 'score:A', phase: 'Gate', schema: SCORECARD, effort: 'xhigh' }),
  () => agent(scorerPrompt('B'), { label: 'score:B', phase: 'Gate', schema: SCORECARD, effort: 'xhigh' }),
  () => agent(consistencyPrompt, { label: 'consistency', phase: 'Gate', schema: FIND, effort: 'xhigh' }),
])
log('gate done: A=' + (scoreA ? scoreA.total + '/' + scoreA.gate : 'null') + ' B=' + (scoreB ? scoreB.total + '/' + scoreB.gate : 'null') + ' lint=' + (lint ? lint.findings.length : '?') + ' consistency=' + (consistency ? consistency.findings.length : '?'))
return { lint, scoreA, scoreB, consistency }