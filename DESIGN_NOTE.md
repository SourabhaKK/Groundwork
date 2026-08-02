# Groundwork — System Design Note

*Sourabha Kallapur · August 2026*

---

## 1. What it does, and why you can trust it

Underwriting a business you have never heard of starts the same way every time.
Is this the company they actually mean? What does it do to make money? Who is it
up against? Is there anything on the record that should worry me?

Groundwork does that first pass. You give it a company name or a registration
number; it identifies the specific legal entity on the Companies House register,
gathers what the register and the open web hold, and returns a report in three
sections — business model, competitive landscape, quality signals — with every
claim linked to the source it came from.

That much is table stakes for anything built on an LLM. The part I actually
designed around is the second question an underwriter asks, which is not *what
does it say* but **how much of this should I believe**.

The report reads like prose, but the trustworthy part of this system is
everything around the prose — every badge, citation, and confidence band is
computed and rendered by code from data the model never gets to touch. The
writing is the least important thing this system does correctly.

Concretely: **each section carries its own confidence band, produced by a rule
you can read, and the report shows its working.** A section tells you how many
claims it rests on, what kinds of sources those were, what evidence was missing,
and where two sources disagreed. A thin section says it is thin and looks thin.

Four design commitments make that trustworthy rather than merely stated. Each is
a structural property of the code, not a promise the model is asked to keep —
and that distinction is the whole argument of this document.

**A citation cannot be fabricated, because the model is never shown a URL.**
Extraction runs one document at a time. The model receives the text and returns
a fact plus a verbatim quote; `source_url`, `source_type` and `section` are
attached afterwards by code, from the document the text came from. There is no
field through which an invented URL could arrive. The obvious alternative —
give the model the whole section's documents and let it cite them — requires
handing it a menu of URLs to choose from, which is precisely the mechanism by
which a claim gets bound to the wrong source.

**A confidence band is a function, not a judgement.** `score_confidence()`
evaluates stated conditions in a fixed order and returns a band plus the reason
that fired. Asking a model how confident it feels produces a number nobody can
audit, nobody can test, and which drifts between model versions. This one has
eighteen unit tests over it and did not change once between Phase 3 and
submission.

**The model writes prose; code renders every piece of structure.** Badges,
citation numbering, source tables, conflict blocks, evidence-gap blocks and the
working table are assembled from data. Three guarantees fall out at once: a
badge cannot drift from the score, since both print from the same assessment
object; a citation cannot be invented, since numbering is bound before the
prose exists; and the primary and fallback models cannot produce
differently-shaped reports. That last one is not an argument — it was measured.
Run against identical evidence, Claude and Gemini produced **identical
structural signatures** while their prose differed completely.

**Uncertainty is surfaced, not disclaimed.** Conflicts between sources are
reported rather than resolved, with both sides printed. Claims that fail quote
verification stay in the report with the reason attached, because dropping them
would make the report look thinner than the evidence gathered *and hide why*.
Gaps are stated per section.

One thing this is not: a credit decision, or a substitute for reading the
filings. It is triage that tells you how much to trust each part of itself.

---

## 2. Architecture

```
                    ┌─────────────────────────────────────────┐
   query ─────────► │  resolve  (Companies House search)      │
                    └───────────────┬─────────────────────────┘
                                    │
              ┌─────────────────────┼──────────────────────┐
              ▼                     ▼                      ▼
      ┌───────────────┐   ┌──────────────────┐        ┌─────────┐
      │ gather_       │   │ gather_web       │        │  halt   │ ◄── unresolvable,
      │ registry      │   │ (Tavily, 1 query │        └─────────┘     ambiguous, or
      │ (filings,     │   │  per section)    │                        no evidence
      │  officers,    │   └────────┬─────────┘
      │  PSC)         │            │
      └───────┬───────┘            │
              └────────┬───────────┘
                       ▼
              ┌─────────────────┐
              │ assemble_       │   tag every document by source authority
              │ evidence        │   (registry / self / trade press / review / web)
              └────────┬────────┘
                       ▼
              ┌─────────────────┐
         ┌───►│ extract         │   one model call per document → EvidenceClaim,
         │    └────────┬────────┘   each with a quote checked against its source
         │             ▼
         │    ┌─────────────────┐
         │    │ evaluate_       │   score_confidence() per section
         │    │ sufficiency     │
         │    └────────┬────────┘
         │             │
         │   LOW *and* recoverable?
         │             │
         │    ┌────────▼────────┐
         └────┤ research_gap    │   one targeted re-search per thin section,
              └─────────────────┘   at that section's own depth. Bounded by a
                       │            counter on state, not by a missing edge.
                       ▼
              ┌─────────────────┐
              │ synthesize      │   model writes prose only; code renders every
              └────────┬────────┘   badge, citation, table and gap block
                       ▼
                    report
```

