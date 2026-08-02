# Phase 7.1 / 7.1a — End-to-end runs, structural diff, and rubric scoring

Four scenarios, run 2 August 2026 against real Companies House and Tavily data
from a scratch script outside the repo, in the same manner as every prior
phase's live pass. **12 Tavily credits spent; ~154 → ~142 of the development
reserve.** Full JSON state and rendered markdown for every run were dumped to
disk before analysis, so reading the output never costs a re-run.

Scenario selection was itself evidence-driven: a **zero-credit reconnaissance
pass** (Companies House only) resolved a dozen candidate queries first, so the
thin-evidence and ambiguous-name companies were chosen on what the register
actually returned rather than guessed at.

| # | Scenario | Query | Outcome | Credits | Wall clock |
|---|---|---|---|---|---|
| 1 | Well-documented | `Monzo Bank Ltd` | `complete` | 7 | 62.3s |
| 2 | Thin evidence | `Ashgrove Bespoke Cabinetry` | `complete` | 5 | 43.4s |
| 3 | Ambiguous name | `Sterling Services` | `needs_input` | 0 | 0.2s |
| 4a | Invalid (symbols) | `!!!` | `invalid_input` | 0 | 0.0s |
| 4b | Malformed (20-digit) | `12345678901234567890` | `not_found` | 0 | 0.0s |

Scenario 2 is ASHGROVE BESPOKE CABINETRY LTD (**16259703**), active,
incorporated 18 February 2025, SIC 16230 (manufacture of other builders'
carpentry and joinery), 7 filings / 1 officer / 2 PSC records. It was chosen
deliberately as a company with a real register footprint but no plausible press
coverage — and specifically because it is the shape of company that Phase 4
predicted would expose the filing-history enumeration limitation.

---

## 1. Do the confidence bands behave correctly?

| Scenario | Business model | Competitive landscape | Quality signals |
|---|---|---|---|
| Monzo | 🟡 MEDIUM (`minor_conflict`, 32/33) | 🔴 LOW (`major_conflict`, 45/46) | 🟢 HIGH (`corroborated`, 23/28) |
| Ashgrove | 🔴 LOW (`major_conflict`, 21/22) | 🔴 LOW (`major_conflict`, 6/6) | 🔴 LOW (`major_conflict`, 20/20) |

**The bands are mechanically correct in every case** — each one is what
`score_confidence()` returns given the claims and conflicts it was handed, and
the precedence order is visibly doing its work (a MAJOR conflict beats a
45-claim count; a MINOR conflict caps at MEDIUM regardless of 32 scored claims
and an `official_registry` source present).

**But mechanically correct is not the same as right, and two of the six bands
are right for the wrong reason.** Both cases are analysed in §4 below. Stating
it plainly here rather than burying it: the rule is behaving exactly as
specified, and the inputs it is being handed are wrong.

### Non-report outcomes

All three non-report scenarios behaved correctly and cost nothing:

- **`Sterling Services` → `needs_input`, 8 candidates, 0 credits, 0.2s.** Real,
  distinct entities (STERLING SERVICES (BRIDLINGTON) LIMITED, STERLING SERVICES
  KENT LTD, STERLING SERVICES NE LTD, STERLING SERVICES HOLDINGS LIMITED,
  STERLING SERVICES EDINBURGH LTD…), each carrying status, SIC codes,
  incorporation date and address — enough for a human to actually choose. The
  graph halts before any gather, so an ambiguous name costs no search credit.
  This is the disambiguation feature working, and it is a `200` carrying a typed
  outcome rather than a `4xx`.
- **`!!!` → `invalid_input`, 0.0s, no network call.** Rejected by
  `_validate_query` for containing no alphanumerics.
- **`12345678901234567890` → `not_found`.** Correct and honest, but **not what
  README §7 claimed.** See §5.

---

## 2. Structural diff against the Phase 0.7 mock (7.1's closing-the-loop requirement)

Run against the already-validated `structural_signature()` — a run, not new
development, exactly as Phase 5 arranged.

