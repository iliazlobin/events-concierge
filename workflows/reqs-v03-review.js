export const meta = {
  name: 'ec-reqs-v03-review',
  description: '3-lens adversarial review of the requirements v0.3 deltas (D1-D8 + FR-2.7) then closure verify',
  phases: [{ title: 'Review' }, { title: 'Closure' }],
}

const WS = '/Users/iliazlobin/Claude/events-concierge'
const REQ = WS + '/design/requirements.md'
const OPINION = WS + '/design/product-opinion.md'
const BRIEF = WS + '/research/00c-product-landscape-brief.md'

const COMMON = [
  'The document under review is ' + REQ + ' — now at v0.3 DRAFT. Read it in full.',
  'v0.3 folds product-landscape deltas D1-D8 (as new groups FR-11 through FR-18 under the "## 4a" heading, plus NFR-18) and the FR-2.7 egress fidelity fix onto the signed-off v0.2. The delta SOURCE OF TRUTH is ' + OPINION + ' section 9 and ' + BRIEF + ' (the D1-D10 statements). D9 and D10 are deliberately HELD (not folded) per owner ruling — flag it as a DEFECT only if you find D9 or D10 content actually folded into an FR.',
  'SCOPE: review the v0.3 DELTAS and their integration with v0.2 (FR-11..18, NFR-18, FR-2.7, the changelog, the §8 held-items). Do NOT re-litigate the signed v0.2 FR-1..10 / NFR-1..17 / AC-1..75 except where a delta breaks or contradicts them. Report ONLY defects that change the document, most severe first.',
].join('\n\n')

const FIND = { type: 'object', required: ['findings'], properties: { findings: { type: 'array', items: { type: 'object',
  required: ['severity', 'location', 'issue', 'proposed_fix'], properties: {
    severity: { type: 'string', enum: ['blocker', 'major', 'minor'] },
    location: { type: 'string' }, issue: { type: 'string' }, proposed_fix: { type: 'string' } } } } } }

const lenses = [
  { key: 'fidelity', prompt: 'FIDELITY lens. Verify every new FR/NFR/AC faithfully represents its D-delta and the opinion/brief evidence. Flag: invented precision (a number or target presented as evidence-derived when the source gives none — new [owner target] numbers like the T-24h nudge, the concurrent-open-RSVP cap, the 40% concentration cap, the ~60s taste interview, digest cadence should be labelled [owner target] where they are product goals not evidence); misattributed dossier citations (D1/D2 cite d24/d25/d20/d21 etc. — check they match 00c); any delta that overstates what the research supports; and any place a new FR contradicts a HARD scope decision (free-RSVP only, no per-action confirmation, no IP rotation, no Gmail scope, RelayInbox inbound-only).' },
  { key: 'completeness', prompt: 'COMPLETENESS lens (staff architect). What is MISSING at requirements altitude in the deltas? Check cross-cutting integration: does the FR-11 attendance SCORE get privacy/erasure treatment (it is per-user PII — is it covered by FR-10.5 erasure)? Does FR-11 auto-un-RSVP reuse FR-8.8 correctly and honor the freeBusy/idempotency invariants? Does FR-12 sniping respect the same policy/kill-switch/audit as FR-5? Does FR-13 streaming-library ingest need a consent record (FR-2.9) and its own erasure path? Does FR-15 digest need the autonomy dial / quiet-hours as stored preferences? Does FR-16 out-of-band verification interact correctly with the FR-9.2 idempotent calendar write and the existing FR-5.5/5.3 detect/read guards (is it redundant or additive)? Does FR-17 signed-agent registration need a per-source capability flag like automation_allowed? Does FR-18 concentration cap have a defined behavior when it cannot be met (degrade vs block)? Flag only requirements-altitude gaps, each with a concrete fix.' },
  { key: 'testability', prompt: 'TESTABILITY lens. Every new FR must have a covering, executable AC; every new NFR a measurement method + number. Flag: any FR-11..18 sub-item with no covering AC; weasel words ("where available", "as appropriate") that are not pinned to an observable; ACs that are not executable as written; NFR-18 measurement rigor; SCOPE CREEP past D1-D8 (any D9/D10 content folded, or any new capability beyond the eight deltas); internal contradictions or duplicate requirements between a new FR and a v0.2 FR it extends (e.g. FR-16 vs FR-5.5, FR-12 vs FR-8.7a, FR-11.3 cap vs FR-7.1 limits); and any [owner target] number that should be flagged for ratification but is not.' },
]

phase('Review')
const reviews = (await parallel(lenses.map(l => () =>
  agent(COMMON + '\n\nYOUR LENS: ' + l.prompt, { label: 'review:' + l.key, phase: 'Review', schema: FIND, effort: 'high' })
    .then(r => r && { lens: l.key, findings: r.findings })
))).filter(Boolean)

const allFindings = reviews.flatMap(r => (r.findings || []).map(f => ({ lens: r.lens, ...f })))
log('review: ' + allFindings.length + ' findings (' + reviews.map(r => r.lens + ':' + r.findings.length).join(', ') + ')')

phase('Closure')
const closurePrompt = [
  'You are the closure-verification agent for the requirements v0.3 deltas. Read ' + REQ + ' in full.',
  'Here are the findings raised by the 3-lens review panel:\n' + JSON.stringify(allFindings, null, 1),
  'For EACH finding, judge whether the CURRENT document text already resolves it, partially resolves it, or misses it (the panel ran against the same text you are reading, so a finding is RESOLVED only if you judge it not actually a defect, MISSED if it is a real defect still present). Then independently hunt for any NEW defect the panel missed in the v0.3 deltas — especially a delta silently contradicting v0.2, a missing AC, an unlabelled invented number, or accidental D9/D10 scope creep. Give an overall verdict: SIGN-OFF-READY (deltas are faithful, complete, testable, no blockers) or NEEDS-REVISION (list the must-fix items). Rank must-fix items by severity.',
].join('\n\n')
const closure = await agent(closurePrompt, { label: 'closure', phase: 'Closure', schema: { type: 'object', required: ['verdict', 'must_fix', 'assessment'], properties: {
  verdict: { type: 'string', enum: ['SIGN-OFF-READY', 'NEEDS-REVISION'] },
  must_fix: { type: 'array', items: { type: 'object', required: ['severity', 'location', 'issue', 'fix'], properties: { severity: { type: 'string' }, location: { type: 'string' }, issue: { type: 'string' }, fix: { type: 'string' } } } },
  new_defects: { type: 'array', items: { type: 'string' } },
  assessment: { type: 'string' } } }, effort: 'xhigh' })

return { reviews, allFindings, closure }