A LangGraph state machine over typed Python functions. FastAPI serves it,
Streamlit consumes it, Cloud Run hosts it.

**Layers.** `src/data_sources/` (register and search clients, source tagging,
per-source circuit breakers) → `src/agent/` (resolution, the graph, extraction)
→ `src/report/` (confidence scoring, synthesis, rendering) → `src/api/` (gate,
rate limits, endpoint). `src/llm/` sits beside them: one router and one circuit
breaker that every model call passes through, so no module talks to a provider
SDK directly.

**Two things in that diagram are the point rather than decoration.**

The gather branches are **two real graph nodes, not one node calling
`asyncio.gather`**. The wall-clock is identical. The difference is that the
concurrency and the per-source failure branch are visible in the graph, and the
graph is a deliverable — agent design is graded on whether the structure can be
inspected.

`evaluate → research_gap → extract → evaluate` is **a genuine cycle**. Control
flow returns to an earlier node because of what the evidence turned out to be,
and it exits because a counter on the state says so, not because the edge back
is missing. `recursion_limit` is a backstop against a counter bug, not the
bound. That distinction is what separates an agent from a pipeline with a retry
in it.

---

## 3. Key decisions, and what I rejected

Tool-level choices and their rejected alternatives are in `TECH_STACK.md` and
are not re-derived here. These are the decisions that shaped the behaviour.

**Auto-resolution is deliberately hard to reach.** A query resolves silently
only on a unique exact normalised-name match against an active, non-dormant
company. Everything else returns candidates, or one company flagged for
confirmation with the reasons attached — and the reasons are a *list*, because a
dissolved company found under a former name is both things at once and reporting
whichever was checked first hands back the less alarming half at random.
*Rejected:* returning the best match. Under an underwriting brief, quietly
reporting on the wrong entity is the worst failure available, and it is
unrecoverable because nothing downstream can detect it.

**Gathering is partial-tolerant; resolution is all-or-nothing.** These are
deliberately opposite. Resolution makes a uniqueness claim that a partial fetch
silently weakens, so if any shortlisted profile fetch fails the whole resolution
fails. Gathering only accumulates, so a failing source degrades to a *recorded*
gap the report prints. Two failures are never degraded: an auth error raises,
because a rejected key would otherwise print "no officers recorded" for every
company we ever look at; and a 404 on the PSC endpoint is a *finding*, because
that is genuinely how the register says nobody is registered as controlling this
company.

**The agent branches on the same function the report publishes.** Phase 3.5's
re-search fires when a section scores LOW *for a reason more evidence could
fix*, judged by `score_confidence()` itself rather than a parallel heuristic.
The refined query is generated by the cheap model and then validated
mechanically — rejected if it drops the company's distinctive name tokens,
exceeds a length cap, or overlaps the original above a similarity threshold —
before a credit is spent. *Rejected:* a separate sufficiency heuristic. The
moment the two disagreed, the agent would be re-searching sections that score
fine, or proceeding on sections it will later mark LOW.

**A contradicted section is deliberately not re-searched.** LOW for
`major_conflict` is excluded from the recoverable set.

I chose not to let the agent re-search its way out of a conflict, because more
evidence isn't the right tool for resolving a contradiction — it's the right
tool for filling a gap. I'd rather leave a contested claim visibly contested
than have the system quietly out-vote it with volume.

The honest cost of that choice: more evidence *would* sometimes adjudicate a
conflict, and this declines to try. §5.1 shows the decision interacting badly
with a real defect.

**The decision log records the times the agent did nothing.** Every section gets
an entry every run, carrying the band, the reason, the claim count, and whether
a re-search fired *and if not, why not*. A log that recorded only the times the
agent acted could not be audited for judgement — you could not tell restraint
from inattention.

**Only a provider failure is an HTTP error.** A company that cannot be found, an
ambiguous name, and a company with thin evidence are all `200`s carrying a typed
outcome. They are findings, not client errors, and rendering disambiguation as a
`4xx` would make the product's best behaviour look like a fault. `503` is
reserved for "our model providers are down", which must never reach a reader
wearing the same clothes as "this company is thinly documented".

