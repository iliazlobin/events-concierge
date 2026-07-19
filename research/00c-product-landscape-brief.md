# Product Landscape Brief — Events Concierge

> Synthesis of the product-landscape dossiers (`18`–`25`) into a product opinion. Companion to the
> technical research brief (`00`) and gap addendum (`00b`); this brief does not restate their
> architecture/feasibility content. Every claim cites its dossier, e.g. "(d18)".

## The landscape at a glance

| Player class | Discover | Rank | Register | Calendar | Reconcile |
|---|---|---|---|---|---|
| Event platforms (Luma, Meetup, Partiful, Posh, Eventbrite, Fever/DICE) | own inventory only | personalized, inventory-biased | own checkout/RSVP | .ics export | own events only |
| ChatGPT ticketing apps (StubHub, Ticketmaster, SeatGeek) | NL, paid tickets | conversational | handoff to merchant checkout | no | no |
| Horizontal agents (ChatGPT agent, Gemini, Claude, Comet, Manus) | one-shot, on request | no persistent taste | pauses at login/RSVP/payment | weak, error-prone | no |
| Scheduling agents (Howie, Skej, Reclaim, Motion, Lindy) | n/a | n/a | meetings with humans, not events | yes | meetings only |
| OSS/indie aggregators + curated newsletters (206.events, VibrantSearch, The Skint, Funcheap) | cross-source lists/digests | editorial or none | no | ICS at best | no |
| DIY auto-RSVP bots (~28 Meetup scripts, 2011–2026) | no | no | blind single-source RSVP | no | no |

Every column has owners; no player connects them (d18, d19, d20, d21). NL discovery is
commoditized — StubHub, SeatGeek, and Ticketmaster all live inside ChatGPT, and n8n templates
replicate discover+rank in a weekend (d18, d21). **The white space is the connected back half:
autonomous cross-source registration plus a reconciled calendar. It is empty for a structural
reason — it is stateful, fee-less, per-source work misaligned with every incumbent's inventory
economics and every horizontal agent's usage-cap economics (d18, d19, d24).**

## What incumbents cover and what none of them close

As of July 2026 no incumbent closes the loop: Eventbrite, Luma, Partiful, Posh, Fever/DICE, and
Meetup all stop at personalized browse-and-register inside their own inventory; the ChatGPT
ticketing apps stop at discovery and redirect to the provider's checkout (d18). Within-inventory
reconcile exists (Luma updates invites for its own events); cross-source reconcile exists nowhere
(d18).

Three findings shape the opinion. First, every incumbent monetizes the transaction/inventory side —
Fever went furthest, steering its feed toward owned Originals — so a user-paid agent is the only
actor whose ranking can be incentive-clean, and can say so (d18). Second, consumers demonstrably pay
to delegate the social decision: Timeleft reached ~EUR 18M ARR (founder-reported) in ~20 months by
choosing the dinner for the user entirely (d18, d25). Third, the "AI event concierge with agency"
category is unclaimed but the window is closing: Posh raised a $37M Series B explicitly framed
around agentic plan-making, Partiful shipped ticketing, Eventbrite is adding AI feeds (d18).

Coverage gaps none of them close: cross-source anything; social/safety composition signals in a
recommendation rationale (Meetup made gender/age mix first-class precisely for the "should I go?"
decision); speed-to-register on scarce drops; and consent UX for agentic features — Bandsintown's
silent AI opt-in and the Songkick-to-Suno data transfer both drew user revolts, and the taste graph
those revolts were about is exactly the durable asset acquirers pay for (d18).

## The horizontal-agent substitute threat

A general agent is good enough for one-off, novel, cross-domain tasks and for paid-ticket discovery
with big-brand inventory — do not compete there (d19). But every major horizontal agent gates or
refuses exactly the step we automate: ChatGPT agent pauses entirely on logins and payments; Gemini
confirms before web-form submission; Claude for Chrome advises against credentialed transactions;
even Comet pauses for permission (d19). Their economics compound the gap: ~40 agent messages/month
on ChatGPT Plus cannot run continuous discovery, and calendar automation is among their most
consistent failure points (d19). The category itself consolidated violently — Operator, Atlas,
Project Mariner dead; MultiOn pivoted twice; Dia acquired — while free/community events (Meetup,
Luma, Partiful) have no ChatGPT app because there is no fee pool (d19).