**11 differences, and none of them are shape.** Every structural property
matches: section count, order and headings; a badge with a band in each section;
gaps block, sources table and exclusion note present; the working table with
three rows; no summary table at the top; no trailing content after the working
table.

The 11 differences are band, one conflict block, and evidence volume:

| Difference | Mock | Real run |
|---|---|---|
| Section 2 band | HIGH | **LOW** |
| Section 2 conflict block | absent | **present** |
| Section 1 source rows / tiers | 4 | 8, far more `official_registry` |
| Section 2 source rows / tiers | 4 | 7, almost all `general_web` |
| Section 3 source rows / tiers | 3 | 7 |
| Citation markers (all three sections) | 4 / 4 / 3 | 6 / 6 / 7 |

Read honestly, that is **the mock working as specified: a floor on shape, not a
ceiling on quality.** The shape conformance 7.1 asks for holds. The band and
conflict-block differences in section 2 are not a shape failure — they are the
conflict-classification limitation, analysed below.

Phase 5's diff also produced exactly 11 differences, though the composition
moved (there it was section 1's band and section 3's conflict block; here it is
section 2 for both). The count matching is a coincidence, not a measurement, and
should not be quoted as stability.

**The thin-evidence report holds the identical shape too** — same headings,
badges, gaps blocks, sources tables and three-row working table. It differs from
Monzo only in band and volume, which is what the design intends: shape comes
from code, substance comes from evidence.

---

## 3. Rubric scoring (7.1a) — deliberately not referencing the mock

### Criterion 1 — no claim without a citation that resolves to the source it names

**Pass.** Every factual sentence in both reports carries a numbered marker
resolving to a row in that section's source table, and every row carries a real
URL and a retrieval date. This is structural rather than observed: the model is
never shown a URL at extraction or synthesis, so there is no mapping it could
invent — provenance is attached by code from the `TaggedDocument` the text came
from. Spot-checked against the register URLs and `monzo.com/annual-report`,
which resolve.

One qualification, and it belongs under criterion 3 rather than here: in the
Ashgrove run the citations resolve to the pages they name, but several of those
pages are **about a different company** (§4.2). The citation is not fabricated;
the attribution is wrong.

### Criterion 2 — no band contradicted by its own listed evidence

**Pass, and it cannot drift by construction.** The badge and the working table
both print from the same `ConfidenceAssessment` the graph scored on; neither
recounts claims. The strongest case is Monzo's competitive landscape: 45 scored
claims across 7 sources, and the badge still reads LOW, stating the MAJOR
conflict as the reason. A reader can check the arithmetic in the working table
and reach the same band.

Ashgrove section 2 is worth calling out as the honest case: **6 claims from 1
source, all `official_self`, band LOW**. A single self-source with six claims
does not clear the authority gate, and the report says so rather than dressing
six facts up as coverage.

### Criterion 3 — no claim stronger than its source supports

**Fail on the thin-evidence run; pass on Monzo.**