**Config fails fast on placeholder credentials, not just missing ones.** This
was learned the hard way: three keys were still `REPLACE_ME_*` when the
deployment skeleton first went up. The build succeeded, `/health` returned 200,
and the service could not have served a single report — because a placeholder
key starts cleanly and fails on every outbound call, surfacing as *a report with
no evidence in it*. That is indistinguishable from an honest report about a
thinly documented company, which is the one signal this system must never fake.
**The test suite could not have caught it** — every external call is mocked by
design, so a present-but-useless credential is invisible to it. It took one real
deployment.

**The gate and the circuit breaker were built first, not last.** They were
originally scheduled for the final phase and moved to the first, so every later
phase was developed against real auth, real rate limits and real
provider-fallback constraints rather than being exempted until the end.

---

## 4. What was deliberately deferred, and why

**Deeper register analysis** — SIC-code changes over time, officer turnover
rate, filing punctuality. Scoped as a stretch item and not built. Worth being
precise: the PSC records and full filing history are already gathered, tagged
and sitting in the evidence bundle, so this is *unbuilt analysis*, not missing
data.

**Progress streaming on `/report`.** Considered and rejected rather than
skipped. The decision log delivers what a progress bar would and delivers it
auditably, after the fact, including the steps where the agent decided to do
nothing.

**Multi-instance-safe rate limiting.** Both counters live in the API process.
They reset if the container is replaced and would multiply if the service scaled
past one instance; deployment pins `--max-instances=2` to bound the second case.
The durable ceiling is upstream anyway — the search free tier stops on its own.
This is a courteous speed bump, documented as such rather than presented as
airtight.

**Trading and brand names that were never registered.** Former *registered*
names work: searching "Focus FS" returns Monzo Bank Limited, confirmed against
the register's own `previous_company_names` before anything is surfaced. Brand
names that were never registered are structurally unreachable — "Mondo" returns
zero results for Monzo at any rank, confirmed by sweeping the complete result
set. *Rejected:* asking an LLM to map a trading name to a legal entity, which
introduces a fabrication surface at the one step where the register gives us
ground truth; and a hand-maintained trading-name table, which is unbounded
maintenance for arbitrary coverage.

**A second frontend deployment.** The Streamlit client runs locally against the
hosted API. A second hosting account, a second secrets path and a second cold
start, on submission eve, for a UI the brief says not to polish.

**`SectionRender.warnings` is not surfaced to the API response.** Flagged and
left rather than absorbed: surfacing it means changing a graph seam, a report
module, the graph node and their tests, which is not a submission-eve change.

A section's write-up can fail without the report failing, and that part *is*
built and tested: the section still renders its real band, sources, gaps and
conflicts from data, and states that the missing element is the written summary
rather than the evidence. A total provider failure is the opposite case and
halts the run. What the unsurfaced warnings cost is knowing when it happened —
on the same Phase 6 deployed run discussed in §5.1, Section 3's prose is absent
while its band and sources render normally, and the response still reported
`outcome: complete` with the reason recorded only in the server log. Disclosed
in README §5 so an evaluator who meets it reads it as designed behaviour.

---

## 5. What would change with more time

Ordered by what the evidence says matters most, not by what is easiest.

### 5.1 Make the conflict classifier state the basis of a disagreement — and read it

This is the strongest finding in the project, it has now been observed on three
independent runs including the deployed service, and it is the one change I
would make first.

Conflict severity is judged by a model. It reliably misreads figures that differ
by **basis** — different denominators, different instruments, different dates —
as contradictions. Two claims like *"40% of customers use Monzo as their main
account"* and *"49% of monthly active users use it as a primary bank"* are not
in conflict; they have different denominators and both can be true.

**Why it matters more than a misclassification normally would: it works against
the agent's best feature.** On the deployed service, competitive landscape
first scored LOW for *weak sources* — the section had drawn SEO content farms.
The 3.5 re-search branch fired correctly, produced a genuinely gap-targeted
query, and **solved that problem**: scored claims went from 16 to 42. The
section then still scored LOW — now for `major_conflict`, because the new
evidence carried the valuation-language pattern the classifier misreads. Three
"MAJOR conflicts": a $5.2bn valuation against a $5bn Series I round, that round
against a later employee secondary near $6bn, and a $501m raise against a $600m
one. Different instruments at different dates; all true simultaneously.