Two 2026 rails redraw the legal/technical map: Amazon v. Perplexity established (preliminarily,
stayed on appeal) that user permission does not equal site authorization for disguised automation,
and Cloudflare's Web Bot Auth default-blocks anonymous agent traffic from Sept 15, 2026. Stealth
automation is dead; a registered, signed, well-behaved vertical agent could be whitelisted where
anonymous horizontal agents are blocked (d19).

The vertical wedge that survives: the recurring, stateful loop — standing taste brief, permitted
auto-RSVP in seconds, always-reconciled calendar — plus a hard-allowlisted action space (RSVP
endpoints and calendar writes only), which converts prompt-injection risk into a categorical safety
claim horizontal agents cannot make (d19). Critically, the moat must rest on economics and
statefulness, not agent incompetence: web-agent benchmark accuracy is closing fast, though the
transactional slice under real-world faults remains far below headline numbers (d19, d22).
Quarterly wedge-erosion watch: a Meetup/Luma app in ChatGPT's directory, Gemini relaxing its
form-submission gate, ChatGPT usage-cap raises or recurring tasks, the Ninth Circuit ruling (d19).

## What open source and indie builders keep proving and keep failing at

The OSS record is a 15-year natural experiment. None of ~40 surveyed projects closes the loop; OSS
clusters at organizer-side ticketing and ICS feed plumbing (d20). Platform-side clones die of
two-sided supply cold-start regardless of backing (freeCodeCamp's Chapter archived at 1,900 stars);
demand-side aggregation persists as long as parsers are maintained — and agentic maintenance has now
flipped that economics: 206.events runs a whole-city deduped aggregator via documented Claude Code
routines (d20). Auto-RSVP is a 15-year continuously re-proven demand: ~28 independent Meetup bot
repos, 2011 through June 2026, motivated by capacity races ("always full after 3 minutes") and
set-and-forget consistency for trusted groups — precisely our permitted-autonomy tier (d20, d21).

The indie record adds the demand-side postmortem. Event discovery is a YC-named tarpit: near-identical
finder apps launch on HN, get warm feedback, and stall, because lists don't retain (d21).
The #1 technical killer is data acquisition — every indie who scraped Eventbrite/Luma was blocked
immediately; URL-only dedupe leaves cross-platform duplicates; thin coverage (6 events in a 200k
metro) churns users instantly (d21). What survives for decades is trusted human niche curation with
push delivery — The Skint (2009–), Funcheap SF (2003–, ~150k subscribers), 19hz.info — proving
retention comes from trust, voice, and push, not search; these outlets are simultaneously our best
long-tail sources and culturally anti-scrape, so attribution etiquette is a distribution asset
(d21). And the Hugecity postmortem's behavioral core: browsing doesn't produce attendance, personal
invitations and commitment do — the founder named the concierge model as a viable escape (d21).

## The graveyard: what kills this category, and our exposure

Fifteen years of corpses encode four failure modes (d23):

1. **Frequency/retention death.** Event-seeking is structurally low-frequency (90% of Hugecity users
   visited ≤1x/week); destination apps never form habits; IRL faked 95% of its 20M MAU rather than
   admit it. **Neutralized by design:** value is delivered as confirmed calendar entries in zero-open
   weeks; the calendar and push digest are the surface, not an app; Songkick's artist-tracking — the
   one retention mechanic that ever worked — generalizes into our standing NL brief (d23).
2. **No monetization between discovery and the ticket.** Affiliate/ads aggregation capped or killed
   Zvents, YPlan (sold for ~5 cents on the dollar), Dojo; DICE lost $51M on $28.5M revenue owning
   checkout; every survivor monetizes something else (Fever's owned inventory, ticketing rails,
   organizer subscriptions). Nobody ever survived charging for the feed itself. **Neutralized:** we
   price the labor — search+register+reconcile — as a consumer subscription; Jukely proved ~$25/mo
   events WTP exists but died subsidizing inventory; we sell work, not tickets (d23, d25).
3. **The supply scrape war.** Facebook's event corpus is permanently API-dark; Bending Spoons now
   owns BOTH Meetup and Eventbrite with a documented fee-hike/paywall playbook; Songkick died in
   Ticketmaster's kill zone. **Not neutralized — our largest standing risk.** Mitigations, not cures:
   per-user authenticated access (harder to lock out than central scraping), agent-maintained parser
   fleets, no source >40% of a metro's supply, connector deprecation playbooks (d23, d20).
4. **Geographic overexpansion.** Multi-city scaling killed YPlan, Sosh, Jukely, WillCall — per-city
   supply cost with no cross-city network effect. **Partially neutralized:** agentic scraper
   economics cut per-metro cost, but supply density per request is still a hard gate; a thin week one
   is trust death. Launch one metro deep with an explicit density bar (d23, d21).

The tailwind: for the first time since ~2010 there is no dominant default discovery surface —
Facebook Events collapsed, Meetup is decaying under fee hikes — and that fragmentation is exactly
what makes a cross-source concierge valuable (d23).

## Trust & autonomy UX patterns to adopt

The 2026 survey corpus quantifies the trust cliff our honest split is built on: 74% would delegate
routine tasks, 32% accept delegation within set parameters, only 9% accept autonomous payment; ~2%
of consumers would use fully autonomous travel booking. Free-RSVP-only launch scope sits below the
cliff — keep it (d24). Agent mistakes are near-unforgivable: six in ten UK consumers would stop
using an agent after one mistake, making wrong-RSVP/stale-calendar rate the top product metric and
reconciliation a retention feature, not polish (d24).

Every human-labor concierge at consumer prices died or fled (Yohana $249/mo dead; Fin, Magic,
Clara's human tier); the live market is software-margin agents. Pricing anchors: Reclaim $8–12,
Skej $10–15, Motion $19–29, Howie $25–95 (1,000 paying customers for an agent that acts on your
calendar), Lindy $49.99, Duckbill $49–350 (human-backstopped ceiling), Dorsia showing premium money
flows to guaranteed access, not recommendations (d24). The only affordable human in a consumer
concierge is the user's own tap — the honest split is the cost structure the graveyard prescribes
(d24).

Adopt the convergent six-pattern autonomy kit as requirements: (1) graduated per-lane autonomy,
confirm-first with promotion after observed correct actions (Lindy); (2) upfront policy budgets in
the standing brief; (3) preview-before-commit; (4) digest-then-user-decides as the default level
(Google Ask for Me; gentle-default Reclaim retains where aggressive Motion generates complaints);
(5) receipts with visible undo/un-RSVP; (6) auto-demotion to confirm on novelty or risk (d24). Frame
delegation as a familiar ritual — briefing a concierge, not enabling AI (d24). Two more lessons:
platform incumbents are absorbing per-vertical "acting" (Google, OpenTable, Expedia MCP), so RSVP
itself will commoditize — durable differentiation is cross-source + standing brief + reconciled
calendar; and single-feature calendar automation dies standalone even with traction (Clockwise,
$76M, killed in a week) — own the whole loop (d24). Finally, regulation: six states are legislating
against bot bookings without genuine intent (bot reservations no-show 4x); aggressive cancel hygiene
is simultaneously the regulatory shield, the organizer-trust moat, and a user feature (d24, d25).

## Who the user is and what they will pay

Demand is real, current, and fragmented. No platform owns the "what should I do Friday" query —
word-of-mouth leads discovery at 47%, social media at 64% for Gen Z (d25). The tailwind is the
loneliness/third-places narrative: 79% of 18–35s plan to attend more events in 2026; 89% want
events that connect them to community (d25).

Willingness to pay attaches to outcomes, never discovery. The verified ladder: information ≤$5–10/mo
(The Nudge); curation + commitment ~$20/mo (222, Timeleft); autonomous watch-and-book $10–30/mo
(Campnab); guaranteed human-backstopped execution $49–99+/mo (Duckbill) (d25). Pure discovery stays
free/ad-supported or dies (d21, d23, d25).

The wedge user is the 25–40 urban "socially displaced" adult — new-in-town movers, remote workers,
post-friend-group millennials — with money, intent, and no local discovery graph; living across
Meetup + Luma + Eventbrite in one metro (d25, d18). Not Gen Z: they discover on TikTok, enjoy the
hunt, and expect free (d25). Framing: decision fatigue beats FOMO — this cohort resents both
over-curated feeds and planning overhead (79% value spontaneity, 51% want logistics fully handled);
present 1–3 confident picks per ask, never a catalog (d25). One pathology to own before it owns us:
free-RSVP no-shows run 40–60%, organizers already ban serial flakes, and an auto-RSVP agent without
an attendance loop amplifies the ecosystem's worst behavior at scale (d25).

## Implications: positioning, wedge, and deltas to the signed-off requirements

**Positioning.** Claim the empty quadrant and the category name "AI event concierge" now — Posh is
circling (d18). Identity = the action layer: "one brief, every source, your calendar stays true."
Position against the horizontal agents' gap ("general agents stop and ask at the RSVP; we finish the
job — in seconds, unattended, reconciled"), not against event platforms (d19). Market the honest
split as the industry-validated trust pattern — the market's biggest players landed on
agent-discovers + merchant-completes after burning money on full autonomy (d19, d24). North-star
metric: confirmed (and attended) events per user per month — explicitly not DAU (d18, d23).

**Wedge and GTM.** One metro (NYC) at full depth including the non-ticketed long tail, with an
explicit coverage bar gating metro 2 (d21, d23). Distribute through scenes and city newsletters,
not the Fever-owned SERP; ride the loneliness narrative with the neutral-agent story no
inventory-owner can copy (d25). Pricing: free digest + one-tap handoff; paid $15–25/mo for
autonomy, sniping, and reconciliation (under the $20 ChatGPT anchor, inside the proven band);
optional per-success fee for scarce-event acquisition; future $50+ guaranteed-access tier; flat
pricing, never credits, never human ops (d19, d24, d25).

**Deltas to `design/requirements.md` v0.2.** The spec already covers the loop, honest-split
routing, handoff, policy, and organizer-side reconcile. The landscape adds:

- **D1 — Attendance loop / cancel hygiene (highest priority).** T-24h attendance confirmation,
  auto-un-RSVP on decline/no-response/conflict, per-user concurrent-open-RSVP cap, attendance score
  throttling autonomy. Extends FR-8's lifecycle with user-side intent decay (d25, d24).
- **D2 — RSVP sniping as a named feature.** Watch trusted groups/venues, instant RSVP on open,
  waitlist-promotion detection — the clearest chargeable moment in free-RSVP scope (d20, d21).
- **D3 — Taste cold-start onboarding.** 60-second taste interview, group-membership import,
  streaming-library ingest (DICE/RA pattern), Qloo build-vs-buy evaluation. Extends FR-4 (d18, d22).
- **D4 — Social/safety signals in the ranking rationale.** Attendee composition and
  friends/community signals where available; "why I picked this" must include who will be there
  (d18).
- **D5 — Weekly digest as the primary proactive surface,** with voice/personality and one-tap
  actions; push (email/SMS), not app opens. Elevates FR-6 from notification contract to product
  surface (d21, d25).
- **D6 — Independent out-of-band RSVP verification** before any calendar write — 68% of agent
  booking failures are silent false successes; strengthens FR-5/FR-8 confirmation (d22).
- **D7 — Agent-identity policy + signed-agent registration.** Never spoof, honor blocks, pursue
  Cloudflare Web Bot Auth; extends FR-10's legal posture (d19).
- **D8 — Supply-density preflight + honest-failure UX** (widen constraints rather than pad weak
  matches) and a named supply-risk register with Bending Spoons as risk #1 and a per-source
  concentration cap (d23).
- **D9 — Growth/coverage bridges.** Forwardable invite cards with their own one-tap handoff;
  paste-a-link ingestion (TikTok/IG/Partiful URL → extract → calendar) to honestly bridge the
  private-graph blind spot (d25).
- **D10 — Graduated-autonomy onboarding.** The evidence says gentle-default-then-promote wins trust
  (d24). This tenses against confirmed decision 3 (no per-action confirmation): reconcile it as
  per-lane default *policy* — digest-approve default, full-auto opt-in per lane — not interactive
  gates. Flagged for the owner below.

## Open product questions for the owner

1. **Autonomy default at onboarding:** full-auto day one (decision 3 read strictly) vs
   digest-approve default with per-lane promotion (d24 evidence)? Is graduated autonomy a UX ramp
   within decision 3, or a scope change?
2. **Pricing confirmation:** $15–25/mo paid tier + free digest; include a per-success sniping fee?
3. **Launch metro:** confirm NYC; set the numeric coverage/density bar that gates metro 2.
4. **Digest surface and voice:** email vs SMS vs Telegram; how much personality?
5. **Category naming/brand:** move now on "AI event concierge" given Posh's trajectory?
6. **Cold start:** Qloo buy vs build; streaming-library import at launch or fast-follow?
7. **Attendance-score policy:** aggressiveness of auto-un-RSVP; is the score user-visible?
8. **Signed-agent registration:** pursue Web Bot Auth pre-launch or post-launch?
9. **Organizer-side posture:** stay pure demand-side, or seed the future "concierge users show up"
   supply asset (d23's optional wedge) in v1 messaging?
10. **Wedge-erosion review:** who owns the quarterly watch-list (ChatGPT app directory, Gemini form
    gate, usage caps, Ninth Circuit ruling)?
