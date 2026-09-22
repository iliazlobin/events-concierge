export const meta = {
  name: 'ec-rubric-gate',
  description: 'Review the concise accepted design: structure, two independent scores and contract consistency',
  phases: [{ title: 'Gate', detail: 'lint, score x2, consistency' }],
}

const WS = '/Users/iliazlobin/Claude/events-concierge'
const DOC = WS + '/design/system-design.md'
const PROJECT = WS + '/PROJECT.md'
const ARCHITECTURE = WS + '/ARCHITECTURE.md'

const SCOPE = [
  'Review a living accepted-design summary, not an options paper. Read PROJECT.md and ARCHITECTURE.md before the design, then follow relevant links to detailed contracts and code.',
  'PROJECT.md defines current product scope. Distinguish implemented behavior, accepted but deferred capabilities, and deployment acceptance. Do not promote historical full-concierge requirements or ADR parameters into current launch commitments.',
  'Prefer short factual bullets, tables and ordered flows; one point per item. Remove long paragraphs, rejected options and decision narratives. Do not demand a numbered pyramid, Approach/Decision/Rationale sections, trade-off tables or a bibliography.',
  'Preserve ownership, data flow, safety invariants, failure behavior and active limits. Link detailed contracts beside the relevant statement instead of copying schemas, endpoint inventories, commands or research. Primary-source citations stay inline.',
  'Concise linked coverage is sufficient; fewer words do not excuse missing invariants. Report missing or contradictory facts, not absent historical narration. Do not invent sizes, quotas, performance figures or verification evidence.',
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
    'You are independent scorer ' + id + '. Read ' + PROJECT + ', ' + ARCHITECTURE + ' and ' + DOC + ', then the relevant linked contracts/code. Score all eight existing dimensions 0-5.',
    SCOPE,
    'Dimension meanings: requirements = current scope and deferred boundaries; back_of_envelope = relevant active limits and honestly labelled targets, without speculative scale arithmetic; data_model = authoritative stores, ownership and isolation; api = public versus operator authority and durable boundaries; hld = components and execution/data flows; deep_dives = critical invariants and failure behavior, with detail linked; voice = concise factual accepted design; references = relevant, accessible inline sources and contract links.',
    'PASS requires total >= 33, no dimension < 3, and requirements/back_of_envelope/data_model/hld each >= 4. Otherwise BLOCK with concrete findings. Quote the passage or identify the missing contract for scores below 4. Return only actionable improvements; do not expand the document to satisfy an old format. Use structured output.',
  ].join('\n\n')
}

const lintPrompt = [
  'Read ' + DOC + ' and its linked Markdown targets. Check structural correctness only:',
  SCOPE,
  '- Short topic headings, factual bullets/tables and bounded numbered flows. No mandatory heading names or section count.',
  '- No standalone References, alternative-comparison or decision-narrative sections. Keep ADR/research evidence in its owning files.',
  '- Links resolve, including incoming anchors from other repository docs; code fences and tables are valid.',
  '- Mermaid diagrams use declared nodes and understandable direction, label people User and browsers Web client, and distinguish application services from shared infrastructure. Do not require a fixed node count.',
  '- Deferred features and unverified deployment claims are visibly labelled; source/code presence is not deployed acceptance.',
  'Return exact locations and proposed fixes. Use blocker for broken structure/links or misleading scope; major for material readability defects. Return an empty findings array when conformant. Use structured output.',
].join('\n')

const consistencyPrompt = [
  'Cross-check ' + DOC + ' against ' + PROJECT + ', ' + ARCHITECTURE + ', relevant linked component contracts, code and operations docs.',
  SCOPE,
  '- Report actual contradictions in scope, component ownership, authority, persistence, retry behavior, security boundaries, active limits or deployment status.',
  '- Historical ADRs and research describe their own accepted context. Do not overwrite them, treat their open riders as settled, or demand deferred behavior for the current discovery milestone.',
  '- Verify quotes and include both the design passage and supporting source passage. Omission of linked implementation detail is not a contradiction; omission of an essential safety invariant is.',
  'Read only. Return structured findings; do not run migrations, providers, cloud operations or publishing workflows.',
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