**More evidence lowered the confidence.** The adaptive re-search and the
conflict classifier are currently working against each other's purpose. It
reproduced independently in Phase 7 testing on different evidence (user counts
of 10m vs 6.5m vs 9m), so it is a pattern, not an anecdote. The one-pass bound
on re-search is the only reason it does not compound.

**Phase 7 testing changed what this fix costs, and that is the reason it is
first on this list rather than third.**

In Phase 4 I scoped this fix as a *second classification pass* — a separate
model call per conflict, required to declare the basis of the disagreement — and
rejected it on cost, because it roughly doubles the classification spend on
every report. That estimate was wrong, and reading the Phase 7 conflict records
is what showed it. The classifier had emitted a MINOR conflict whose own
`detail` field reads:

> "These claims describe two separate foreign transaction charges … **rather
> than conflicting with each other**, so they represent different fees rather
> than a genuine disagreement."

**The classifier is already writing the correct reasoning into `detail`, and
`score_confidence()` never reads it.** The rule branches on `severity` alone;
`detail` is rendered to the reader and otherwise discarded. So the model has
already done the work the second pass was going to pay for — it diagnosed the
basis mismatch correctly, unprompted, in a field that already exists on the
object.

That drops the fix from *"add a second classification pass"* to *"read a field
that's already populated."* No extra model call, no extra latency, no extra
cost per report. The work is to make the basis a structured field rather than
free prose, and to let a declared basis mismatch downgrade or discard the
conflict instead of letting `severity` decide alone.

The evidence that this matters rather than being a tidy observation: that single
self-refuting MINOR is the sole reason Monzo's business model scored MEDIUM
rather than HIGH. And a further argument against trying to fix this by tuning
the prompt instead — the same $5.2bn-vs-$5bn valuation pair was classified MAJOR
in one run and MINOR in another, so `severity` is not stable across runs even
when the underlying disagreement is identical. The field that stayed correct
across both was `detail`.

**Why it was not fixed:** the direction of the error is safe. A false MAJOR can
only *understate* confidence, never inflate it, and both sides of every
disagreement are printed for the reader to judge. Under a fixed deadline I would
rather ship a disclosed, safe-direction error than an undisclosed semantic
patch to the scoring path.

### 5.2 Fix how filing history is rendered into an evidence document

Extraction is meant to turn a list of filings into the pattern across them —
"accounts filed in each of the last six years" — plus any individually notable
row. It does that for the officers document and does not for filing history,
which comes back one claim per row.

**The cause was isolated rather than guessed, and it is not prompt
non-compliance.** The filing-history document renders the 100 most recent
filings, roughly 90 of which are opaque officer-movement codes. Given the same
document with those rows removed, the same prompt and the same model return
exactly the three intended pattern claims. Two attempted fixes failed on
evidence: a per-document scoring cap (rejected — it changes what a claim count
means, which is the settled scoring spec), and a document-specific worked
example in the prompt (tried; made it *worse*, 9 claims to 12).

The same document is responsible for a second defect: a filing row renders as
`2025-09-01 — accounts — Full accounts made up to 2025-02-28`, and the leading
date is **the only unlabelled date anywhere in the register documents** — its
meaning is positional convention. In one run that produced *"full accounts for
the year to 1 September 2025"*, conflating the filed date with the period end.
For an underwriter that is not cosmetic: the period an account covers is what
makes a figure current or stale.

**Phase 7 testing turned the predicted exposure into an observed one.** The
thin-evidence run drew 11 scored `official_registry` claims almost entirely from
register enumeration. That clears the HIGH band's threshold with an
`official_registry` source present — so a 2025-incorporated joinery
micro-business with no trading history *scores HIGH on its business model* under
the rule. It reported LOW only because a second defect (§5.3) tripped the
conflict check first. **Two errors cancelled**, which is exactly the kind of
thing that ships looking correct.

The fix belongs in how the document is rendered — collapse the officer-movement
rows into a count, label the filed date — not in the prompt and not in a scoring
cap. Both changes are small. Both change what an evidence document *contains*,
which is a data-gathering-layer decision, and they should be taken together.

### 5.3 Verify entity identity for web evidence, not just for resolution

Found in Phase 7 testing and disclosed as a fourth limitation rather than
patched.