Monzo's prose is consistently hedged at the right strength — "the company's own
disclosure states", "this figure comes from a single non-regulatory source and
has not been corroborated elsewhere", "which should be read alongside, rather
than as confirmation of, the company's own growth narrative". Notably, the
Phase 5 date-conflation slip (*"full accounts for the year to 1 September
2025"*) **did not recur in this run's prose**; the one filing-type claim that
touched the same shape was excluded by quote verification. That is not evidence
the limitation is fixed — it is unfixed and untouched — only that it did not
manifest here.

Ashgrove fails, and the mechanism is §4.2: claims about three different
businesses are attributed to the subject company. A statement like *"The company
is based in Glasgow"* is stronger than its source supports, because the source
is a different company's website.

**Mitigating, and material:** the report does not present the conflated facts
confidently. It surfaces the contradiction, prints both locations, and states
that the evidence does not allow either to be confirmed — *"Both are reported
here without preference… this conflict is a primary reason the evidence in this
section supports only a low-confidence account of the business."* The
uncertainty machinery contained a grounding failure it was not designed to
catch. The defect is real; the blast radius was bounded by the rest of the
design.

### Criterion 4 — the thin company visibly, honestly thinner

**Pass, and measurably so** — but read §4.1 before treating it as validation.

| | Monzo | Ashgrove |
|---|---|---|
| Bands | MEDIUM / LOW / HIGH | **LOW / LOW / LOW** |
| Claims (scored) | 107 (100) | 48 (47) |
| Source rows across sections | 22 | 14 |
| Report length | 24,449 chars | 17,103 chars |
| Section 2 sources | 7 | **1** |

The prose difference is starker than the numbers. Ashgrove's competitive
landscape says outright that *"None of the six claims describe a named
competitor, a market share figure, a pricing comparison, or any third-party
commentary"* and declines to characterise competitive intensity at all. Monzo's
names Revolut, Starling and Chase UK with figures for each. Nobody reading the
two side by side would mistake one for the other.

**The honest caveat:** all three of Ashgrove's LOW bands fired for
`major_conflict`, not for `no_claims` or `weak_sources`. The report is correctly
thin, but the *thinness mechanism* is not what produced the LOW bands — the
conflation did. Criterion 4 passes on output and is not evidence that the
uncertainty rule would have caught this company's thinness on its own. §4.1
shows it would not have.

---

## 4. What reading the output found

### 4.1 The predicted filing-row exposure materialised — and was masked by a second defect

Phase 4 predicted the exposure precisely: *"nine filing rows alone clear HIGH's
`>= 3` gate with an `official_registry` source present, so a business with no
press coverage and no regulatory history could report HIGH confidence on the
strength of having filed its accounts."*

Ashgrove's business model section drew **11 scored `official_registry` claims**,
almost all register enumeration — incorporation date, SIC code, registered
office, and PSC rows listed one per record. That is 11 ≥ 3 with
`official_registry` present. **Under the rule, that section scores HIGH.**

It reported LOW only because the entity-conflation contradictions (§4.2) tripped
`major_conflict`, which is checked first. **Two defects cancelled**, and the
output looks correct as a result. Had the web search returned nothing at all —
entirely plausible for a company this small — there would have been no conflict,
and a 2025-incorporated joinery micro-business with no trading history would
have reported **HIGH confidence on its business model**.

Also observed, and predicted as unhandled by Phase 4: *"The company was
incorporated on 18 February 2025"* appears **twice** as two separate scored
claims, once from the profile document and once from filing history. Different
URLs give different ids, so `merge_claims` keeps both. Phase 4 recorded
cross-document duplication of one fact as unhandled and noted it would inflate a
count if it ever happened to scoring claims. Here it did.

### 4.2 New finding — web-evidence entity conflation on a shared distinctive name

**Not one of the three disclosed limitations. Disclosed as a fourth.**

The web gather for ASHGROVE BESPOKE CABINETRY LTD (registered office **Tonbridge,
Kent**) returned at least three different UK businesses, and extraction
attributed all of them to the subject:

- `ashgrovekitchens.co.uk` — **Glasgow**, 45–47 Dalsholm Avenue
- `ashgrovejoineryandinteriors.com` — **Devon**
- `ashgrovebespokecabinetry.co.uk` — tagged `official_self`, states **East Sussex**

**Zero entity warnings fired.** `names_a_different_entity()` — built in Phase 2
for exactly this class after a Better Business Bureau page for *Monzo, Inc.*
reached a UK report — is defeated by construction here, because every one of
these shares the distinctive token "Ashgrove". Phase 2's other half, anchoring
queries to the UK, does not help when all three confusable entities *are* UK
companies.

**Why this is worse than it first appears: the failure is correlated with thin
evidence.** A well-documented company dominates its own search results. A
micro-business with a generic-sounding name does not, so the engine fills the
ranking with same-named others. The mode bites hardest exactly where an
underwriter most needs the report to be careful.

Not fixed. A real fix — cross-checking web evidence against the registered
office, or requiring corroboration of identity before a web document may
contribute scoring claims — is data-gathering-layer work with its own decision,
and the same Phase 2 rendering decision the other two limitations are already
waiting on. Consistent with how every prior correctness-adjacent finding in this
project was handled under time pressure: disclosed with the reasoning, not
patched on submission eve. Recorded in README §5.

### 4.3 The conflict classifier is emitting self-refuting conflict records

A sharper statement of the disclosed basis-mismatch limitation, and it makes the
recommended fix cheaper than Phase 4 sketched.

In the Monzo run, business model's band is capped at MEDIUM by a MINOR conflict
whose own detail text reads:

> "These claims describe two separate foreign transaction charges (£5 for card
> delivery and 3% for card usage abroad) **rather than conflicting with each
> other**, so they represent different fees rather than a genuine disagreement."

The classifier emitted a conflict record that explains, in the same field, why
the two claims are not in conflict — and the band moved anyway. The thin run
contains a second instance ("these are different types of filings on different
dates"). `score_confidence` reads only `severity`; `detail` is rendered to the
reader and never inspected by the rule.

**That MINOR is the sole reason Monzo's business model is MEDIUM and not HIGH.**

Phase 4 proposed the fix as "a second classification pass that must state the
*basis* of the disagreement, giving a denominator mismatch somewhere to be
declared", and rejected it as doubling the cost of every report. This run shows
the model **already volunteers the basis, unprompted, in a field that already
exists** — so the fix is closer to making an existing signal load-bearing than
to adding a pass. That materially changes the cost estimate, and it is the
single most actionable thing in this document.

### 4.4 The Phase 6 second-order effect reproduced independently

Monzo's competitive landscape repeated the Phase 6 sequence on a different day,
different evidence and different figures:

1. First pass scores LOW.
2. The 3.5 branch fires correctly and re-searches at the section's own depth —
   *"regulatory and financial performance data from official filings rather than
   business analysis blogs"*.
3. Second pass: claims rise to 45 scored across 7 sources.
4. The section still finishes **LOW on `major_conflict`** — user counts of 10m
   vs 6.5m vs 9m, and "UK's largest digital-only bank" vs "7th largest UK bank".
   Different dates and different denominators; all can be true at once.

**More evidence lowered the confidence, again.** This is now observed twice, on
two independent runs, one of them on the deployed service. It is no longer a
single anecdote.

**New detail:** the $5.2bn-vs-$5bn valuation pair that Phase 6 classified MAJOR
was classified **MINOR** in this run. The same disagreement pattern, the same
prompt, two different severities — so severity classification is not stable
across runs. That is a further argument for the §4.3 fix, and against tuning the
classifier by adjusting its prompt.

### 4.5 One extraction failure, logged

The Monzo officers document returned `no JSON object in response` and
contributed **no claims** to that run. This is the failure shape Phase 5's
`json_payload` fix addressed for control characters; this instance is a
different cause (no parseable object at all), and it is handled correctly —
per-document isolation meant one bad response cost one document, not the report.
It is invisible in the output, which is the same criticism Phase 5 made of the
original strict-JSON bug: a warning line reads identically to a model returning
nonsense.

---

## 5. Documentation defect found by these runs

**README §7 overstates input validation.** It says:

> **Invalid input.** `!!!` or a 20-digit number is rejected before any network
> call.

The second half is wrong. `_validate_query` rejects empty input, input over the
length cap, input with no alphanumerics, and single characters. A 20-digit
number passes all four, fails the company-number pattern, and is sent to the
register as a name search — returning `NOT_FOUND`. The outcome is honest and
correct, but it is not `INVALID` and it is not free of a network call. Corrected
in Phase 7.4.

---

## 6. Summary

- **7.1 structural diff: passes.** 11 differences, none structural.
- **7.1a criteria 1, 2, 4: pass.** Criterion 3 **fails on the thin-evidence run**
  via the new entity-conflation finding, with the failure contained by the
  uncertainty machinery.
- **Bands behave correctly against the rule** in all six section-scores; two of
  the six are right for the wrong reason, both traced.
- **One new limitation disclosed** (entity conflation), **one predicted exposure
  observed** (filing-row enumeration reaching HIGH, masked), **one disclosed
  limitation sharpened** (self-refuting conflict records), **one prior finding
  reproduced** (re-search defeated by the classifier).
- **12 credits spent; ~142 remain.**