The web gather for ASHGROVE BESPOKE CABINETRY LTD — registered office Tonbridge,
Kent — returned at least three different UK businesses, and extraction
attributed all of them to the subject: a Glasgow cabinetmaker, a Devon joinery
firm, and a site claiming East Sussex.

**No entity warning fired, and the reason is structural.** The mechanism built
for exactly this class — after a Better Business Bureau page for *Monzo, Inc.*
reached a UK report — flags documents whose names lack the subject's distinctive
tokens. Every one of these shares the token "Ashgrove". Anchoring queries to the
UK, the other half of that fix, does not help when all three confusable entities
*are* UK companies.

**The property that makes this matter: the failure is correlated with thin
evidence.** A well-documented company dominates its own search results; a
micro-business with a generic name does not, so the ranking fills with
same-named others. The mode bites hardest exactly where an underwriter most
needs care.

**What contained it.** The report did not state the conflated facts
confidently — it surfaced the contradiction, printed both locations, and said
the evidence did not allow either to be confirmed. The uncertainty machinery
bounded a grounding failure it was not designed to catch. That is genuinely
reassuring about the architecture and *not* a reason to leave the defect: the
attribution is still wrong.

The fix is to require identity corroboration — matching the registered office,
company number or officer names — before a web document may contribute *scoring*
claims, leaving uncorroborated ones in prose as non-scoring. Same layer, and the
same decision, as §5.2.

### 5.4 Smaller items

- **Cross-document duplication of a single fact.** "Incorporated on 18 February
  2025" appeared twice as two scored claims, from the profile and filing-history
  documents. Different URLs give different claim ids, so deduplication keeps
  both. Known and unhandled; observed inflating a count for the first time in
  Phase 7.
- **Quote verification is lossy on tables.** Roughly one claim in five failed
  verification in earlier testing, concentrated in pages whose content arrives
  as a mangled table where the figure is present but not as a contiguous span.
  The direction is safe — the claim renders, it just does not score — and
  loosening the check to accept fragments would stop it catching fabrication.
- **The register's *next accounts due* date is never rendered** into any
  document, so the system cannot claim anything about filing deadlines even
  though the register holds one. Missing evidence rather than wrong evidence.
- **Deeper register analysis**, per §4 — the data is already gathered.

---

## 6. What I would tell an evaluator to look at first

Start with the Ashgrove finding in §5.2 and §5.3 — the thin-evidence run where
two defects cancelled each other out. I would point at it before anything else
in this repo, and not because it is the worst bug. It is the strongest
demonstration of the discipline the project was actually built with.

Here is why I rate it that way. In Phase 4 I named a specific risk in writing:
that a thinly-documented company could clear the HIGH confidence threshold on
the strength of its filing rows alone, with no press coverage and no trading
history behind it. In Phase 7 I ran a real company of exactly that shape, and
the risk materialised precisely as named — eleven scored register claims, enough
to score HIGH on business model. It reported LOW anyway, because a completely
separate defect tripped the conflict check one step earlier and hid it.

Every automated signal available said that run was fine. The band was plausible,
the report was well-formed, the structural diff passed, and three of the four
rubric criteria passed. **The only thing that caught it was reading the claims
one by one and asking where each had come from.** A pass/fail check would have
recorded a healthy run, and the exposure would have gone into the submission
undetected — not because it was hidden, but because nothing was looking at it
from the right angle.

That is the habit I would most want judged here: predictions written down early
enough to be embarrassing later, live runs read rather than counted, and two
errors that happened to produce a correct-looking answer reported as two errors
rather than one success. The same habit produced every other finding in §5.

After that, look at the disambiguation path — `Sterling Services` returns eight
real candidates with what you need to choose between them, costs no search
credit, and re-submits by registration number when you pick one. Then put any
small recently-incorporated company's report next to Monzo's. If the thin one
does not look visibly thinner, the uncertainty mechanism is not working, and
that comparison is faster than reading either report end to end.

---

## Appendix — provenance of this document

The reasoning, decisions and rejected alternatives here are mine, developed
across the build and recorded phase by phase in `BUILD_LOG.md` as they happened,
including the ones that turned out wrong. Claude Code was used as the
implementation tool throughout, under a process (`workflows.md`) requiring every
non-trivial decision to be stated with its rejected alternative and confirmed
before code was written — specifically so that the judgement in this codebase
would be defensible rather than merely present.
